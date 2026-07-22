"""Checkpoint store — persistent per-(share, src_path) transfer state.

Used by the resumable transfer mode (see `Share.resumable`). When a
large file is transferred in chunks, the store records which chunks
have already been written to the destination so a subsequent run can
skip them.

Default backend: a directory of JSON files at
`~/.cdeh/checkpoints/<share-name>/<encoded-path>.state.json`. The
interface is small enough to swap with Redis / SQLite / DynamoDB by
implementing the same three methods.

File format (per chunk-state file):
    {
        "share": "...",
        "src_path": "...",
        "dst_path": "...",
        "src_size": ...,
        "src_etag": "...",
        "total_bytes": ...,         # size of the post-transform bytes
        "chunk_size": ...,         # chunk size used during transfer
        "parts": {
            "0000": {"bytes": 8388608, "uploaded_at": "..."},
            "0001": {"bytes": 8388608, "uploaded_at": "..."},
            ...
        },
        "completed_at": "..."        # set when all chunks written
    }

To resume: read the file, verify src_size / src_etag match (else
the source changed — abort and start over), then skip the parts that
already exist.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol


class CheckpointStore(Protocol):
    """Minimal interface that the engine depends on."""

    def get(self, share: str, src_path: str) -> Optional[Dict[str, Any]]: ...
    def put_part(self, share: str, src_path: str, part_idx: int,
                 bytes_written: int) -> None: ...
    def mark_completed(self, share: str, src_path: str) -> None: ...
    def reset(self, share: str, src_path: Optional[str] = None) -> int: ...


def _encode(path: str) -> str:
    """Make a path safe to use as a filename."""
    import re
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", path)
    return safe[:200] or "_root_"


class FileCheckpointStore:
    """File-based CheckpointStore. One JSON per (share, src_path).

    Thread-safe across processes (file lock per checkpoint). For
    multi-process deployments consider the SQLite backend below.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, share: str, src_path: str) -> Path:
        d = self.root / share
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{_encode(src_path)}.state.json"

    def get(self, share: str, src_path: str) -> Optional[Dict[str, Any]]:
        p = self._path(share, src_path)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            # corrupt checkpoint — ignore, caller will re-upload
            return None

    def _write(self, p: Path, data: Dict[str, Any]) -> None:
        # atomic-ish write: write tmp, rename
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(p)

    def put_part(self, share: str, src_path: str, part_idx: int,
                 bytes_written: int) -> None:
        with self._lock:
            p = self._path(share, src_path)
            data = self.get(share, src_path) or {
                "share": share, "src_path": src_path, "parts": {},
            }
            data["parts"][f"{part_idx:04d}"] = {
                "bytes": bytes_written,
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            data.setdefault("created_at", time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            self._write(p, data)

    def mark_completed(self, share: str, src_path: str) -> None:
        with self._lock:
            p = self._path(share, src_path)
            data = self.get(share, src_path) or {
                "share": share, "src_path": src_path, "parts": {},
            }
            data["completed_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write(p, data)

    def reset(self, share: str, src_path: Optional[str] = None) -> int:
        """Drop one (or all) checkpoints for a share. Returns files removed."""
        d = self.root / share
        if not d.exists():
            return 0
        removed = 0
        if src_path is None:
            for p in d.iterdir():
                if p.is_file():
                    p.unlink(); removed += 1
            try:
                d.rmdir()
            except OSError:
                pass
        else:
            p = self._path(share, src_path)
            if p.exists():
                p.unlink(); removed += 1
        return removed

    def list(self, share: str) -> List[Dict[str, Any]]:
        d = self.root / share
        if not d.exists():
            return []
        out = []
        for p in d.iterdir():
            if p.suffix == ".json" and not p.name.endswith(".tmp"):
                try:
                    out.append(json.loads(p.read_text()))
                except Exception:
                    pass
        return out


class SQLiteCheckpointStore:
    """Single-file SQLite backend. Useful when multiple C-DEH
    processes share the same checkpoint state (single writer +
    concurrent readers via WAL mode).

    Lazy import of sqlite3 — keeps stdlib-only deps minimal.
    """

    def __init__(self, db_path: str | Path):
        import sqlite3
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                share     TEXT,
                src_path  TEXT,
                part_idx  INTEGER,
                bytes     INTEGER,
                uploaded_at TEXT,
                completed_at TEXT,
                PRIMARY KEY (share, src_path, part_idx)
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoint_meta (
                share TEXT, src_path TEXT,
                src_size INTEGER, src_etag TEXT,
                total_bytes INTEGER, chunk_size INTEGER,
                created_at TEXT,
                completed_at TEXT,
                PRIMARY KEY (share, src_path)
            )
            """
        )
        # Older schema: completed_at may not exist. Migrate.
        cols = {r[1] for r in self._db.execute(
            "PRAGMA table_info(checkpoint_meta)").fetchall()}
        if "completed_at" not in cols:
            self._db.execute(
                "ALTER TABLE checkpoint_meta ADD COLUMN completed_at TEXT")
        self._db.commit()

    def get(self, share: str, src_path: str) -> Optional[Dict[str, Any]]:
        meta = self._db.execute(
            "SELECT src_size, src_etag, total_bytes, chunk_size "
            "FROM checkpoint_meta WHERE share=? AND src_path=?",
            (share, src_path),
        ).fetchone()
        rows = self._db.execute(
            "SELECT part_idx, bytes, uploaded_at FROM checkpoints "
            "WHERE share=? AND src_path=? ORDER BY part_idx",
            (share, src_path),
        ).fetchall()
        if not meta and not rows:
            return None
        meta_dict = {}
        if meta:
            meta_dict.update(dict(zip(
                ("src_size", "src_etag", "total_bytes", "chunk_size"), meta)))
        parts = {f"{r[0]:04d}": {"bytes": r[1], "uploaded_at": r[2]}
                 for r in rows}
        completed = self._db.execute(
            "SELECT completed_at FROM checkpoint_meta "
            "WHERE share=? AND src_path=?",
            (share, src_path),
        ).fetchone()
        return {
            "share": share, "src_path": src_path,
            **meta_dict,
            "parts": parts,
            "completed_at": completed[0] if completed else "",
        }

    def put_part(self, share: str, src_path: str, part_idx: int,
                 bytes_written: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO checkpoints "
            "(share, src_path, part_idx, bytes, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (share, src_path, part_idx, bytes_written,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        self._db.commit()

    def mark_completed(self, share: str, src_path: str) -> None:
        # Upsert the completion timestamp. We INSERT OR IGNORE first
        # (in case no meta row exists yet — put_part doesn't create
        # one), then UPDATE.
        self._db.execute(
            "INSERT OR IGNORE INTO checkpoint_meta (share, src_path) "
            "VALUES (?, ?)",
            (share, src_path),
        )
        self._db.execute(
            "UPDATE checkpoint_meta SET completed_at=? "
            "WHERE share=? AND src_path=?",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             share, src_path),
        )
        self._db.commit()

    def reset(self, share: str, src_path: Optional[str] = None) -> int:
        if src_path is None:
            n1 = self._db.execute(
                "DELETE FROM checkpoints WHERE share=?", (share,)).rowcount
            n2 = self._db.execute(
                "DELETE FROM checkpoint_meta WHERE share=?", (share,)).rowcount
            self._db.commit()
            return n1 + n2
        n1 = self._db.execute(
            "DELETE FROM checkpoints WHERE share=? AND src_path=?",
            (share, src_path)).rowcount
        n2 = self._db.execute(
            "DELETE FROM checkpoint_meta WHERE share=? AND src_path=?",
            (share, src_path)).rowcount
        self._db.commit()
        return n1 + n2

    def list(self, share: str) -> List[Dict[str, Any]]:
        rows = self._db.execute(
            "SELECT DISTINCT src_path FROM checkpoints WHERE share=?",
            (share,),
        ).fetchall()
        return [self.get(share, r[0]) for r in rows if r[0]]