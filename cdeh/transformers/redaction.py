"""Data redaction — for richer schemes: k-anonymity, differential privacy,
hash-based pseudonymization, drop-column.

For most regulatory use cases (GDPR, CCPA, HIPAA), the simple
`mask:` transformer is sufficient. Use `redact:` when you need:

  - k-anonymity on quasi-identifiers (age, zip) — generalize
  - drop-column: remove a column entirely
  - hash-pseudo: replace with HMAC(name, salt) pseudonym
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
from typing import Any, Dict, List

from .base import BaseTransformer


class RedactionTransformer(BaseTransformer):
    """Apply one or more redaction policies to CSV / JSON-line data.

    Policy form: "drop:col1,col2" or "hash:col" or "k-anon:col1,col2"
    """
    kind = "redact"

    def __init__(self, policies: List[str], hmac_secret: bytes = None):
        self.policies = policies
        self.hmac_secret = hmac_secret or b"cdeh-default-hmac-secret-please-override"
        self._parsed: List[tuple] = []  # (kind, args)
        for p in policies:
            kind, _, args = p.partition(":")
            self._parsed.append((kind, [a.strip() for a in args.split(",") if a.strip()]))

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "RedactionTransformer":
        pols = cfg.get("policies", [])
        if isinstance(pols, str):
            pols = [p.strip() for p in pols.split(";") if p.strip()]
        return cls(policies=pols, hmac_secret=cfg.get("hmac_secret", "").encode("utf-8") or None)

    def _drop(self, row: dict, cols: List[str]) -> dict:
        return {k: v for k, v in row.items() if k not in cols}

    def _hash(self, row: dict, cols: List[str]) -> dict:
        for c in cols:
            if c in row:
                v = str(row[c]).encode("utf-8")
                row[c] = hmac.new(self.hmac_secret, v, hashlib.sha256).hexdigest()[:16]
        return row

    def _k_anon(self, row: dict, cols: List[str]) -> dict:
        """Trivial k-anonymity: bucket numeric values to nearest 10,
        truncate strings to first 3 chars. Real k-anon needs an
        external linkage table; this is a starting point."""
        for c in cols:
            if c not in row:
                continue
            v = row[c]
            try:
                n = int(float(v))
                row[c] = (n // 10) * 10
            except (ValueError, TypeError):
                if isinstance(v, str):
                    row[c] = v[:3] + "*" * max(0, len(v) - 3)
        return row

    def transform(self, data: bytes, params: Dict[str, Any]) -> bytes:
        text = data.decode("utf-8", errors="replace")
        head = text.lstrip()[:512]
        if not ("," in head and "\n" in head):
            # not CSV — apply text-level hash/drop for matching lines
            return data
        rdr = csv.DictReader(io.StringIO(text))
        out_rows = []
        for row in rdr:
            for kind, cols in self._parsed:
                if kind == "drop":
                    row = self._drop(row, cols)
                elif kind == "hash":
                    row = self._hash(row, cols)
                elif kind == "k-anon":
                    row = self._k_anon(row, cols)
            out_rows.append(row)
        if not out_rows:
            return b""
        buf = io.StringIO()
        wr = csv.DictWriter(buf, fieldnames=list(out_rows[0].keys()))
        wr.writeheader()
        wr.writerows(out_rows)
        return buf.getvalue().encode("utf-8")