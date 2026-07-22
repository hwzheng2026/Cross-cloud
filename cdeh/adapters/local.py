"""Local filesystem adapter — useful for testing and on-prem sources."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, Optional, Tuple, Union

from .base import (
    AdapterError, BaseAdapter, FileStat, _guess_ct,
)


class LocalAdapter(BaseAdapter):
    """Read/write files on the local filesystem.

    `path` semantics: a virtual "bucket" + "key" combined as
    `bucket/key` where `bucket` maps to a local directory. The default
    bucket `""` maps to the configured `root`.
    """
    kind = "local"

    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "LocalAdapter":
        root = cfg.get("root") or cfg.get("path")
        if not root:
            raise AdapterError("LocalAdapter requires config['root'] or config['path']")
        return cls(root=root)

    def _resolve(self, path: str) -> Path:
        # `path` is like "/foo/bar.csv" or "" (root). Strip leading slash, no
        # bucket concept here. Sandbox via realpath() to prevent escape.
        rel = path.lstrip("/")
        full = (self.root / rel).resolve()
        if not str(full).startswith(str(self.root)):
            raise AdapterError(f"path escapes root: {path}")
        return full

    def ping(self) -> Dict[str, Any]:
        return {"root": str(self.root), "exists": self.root.exists()}

    def stat(self, path: str) -> FileStat:
        full = self._resolve(path)
        if not full.exists():
            raise AdapterError(f"not found: {path}")
        st = full.stat()
        return FileStat(
            path="/" + str(full.relative_to(self.root)),
            size=st.st_size,
            etag=LocalAdapter._compute_etag_for(full),
            mtime_ns=st.st_mtime_ns,
            content_type=_guess_ct(full),
        )

    def list(self, prefix: str = "", recursive: bool = True) -> Iterator[FileStat]:
        base = self._resolve(prefix) if prefix else self.root
        if not base.exists():
            return
        if base.is_file():
            yield self.stat("/" + str(base.relative_to(self.root)))
            return
        glob = "**/*" if recursive else "*"
        for p in base.glob(glob):
            if p.is_file():
                rel = "/" + str(p.relative_to(self.root))
                try:
                    yield self.stat(rel)
                except AdapterError:
                    pass

    def get(self, path: str, range_: Optional[Tuple[int, int]] = None) -> bytes:
        full = self._resolve(path)
        if not full.exists():
            raise AdapterError(f"not found: {path}")
        if range_ is None:
            return full.read_bytes()
        start, end = range_
        with open(full, "rb") as f:
            f.seek(start)
            return f.read(end - start + 1)

    def put(self, path: str, data: Union[bytes, BinaryIO],
           content_type: str = "",
           metadata: Optional[Dict[str, str]] = None) -> FileStat:
        full = self._resolve(path)
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, (bytes, bytearray)):
            full.write_bytes(data)
        else:
            with open(full, "wb") as f:
                # chunked to avoid huge memory
                while True:
                    chunk = data.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        return self.stat(path)

    def delete(self, path: str) -> None:
        full = self._resolve(path)
        if full.is_dir():
            import shutil
            shutil.rmtree(full, ignore_errors=True)
        elif full.exists():
            full.unlink()

    @staticmethod
    def _compute_etag_for(path: Path) -> str:
        """Multi-part-style S3 etag: 'md5-<hex-of-md5s>'. For small files
        (≤ 8 MiB) the multi-part count is 1 so this collapses to plain md5."""
        import hashlib
        size = path.stat().st_size
        if size == 0:
            return hashlib.md5(b"").hexdigest()
        part_size = 8 * 1024 * 1024
        if size <= part_size:
            return hashlib.md5(path.read_bytes()).hexdigest()
        # multi-part
        md5s = b""
        with open(path, "rb") as f:
            while True:
                chunk = f.read(part_size)
                if not chunk:
                    break
                md5s += hashlib.md5(chunk).digest()
            n_parts = (size + part_size - 1) // part_size
        return f"{hashlib.md5(md5s).hexdigest()}-{n_parts}"