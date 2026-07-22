"""MySQL adapter — share a table (or the result of a SELECT) across
clouds as a CSV/Parquet blob.

This is a "tabular" adapter — it implements the same BaseAdapter
interface but `get()` / `put()` operate on a logical table or query
result, and the underlying transport serializes to a columnar file.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, BinaryIO, Dict, Iterator, List, Optional, Tuple, Union

from .base import (
    AdapterError, AdapterNotFound, BaseAdapter, FileStat, _guess_ct,
)


class MySQLAdapter(BaseAdapter):
    """Share a MySQL table or query result as a virtual object.

    Path semantics:
      `/<schema>/<table>`            — full table
      `/<schema>/<table>/<partition>` — single partition
      `?sql=<URL-encoded query>`     — ad-hoc query result

    All paths are read-only by default. Writes require `mode="write"` in
    the config (and `replace=...` to clear before load).
    """
    kind = "mysql"

    def __init__(self, host: str, port: int, user: str, password: str,
                 database: str = "", default_fetch_size: int = 10000,
                 write_mode: str = "read"):
        try:
            import pymysql
        except ImportError as e:
            raise AdapterError(
                "MySQLAdapter requires `pip install pymysql`"
            ) from e
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.default_fetch_size = default_fetch_size
        self.write_mode = write_mode  # "read" or "replace"
        self._pymysql = pymysql

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "MySQLAdapter":
        for k in ("host", "user", "password"):
            if k not in cfg:
                raise AdapterError(f"MySQLAdapter requires config['{k}']")
        return cls(
            host=cfg["host"], port=cfg.get("port", 3306),
            user=cfg["user"], password=cfg["password"],
            database=cfg.get("database", ""),
        )

    def _conn(self):
        return self._pymysql.connect(
            host=self.host, port=self.port, user=self.user,
            password=self.password, database=self.database or None,
            cursorclass=self._pymysql.cursors.SSDictCursor,
            charset="utf8mb4",
        )

    def ping(self) -> Dict[str, Any]:
        try:
            c = self._conn()
            cur = c.cursor()
            cur.execute("SELECT VERSION() AS v")
            row = cur.fetchone()
            cur.close(); c.close()
        except Exception as e:
            raise AdapterError(f"ping failed: {e}") from e
        v = (row or {}).get("v", "")
        return {"kind": "mysql", "host": self.host, "version": v}

    @staticmethod
    def _parse_path(path: str):
        """`/db/table` → ('db', 'table', None)  or `?sql=...` → query mode."""
        p = path.lstrip("/")
        if p.startswith("?sql="):
            from urllib.parse import unquote
            return ("__query__", unquote(p[5:]), None)
        parts = p.split("/", 2)
        if len(parts) < 2:
            raise AdapterError(f"invalid MySQL path: {path} (need /db/table)")
        return (parts[0], parts[1], parts[2] if len(parts) > 2 else None)

    def stat(self, path: str) -> FileStat:
        db, table_or_query, _ = self._parse_path(path)
        c = self._conn()
        try:
            cur = c.cursor()
            if db == "__query__":
                # For ad-hoc queries, stat is approximate
                cur.execute(f"EXPLAIN {table_or_query}")
                rows = list(cur.fetchall())
                cols = len(rows[0]) if rows else 0
                # rough row-count estimate
                return FileStat(
                    path=path if path.startswith("/") else "/" + path,
                    size=cols * 100,  # placeholder
                    etag="", mtime_ns=0,
                    content_type="text/csv",
                )
            # Table: get row count + create time
            cur.execute(
                "SELECT TABLE_ROWS, UPDATE_TIME, CREATE_TIME, DATA_LENGTH "
                "FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (db, table_or_query),
            )
            row = cur.fetchone()
            if not row:
                raise AdapterNotFound(f"table not found: {db}.{table_or_query}")
            size = int(row.get("DATA_LENGTH") or 0)
            mtime = row.get("UPDATE_TIME") or row.get("CREATE_TIME")
            mtime_ns = int(mtime.timestamp() * 1_000_000_000) if mtime else 0
            return FileStat(
                path="/" + f"{db}/{table_or_query}",
                size=size, etag=f"mysql-{mtime_ns}-{row.get('TABLE_ROWS', 0)}",
                mtime_ns=mtime_ns, content_type="text/csv",
            )
        finally:
            c.close()

    def list(self, prefix: str = "", recursive: bool = True) -> Iterator[FileStat]:
        """List tables in a database (or databases in the instance)."""
        c = self._conn()
        try:
            cur = c.cursor()
            if not prefix or prefix == "/":
                # list databases
                cur.execute("SHOW DATABASES")
                for (db,) in cur.fetchall():
                    if db in ("information_schema", "mysql", "performance_schema", "sys"):
                        continue
                    # synthesise a stat from the DB name
                    yield FileStat(
                        path=f"/{db}", size=0, etag="", mtime_ns=0,
                        content_type="text/csv",
                    )
                return
            db, _, _ = self._parse_path(prefix)
            cur.execute(
                "SELECT TABLE_NAME, UPDATE_TIME, CREATE_TIME, DATA_LENGTH "
                "FROM information_schema.tables WHERE TABLE_SCHEMA=%s",
                (db,),
            )
            for row in cur.fetchall():
                tname = row.get("TABLE_NAME")
                mtime = row.get("UPDATE_TIME") or row.get("CREATE_TIME")
                mtime_ns = int(mtime.timestamp() * 1_000_000_000) if mtime else 0
                yield FileStat(
                    path=f"/{db}/{tname}",
                    size=int(row.get("DATA_LENGTH") or 0),
                    etag=f"mysql-{mtime_ns}-{tname}",
                    mtime_ns=mtime_ns, content_type="text/csv",
                )
        finally:
            c.close()

    def get(self, path: str, range_: Optional[Tuple[int, int]] = None) -> bytes:
        """Export a table or query to CSV. `range_` lets you slice rows
        (row numbers, not bytes)."""
        db, table_or_query, _ = self._parse_path(path)
        c = self._conn()
        try:
            cur = c.cursor()
            sql = table_or_query if db == "__query__" else \
                  f"SELECT * FROM `{db}`.`{table_or_query}`"
            cur.execute(sql)
            buf = io.StringIO()
            writer = None
            row_count = 0
            start, end = range_ if range_ else (0, None)
            while True:
                rows = cur.fetchmany(self.default_fetch_size)
                if not rows:
                    break
                for i, row in enumerate(rows):
                    actual_idx = row_count + i
                    if actual_idx < start:
                        continue
                    if end is not None and actual_idx > end:
                        break
                    if writer is None:
                        writer = csv.DictWriter(buf, fieldnames=list(row.keys()))
                        writer.writeheader()
                    writer.writerow(row)
                row_count += len(rows)
                if end is not None and row_count > end:
                    break
            return buf.getvalue().encode("utf-8")
        finally:
            c.close()

    def put(self, path: str, data: Union[bytes, BinaryIO],
           content_type: str = "",
           metadata: Optional[Dict[str, str]] = None) -> FileStat:
        if self.write_mode != "replace":
            raise AdapterError(
                "MySQLAdapter is read-only by default; set write_mode='replace' "
                "in config to allow CSV imports (TRUNCATE + LOAD DATA LOCAL INFILE)"
            )
        db, table, _ = self._parse_path(path)
        c = self._conn()
        try:
            cur = c.cursor()
            cur.execute(f"TRUNCATE TABLE `{db}`.`{table}`")
            text = data if isinstance(data, (bytes, bytearray)) else data.read().decode("utf-8")
            if isinstance(text, bytes):
                text = text.decode("utf-8")
            reader = csv.DictReader(io.StringIO(text))
            cols = reader.fieldnames or []
            if not cols:
                return self.stat(path)
            placeholders = ",".join(["%s"] * len(cols))
            col_list = ",".join(f"`{c_}`" for c_ in cols)
            cur.executemany(
                f"INSERT INTO `{db}`.`{table}` ({col_list}) VALUES ({placeholders})",
                [tuple(r.get(c) for c in cols) for r in reader],
            )
            c.commit()
            return self.stat(path)
        finally:
            c.close()

    def delete(self, path: str) -> None:
        # No-op for MySQL; deleting a table is a destructive admin op
        # and we don't expose it through the adapter. Use the underlying
        # client directly if you really want it.
        raise AdapterNotSupported(
            "MySQLAdapter.delete is not supported — manage table lifecycle "
            "via DDL outside C-DEH"
        )