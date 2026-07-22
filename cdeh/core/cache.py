"""In-memory transfer cache.

For repeated reads of the same object (e.g. a daily export that
re-runs the same query), we cache the last fetched bytes for a short
TTL. The cache is keyed on (adapter, path) and uses an LRU bound.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple


class TransferCache:
    def __init__(self, max_items: int = 256, ttl_seconds: int = 300):
        self.max_items = max_items
        self.ttl = ttl_seconds
        self._lock = threading.RLock()
        self._data: "OrderedDict[Tuple[str, str], Tuple[float, bytes]]" = OrderedDict()

    def get(self, adapter: str, path: str) -> Optional[bytes]:
        """Returns the cached bytes, or None. Cached `b""` (empty file)
        is correctly returned as `b""` — not coalesced with absent."""
        key = (adapter, path)
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, data = entry
            if now - ts > self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return data

    def put(self, adapter: str, path: str, data: bytes) -> None:
        key = (adapter, path)
        now = time.time()
        with self._lock:
            self._data[key] = (now, data)
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)

    def mark_present(self, adapter: str, path: str) -> None:
        """Record that an object exists at (adapter, path) without caching
        its bytes. Use this for large files where re-fetching on cache
        miss is expensive but incremental-skip can be done from
        server-side etag alone."""
        self.put(adapter, path, b"")  # b"" is a valid cached value; see get()

    def invalidate(self, adapter: str = None, path: str = None) -> int:
        """Drop one entry or all entries for an adapter. Returns count dropped."""
        with self._lock:
            if adapter is None and path is None:
                n = len(self._data)
                self._data.clear()
                return n
            keys_to_drop = [k for k in self._data
                            if (adapter is None or k[0] == adapter)
                            and (path is None or k[1] == path)]
            for k in keys_to_drop:
                del self._data[k]
            return len(keys_to_drop)

    def stats(self) -> dict:
        with self._lock:
            total_bytes = sum(len(d) for _, d in self._data.values())
            return {
                "items": len(self._data),
                "bytes": total_bytes,
                "max_items": self.max_items,
                "ttl_seconds": self.ttl,
            }