"""Codec transformer — format conversion: CSV ↔ JSON-lines ↔ Parquet.

Parquet support is optional (depends on `pyarrow`); the transformer
gracefully degrades to "passthrough" if pyarrow isn't installed.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict

from .base import BaseTransformer, TransformerError


class CodecTransformer(BaseTransformer):
    kind = "codec"

    def __init__(self, from_format: str, to_format: str):
        self.from_format = from_format.lower()
        self.to_format = to_format.lower()
        if self.from_format not in ("csv", "jsonl", "json", "parquet"):
            raise TransformerError(f"unsupported from_format: {from_format}")
        if self.to_format not in ("csv", "jsonl", "json", "parquet"):
            raise TransformerError(f"unsupported to_format: {to_format}")
        if "parquet" in (self.from_format, self.to_format):
            try:
                import pyarrow  # noqa: F401
                import pyarrow.parquet as pq
                self._pq = pq
            except ImportError as e:
                raise TransformerError(
                    "Parquet codec requires `pip install pyarrow`"
                ) from e

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "CodecTransformer":
        try:
            return cls(from_format=cfg["from"], to_format=cfg["to"])
        except KeyError as e:
            raise TransformerError(
                f"CodecTransformer requires config['from'] and config['to']: {e}"
            )

    def _to_records(self, data: bytes) -> list:
        if self.from_format == "csv":
            return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
        if self.from_format in ("jsonl", "json"):
            return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]
        if self.from_format == "parquet":
            import io as _io
            return self._pq.read_table(_io.BytesIO(data)).to_pylist()
        return []

    def _from_records(self, records: list) -> bytes:
        if self.to_format == "csv":
            if not records:
                return b""
            buf = io.StringIO()
            wr = csv.DictWriter(buf, fieldnames=list(records[0].keys()))
            wr.writeheader()
            wr.writerows(records)
            return buf.getvalue().encode("utf-8")
        if self.to_format in ("jsonl", "json"):
            return ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode("utf-8")
        if self.to_format == "parquet":
            import pyarrow as pa
            if not records:
                return b""
            tbl = pa.Table.from_pylist(records)
            import io as _io
            buf = _io.BytesIO()
            self._pq.write_table(tbl, buf, compression="snappy")
            return buf.getvalue()
        return b""

    def transform(self, data: bytes, params: Dict[str, Any]) -> bytes:
        if self.from_format == self.to_format:
            return data
        return self._from_records(self._to_records(data))