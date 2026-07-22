"""Base adapter — common interface for all storage backends.

Every concrete adapter (S3, Azure Blob, MySQL, ...) subclasses
BaseAdapter and registers itself under a unique `kind` string via
the `cdeh.adapters.registry` decorator.

The interface is intentionally small — adapters do I/O, the rest of
C-DEH handles catalog / policy / transfer / audit.
"""
from __future__ import annotations

import abc
import dataclasses
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Tuple, Union


class AdapterError(Exception):
    """Base class for adapter-level errors."""


class AdapterNotFound(AdapterError):
    pass


class AdapterAuthError(AdapterError):
    pass


class AdapterNotSupported(AdapterError):
    pass


@dataclasses.dataclass
class FileStat:
    """Lightweight file/object stat returned by `stat()`.

    `etag` and `mtime_ns` are the two fields the TransferEngine uses to
    decide whether to skip a file (incremental sync). Keep them cheap
    to fetch.
    """
    path: str                       # canonical "/key" within the bucket/container
    size: int
    etag: str                       # Opaque. S3 etag, MD5, sha256, etc. Empty if not available.
    mtime_ns: int                   # last-modified time in nanoseconds since epoch
    content_type: str = ""
    metadata: Dict[str, str] = dataclasses.field(default_factory=dict)

    def fingerprint(self) -> str:
        """Stable fingerprint for incremental-skip comparison.

        Prefer etag (cheap, server-side, immutable) over mtime. Falls
        back to `(size, mtime_ns)` for backends that don't expose etag.
        """
        if self.etag:
            return f"etag:{self.etag}"
        return f"sm:{self.size}:{self.mtime_ns}"


class BaseAdapter(abc.ABC):
    """Abstract base for all C-DEH adapters.

    Concrete subclasses MUST:
      - set `kind: str` (class attribute, e.g. "s3", "azure_blob", "sftp")
      - implement `stat`, `get`, `put`, `list`, `delete`, `ping`
      - register themselves via the `cdeh.adapters.register` decorator
    """

    kind: str = ""  # subclasses override

    # ─── factory ─────────────────────────────────────────────────────
    @classmethod
    @abc.abstractmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "BaseAdapter":
        """Build an adapter from a plain-dict config (from CLI or YAML)."""

    # ─── capability discovery ───────────────────────────────────────
    @abc.abstractmethod
    def ping(self) -> Dict[str, Any]:
        """Test connectivity. Returns backend info dict (region, bucket, ...)."""

    # ─── file/object operations ─────────────────────────────────────
    @abc.abstractmethod
    def stat(self, path: str) -> FileStat:
        """Stat a single object. Raise AdapterError if not found."""

    @abc.abstractmethod
    def list(self, prefix: str = "", recursive: bool = True) -> Iterator[FileStat]:
        """Iterate over objects under `prefix`. Empty prefix = full list."""

    @abc.abstractmethod
    def get(self, path: str, range_: Optional[Tuple[int, int]] = None) -> bytes:
        """Download object. `range_=(start,end)` for partial read (inclusive)."""

    @abc.abstractmethod
    def put(self, path: str, data: Union[bytes, BinaryIO],
           content_type: str = "",
           metadata: Optional[Dict[str, str]] = None) -> FileStat:
        """Upload object. Returns the new FileStat (with etag/size/mtime)."""

    @abc.abstractmethod
    def delete(self, path: str) -> None:
        """Delete a single object. Idempotent (no error if already gone)."""

    # ─── bulk helpers (default impl uses the above) ────────────────
    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except AdapterError:
            return False

    def put_file(self, path: str, local: Union[str, Path],
                 content_type: str = "") -> FileStat:
        """Upload a local file. Override for backends that support
        server-side copy from a mounted FS."""
        local = Path(local)
        with open(local, "rb") as f:
            return self.put(path, f, content_type=content_type or _guess_ct(local))

    def get_to_file(self, path: str, local: Union[str, Path]) -> Path:
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        with open(local, "wb") as f:
            f.write(self.get(path))
        return local

    def compute_etag(self, data: bytes) -> str:
        """Default etag computation for backends that don't have one.
        Returns MD5 hex (S3-compatible single-part format)."""
        return hashlib.md5(data).hexdigest()


# ─── helpers shared by adapters ─────────────────────────────────────
_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")

def parse_http_range(header_val: str) -> Optional[Tuple[int, int]]:
    """Parse `bytes=START-END` per RFC 7233. Returns (start, end) inclusive."""
    if not header_val:
        return None
    m = _RANGE_RE.match(header_val.strip())
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else None
    return (start, end)


_CT_MAP = {
    ".csv": "text/csv", ".json": "application/json", ".parquet": "application/octet-stream",
    ".txt": "text/plain", ".log": "text/plain", ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".bin": "application/octet-stream", ".xml": "application/xml",
}

def _guess_ct(path: Path) -> str:
    return _CT_MAP.get(path.suffix.lower(), "application/octet-stream")


def utcnow_ns() -> int:
    return time.time_ns()