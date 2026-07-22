"""Field-level mask — for tabular data (CSV / JSON), replace specific
columns' values with a mask token before transfer.

Usage:
    mask:email,phone,ssn
    mask:email                          # email only
    mask::0,1,2,3                       # positional columns

The transformer auto-detects CSV vs JSON-line by sniffing the first
non-whitespace byte; CSV is rewritten, JSON-line is left untouched
(use `redact:` for JSON objects).
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Dict, List

from .base import BaseTransformer, TransformerError


# Email / phone / SSN detection. Conservative — false negatives are OK,
# false positives could leak PII we meant to mask.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[- ]?)?(?:\d{3}[- ]?\d{3,4}[- ]?\d{4}|\d{10,11})(?!\d)")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


class MaskTransformer(BaseTransformer):
    kind = "mask"

    def __init__(self, columns: List[str] = None,
                 field_patterns: List[str] = None,
                 mask_char: str = "*",
                 preserve_length: bool = True,
                 detect_in_text: bool = False):
        """columns: CSV column names to mask (or [''] to mask all).
        field_patterns: pre-defined patterns to scan for in plain text.
        """
        self.columns = columns or []
        self.field_patterns = field_patterns or []
        self.mask_char = mask_char
        self.preserve_length = preserve_length
        self.detect_in_text = detect_in_text
        # Pre-compile patterns
        self._regexes = []
        for p in self.field_patterns:
            if p == "email":
                self._regexes.append(_EMAIL)
            elif p == "phone":
                self._regexes.append(_PHONE)
            elif p == "ssn":
                self._regexes.append(_SSN)
            else:
                try:
                    self._regexes.append(re.compile(p))
                except re.error as e:
                    raise TransformerError(f"bad regex {p!r}: {e}")

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "MaskTransformer":
        cols = cfg.get("columns") or []
        if isinstance(cols, str):
            cols = [c.strip() for c in cols.split(",") if c.strip()]
        pats = cfg.get("patterns") or []
        if isinstance(pats, str):
            pats = [p.strip() for p in pats.split(",") if p.strip()]
        return cls(
            columns=cols,
            field_patterns=pats,
            mask_char=cfg.get("mask_char", "*"),
            preserve_length=cfg.get("preserve_length", True),
            detect_in_text=cfg.get("detect_in_text", False),
        )

    def _mask_value(self, val: str) -> str:
        if val is None or val == "":
            return val
        if self.preserve_length:
            return self.mask_char * max(3, min(len(val), 32))
        return self.mask_char * 4

    def _scan_text(self, text: str) -> str:
        for rx in self._regexes:
            text = rx.sub(lambda m: self.mask_char * len(m.group(0)), text)
        return text

    def transform(self, data: bytes, params: Dict[str, Any]) -> bytes:
        text = data.decode("utf-8", errors="replace")
        # Try CSV first if it looks like one (heuristic: starts with letter/digit then comma)
        head = text.lstrip()[:512]
        is_csv_like = ("," in head and "\n" in head and
                        (head.startswith('"') or re.match(r"^[\w\-]+,", head)))
        if is_csv_like and (self.columns or self.field_patterns):
            rdr = csv.DictReader(io.StringIO(text))
            out_rows = []
            for row in rdr:
                masked = {}
                for k, v in row.items():
                    if self.columns and k in self.columns:
                        masked[k] = self._mask_value(v)
                    elif self.field_patterns and not self.columns:
                        masked[k] = self._scan_text(str(v))
                    else:
                        masked[k] = v
                out_rows.append(masked)
            buf = io.StringIO()
            if out_rows:
                wr = csv.DictWriter(buf, fieldnames=list(out_rows[0].keys()))
                wr.writeheader()
                wr.writerows(out_rows)
            return buf.getvalue().encode("utf-8")
        # Plain text: scan with regex
        if self.field_patterns:
            return self._scan_text(text).encode("utf-8")
        return data  # nothing to do