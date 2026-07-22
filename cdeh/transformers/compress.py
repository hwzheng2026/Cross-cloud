"""Compression transformer — transparent gzip / zstd for in-flight bytes.

Useful when:
- source is a small cloud in a region with high egress cost
- bandwidth is the bottleneck (compress before transfer, decompress
  on destination — net payload is smaller)

Usage:
    "compress:gzip"           # gzip with default level (6)
    "compress:zstd"           # zstd (smaller + faster, requires `zstandard`)
    "compress:gzip:9"         # gzip level 9 (max compression)
    "compress:gzip:1"         # gzip level 1 (fastest)

For the local adapter the payload is compressed bytes on disk. For
real cloud destinations the destination adapter sees compressed bytes
as well — so you'd need a sibling decoder on the consumer side, or
decompress in the same share's transformer chain (two-pass).
"""
from __future__ import annotations

import gzip
import io
from typing import Any, Dict

from .base import BaseTransformer


class CompressTransformer(BaseTransformer):
    kind = "compress"

    def __init__(self, algo: str = "gzip", level: int = 6):
        self.algo = algo.lower()
        self.level = level

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "CompressTransformer":
        algo = cfg.get("algo", "gzip")
        level = int(cfg.get("level", 6))
        return cls(algo=algo, level=level)

    def transform(self, data: bytes, params: Dict[str, Any]) -> bytes:
        if self.algo == "gzip":
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb",
                                compresslevel=self.level) as gz:
                gz.write(data)
            return buf.getvalue()
        if self.algo == "zstd":
            try:
                import zstandard as zstd
            except ImportError:
                # graceful fallback: zstd isn't installed, do gzip
                buf = io.BytesIO()
                with gzip.GzipFile(fileobj=buf, mode="wb",
                                    compresslevel=self.level) as gz:
                    gz.write(data)
                return buf.getvalue()
            return zstd.ZstdCompressor(level=self.level).compress(data)
        raise ValueError(f"unknown compression algo: {self.algo!r}; "
                          "use 'gzip' or 'zstd'")

    def __repr__(self) -> str:
        return f"CompressTransformer(algo={self.algo!r}, level={self.level})"