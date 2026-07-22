"""Data catalog — in-memory registry of all known data assets and their
metadata (schema, tags, lineage, version).

Persisted as JSON at `~/.cdeh/catalog.json` by default. Production
deployments would swap this for a real Data Catalog (Apache Atlas,
DataHub, Unity Catalog, AWS Glue Catalog, etc.).
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclasses.dataclass
class DataAsset:
    """Metadata about a single shareable data asset."""
    name: str
    adapter: str                  # e.g. "s3", "azure_blob"
    path: str                    # e.g. "/orders/2024.csv"
    schema: List[Dict[str, str]] = dataclasses.field(default_factory=list)
    tags: List[str] = dataclasses.field(default_factory=list)
    classification: str = "internal"   # public | internal | confidential | restricted
    owner: str = ""
    version: int = 1
    created_at: str = dataclasses.field(default_factory=lambda: _now())
    last_sync: str = ""
    extra: Dict[str, Any] = dataclasses.field(default_factory=dict)
    # Per-path source-fingerprint history for incremental-skip across
    # transforms. Keyed by canonical src path; value is the fingerprint
    # we last wrote to dst.
    src_fingerprints: Dict[str, str] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DataAsset":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class DataCatalog:
    """Thread-safe in-memory catalog with atomic JSON persistence."""

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or os.path.expanduser("~/.cdeh/catalog.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._assets: Dict[str, DataAsset] = {}
        self._load()

    def _load(self):
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text())
                    for name, d in raw.items():
                        self._assets[name] = DataAsset.from_dict(d)
                except (json.JSONDecodeError, KeyError):
                    self._assets = {}

    def _save(self):
        # atomic write: tmp + rename
        tmp = self.path.with_suffix(".tmp")
        with self._lock:
            data = {n: a.to_dict() for n, a in self._assets.items()}
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(self.path)

    def register(self, asset: DataAsset) -> DataAsset:
        with self._lock:
            existing = self._assets.get(asset.name)
            if existing:
                asset.version = existing.version + 1
            asset.created_at = existing.created_at if existing else _now()
            self._assets[asset.name] = asset
            self._save()
            return asset

    def get(self, name: str) -> Optional[DataAsset]:
        with self._lock:
            return self._assets.get(name)

    def list(self, tag: Optional[str] = None,
             classification: Optional[str] = None) -> List[DataAsset]:
        with self._lock:
            items = list(self._assets.values())
        if tag:
            items = [a for a in items if tag in a.tags]
        if classification:
            items = [a for a in items if a.classification == classification]
        return items

    def delete(self, name: str) -> bool:
        with self._lock:
            if name in self._assets:
                del self._assets[name]
                self._save()
                return True
            return False

    def mark_synced(self, name: str) -> None:
        with self._lock:
            a = self._assets.get(name)
            if a:
                a.last_sync = _now()
                self._save()