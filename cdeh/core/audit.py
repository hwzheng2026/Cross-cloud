"""Audit log — append-only, hash-chained.

Each entry references the previous entry's hash (a la blockchain /
certificate transparency). Tampering with any past entry is detectable
by recomputing the chain.

Entries are stored as JSONL at `~/.cdeh/audit.log`. Rotated by line
count (default 100k) or by size (default 50 MB), whichever first.
"""
from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclasses.dataclass
class AuditEntry:
    ts: str                       # ISO-8601 with TZ
    user: str
    action: str                   # "share.run" | "adapter.register" | "transfer" | ...
    asset: str = ""
    src_adapter: str = ""
    src_path: str = ""
    dst_adapter: str = ""
    dst_path: str = ""
    bytes: int = 0
    policy: str = ""
    transforms: str = ""          # comma-separated
    success: bool = True
    error: str = ""
    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)
    prev_hash: str = ""           # hash of the previous entry (hex, first 16 chars)
    entry_hash: str = ""          # sha256 of the entry content (hex, first 16 chars)

    def canonical(self) -> bytes:
        """Stable serialization for hashing (excludes entry_hash itself)."""
        d = dataclasses.asdict(self)
        d.pop("entry_hash", None)
        return json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")

    def to_jsonl(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False)


class AuditLog:
    def __init__(self, path: Optional[str] = None,
                 max_lines: int = 100_000, max_bytes: int = 50 * 1024 * 1024):
        self.path = Path(path or os.path.expanduser("~/.cdeh/audit.log"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._last_hash = "0" * 16
        self._line_count = 0
        self._bootstrap()

    def _bootstrap(self):
        with self._lock:
            if not self.path.exists():
                self.path.touch()
            self._line_count = 0
            self._last_hash = "0" * 16
            # Replay last 10 entries to rebuild the chain head
            try:
                with open(self.path, "rb") as f:
                    lines = f.readlines()[-10:]
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                        self._last_hash = d.get("entry_hash", self._last_hash)
                        self._line_count += 1
                    except json.JSONDecodeError:
                        pass
            except FileNotFoundError:
                pass

    def append(self, **fields) -> AuditEntry:
        """Append a new entry. Computes prev_hash + entry_hash automatically."""
        with self._lock:
            entry = AuditEntry(
                ts=fields.get("ts") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                user=fields.get("user", "system"),
                action=fields.get("action", ""),
                asset=fields.get("asset", ""),
                src_adapter=fields.get("src_adapter", ""),
                src_path=fields.get("src_path", ""),
                dst_adapter=fields.get("dst_adapter", ""),
                dst_path=fields.get("dst_path", ""),
                bytes=int(fields.get("bytes", 0)),
                policy=fields.get("policy", ""),
                transforms=fields.get("transforms", ""),
                success=bool(fields.get("success", True)),
                error=fields.get("error", ""),
                extra=fields.get("extra") or {},
                prev_hash=self._last_hash,
            )
            entry.entry_hash = hashlib.sha256(entry.canonical()).hexdigest()[:16]
            # Append (we hold the lock for the whole append so the chain
            # is always consistent under concurrent writers)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(entry.to_jsonl() + "\n")
            self._last_hash = entry.entry_hash
            self._line_count += 1
            self._maybe_rotate()
            return entry

    def _maybe_rotate(self):
        if self._line_count < self.max_lines and self.path.stat().st_size < self.max_bytes:
            return
        # rotate: audit.log -> audit.log.1
        rotated = self.path.with_suffix(".log.1")
        try:
            if rotated.exists():
                rotated.unlink()
        except FileNotFoundError:
            pass
        self.path.rename(rotated)
        self.path.touch()
        self._line_count = 0
        # New chain starts fresh
        self._last_hash = "0" * 16
        # Note: old chain is still verifiable as long as the rotated
        # file isn't tampered with. We log a "chain rotation" entry.
        rotation_entry = AuditEntry(
            ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            user="system", action="audit.rotate",
            prev_hash="0" * 16, extra={"rotated_to": str(rotated)},
        )
        rotation_entry.entry_hash = hashlib.sha256(rotation_entry.canonical()).hexdigest()[:16]
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(rotation_entry.to_jsonl() + "\n")
        self._last_hash = rotation_entry.entry_hash
        self._line_count = 1

    def verify_chain(self, path: Optional[Path] = None) -> Dict[str, Any]:
        """Walk the entire audit log and recompute the hash chain.

        Returns {"ok": bool, "broken_at": int, "entries": int}.
        """
        p = path or self.path
        prev = "0" * 16
        n = 0
        for i, line in enumerate(p.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                return {"ok": False, "broken_at": i, "entries": n, "reason": "json"}
            if d.get("prev_hash") != prev:
                return {"ok": False, "broken_at": i, "entries": n, "reason": "prev_hash_mismatch"}
            # Recompute entry_hash
            entry_d = dict(d)
            stored = entry_d.pop("entry_hash", None)
            canonical = json.dumps(entry_d, sort_keys=True, ensure_ascii=False).encode("utf-8")
            computed = hashlib.sha256(canonical).hexdigest()[:16]
            if computed != stored:
                return {"ok": False, "broken_at": i, "entries": n, "reason": "entry_hash_mismatch"}
            prev = stored
            n += 1
        return {"ok": True, "entries": n}

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                with open(self.path, "rb") as f:
                    lines = f.readlines()[-n:]
            except FileNotFoundError:
                return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out