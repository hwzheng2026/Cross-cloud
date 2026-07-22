"""Azure Blob Storage adapter — uses azure-storage-blob SDK.

A separate adapter from S3 (not just S3-compat) because Azure has
slightly different semantics: block blobs vs append blobs, snapshot
operations, and SAS tokens (no HMAC-style access keys). For shops
heavily invested in Azure, this is the right path even though Azure
also offers an S3-compat surface.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any, BinaryIO, Dict, Iterator, Optional, Tuple, Union

from .base import AdapterError, AdapterNotFound, BaseAdapter, FileStat


class AzureBlobAdapter(BaseAdapter):
    kind = "azure_blob"

    def __init__(self, account_name: str, account_key: str, container: str,
                 endpoint_suffix: str = "core.windows.net",
                 max_single_put_size: int = 64 * 1024 * 1024,
                 max_block_size: int = 4 * 1024 * 1024):
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError as e:
            raise AdapterError(
                "AzureBlobAdapter requires `pip install azure-storage-blob`"
            ) from e
        self.account_name = account_name
        self.container = container
        self.endpoint_suffix = endpoint_suffix
        self.max_single_put_size = max_single_put_size
        self.max_block_size = max_block_size
        self._cred = self._build_credential(account_name, account_key)
        url = f"https://{account_name}.blob.{endpoint_suffix}"
        self._client = BlobServiceClient(account_url=url, credential=self._cred)
        self._container = self._client.get_container_client(container)

    @staticmethod
    def _build_credential(name: str, key: str):
        from azure.storage.blob import (
            BlobServiceClient,
        )
        try:
            from azure.core.credentials import AzureNamedKeyCredential
            return AzureNamedKeyCredential(name, key)
        except ImportError:
            from azure.storage.blob import (
                BlobServiceClient,
            )
            # Older API
            return name, key

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "AzureBlobAdapter":
        for k in ("account_name", "account_key", "container"):
            if k not in cfg:
                raise AdapterError(f"AzureBlobAdapter requires config['{k}']")
        return cls(
            account_name=cfg["account_name"],
            account_key=cfg["account_key"],
            container=cfg["container"],
            endpoint_suffix=cfg.get("endpoint_suffix", "core.windows.net"),
        )

    def ping(self) -> Dict[str, Any]:
        try:
            exists = self._container.exists()
        except Exception as e:
            raise AdapterError(f"ping failed: {e}") from e
        if not exists:
            raise AdapterNotFound(f"container not found: {self.container}")
        return {"kind": "azure_blob", "account": self.account_name, "container": self.container}

    def stat(self, path: str) -> FileStat:
        blob_name = self._key(path)
        try:
            props = self._container.get_blob_client(blob_name).get_blob_properties()
        except Exception as e:
            code = _azure_error_code(e)
            if code in ("BlobNotFound", "404"):
                raise AdapterNotFound(f"blob not found: {blob_name}")
            raise AdapterError(f"stat failed: {e}") from e
        return FileStat(
            path="/" + blob_name,
            size=props.size,
            etag=(props.etag or "").strip('"'),
            mtime_ns=int(props.last_modified.timestamp() * 1_000_000_000) if props.last_modified else 0,
            content_type=props.content_settings.content_type or "",
            metadata=dict(props.metadata or {}),
        )

    def list(self, prefix: str = "", recursive: bool = True) -> Iterator[FileStat]:
        name_starts_with = self._key(prefix) if prefix else None
        # Azure Blob's "delimiter" controls hierarchy. We use "" (no
        # delimiter) to flatten — `recursive` is informational here.
        for blob in self._container.list_blobs(name_starts_with=name_starts_with):
            if blob.name.endswith("/"):
                continue
            yield FileStat(
                path="/" + blob.name,
                size=blob.size or 0,
                etag=(blob.etag or "").strip('"'),
                mtime_ns=int(blob.last_modified.timestamp() * 1_000_000_000) if blob.last_modified else 0,
                content_type=blob.content_settings.content_type if blob.content_settings else "",
            )

    def get(self, path: str, range_: Optional[Tuple[int, int]] = None) -> bytes:
        blob_name = self._key(path)
        client = self._container.get_blob_client(blob_name)
        if range_ is None:
            stream = client.download_blob()
            return stream.readall()
        start, end = range_
        stream = client.download_blob(offset=start, length=end - start + 1)
        return stream.readall()

    def put(self, path: str, data: Union[bytes, BinaryIO],
           content_type: str = "",
           metadata: Optional[Dict[str, str]] = None) -> FileStat:
        blob_name = self._key(path)
        client = self._container.get_blob_client(blob_name)
        if isinstance(data, (bytes, bytearray)):
            blob_bytes = bytes(data)
        else:
            data.seek(0, 2)
            blob_bytes = data.read()
        kwargs = {"overwrite": True}
        if content_type:
            kwargs["content_type"] = content_type
        if metadata:
            kwargs["metadata"] = metadata
        client.upload_blob(blob_bytes, **kwargs)
        return self.stat(path)

    def delete(self, path: str) -> None:
        try:
            self._container.get_blob_client(self._key(path)).delete_blob()
        except Exception:
            pass

    @staticmethod
    def _key(path: str) -> str:
        return path.lstrip("/")


def _azure_error_code(exc) -> Optional[str]:
    """Extract a stable error code string from an azure.core.exceptions.AzureError."""
    err = getattr(exc, "error", None)
    if err:
        return getattr(err, "code", None) or (err.get("code") if isinstance(err, dict) else None)
    return getattr(exc, "status_code", None) and str(getattr(exc, "status_code", None))