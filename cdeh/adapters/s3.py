"""S3-compatible adapter — works with AWS S3, MinIO, Aliyun OSS, Tencent COS,
Cloudflare R2, Backblaze B2, Ceph RGW, Wasabi, etc.

Single class, configured per-endpoint. The `endpoint_url` parameter is
the only difference between vendors; everything else is the same S3
API surface.
"""
from __future__ import annotations

from typing import Any, BinaryIO, Dict, Iterator, Optional, Tuple, Union

from .base import (
    AdapterError, AdapterNotFound, BaseAdapter, FileStat, _guess_ct,
)


class S3Adapter(BaseAdapter):
    """AWS S3 and any S3-API-compatible storage.

    Required: ``endpoint``, ``bucket``, ``access_key``, ``secret_key``.
    Optional: ``region`` (default "us-east-1"), ``use_ssl`` (default True
    if endpoint starts with https), ``multipart_threshold`` (default 8 MiB),
    ``multipart_chunksize`` (default 8 MiB), ``endpoint_url`` (alias for
    ``endpoint``).
    """
    kind = "s3"

    def __init__(self, endpoint: str, bucket: str, access_key: str,
                 secret_key: str, region: str = "us-east-1",
                 use_ssl: Optional[bool] = None,
                 multipart_threshold: int = 8 * 1024 * 1024,
                 multipart_chunksize: int = 8 * 1024 * 1024,
                 addressing_style: str = "auto"):
        # Lazy import — boto3 is heavy and not everyone needs S3
        import boto3
        from botocore.client import Config
        from botocore.exceptions import ClientError

        self._boto3 = boto3
        self._ClientError = ClientError
        self.bucket_name = bucket
        if use_ssl is None:
            use_ssl = endpoint.startswith("https://")
        self.endpoint = endpoint
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            use_ssl=use_ssl,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": addressing_style},
                retries={"max_attempts": 5, "mode": "standard"},
                max_pool_connections=20,
            ),
        )
        self._multipart_threshold = multipart_threshold
        self._multipart_chunksize = multipart_chunksize

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "S3Adapter":
        endpoint = cfg.get("endpoint") or cfg.get("endpoint_url")
        if not endpoint:
            raise AdapterError("S3Adapter requires config['endpoint']")
        return cls(
            endpoint=endpoint,
            bucket=cfg["bucket"],
            access_key=cfg.get("access_key", ""),
            secret_key=cfg.get("secret_key", ""),
            region=cfg.get("region", "us-east-1"),
            use_ssl=cfg.get("use_ssl"),
        )

    def ping(self) -> Dict[str, Any]:
        try:
            self._s3.head_bucket(Bucket=self.bucket_name)
        except self._ClientError as e:
            code = e.response.get("Error", {}).get("Code") if hasattr(e, "response") else None
            if code in ("404", "NoSuchBucket", "NotFound"):
                raise AdapterNotFound(f"bucket not found: {self.bucket_name}")
            raise
        return {
            "endpoint": self.endpoint,
            "bucket": self.bucket_name,
            "kind": "s3",
            "vendor": _vendor_hint(self.endpoint),
        }

    def stat(self, path: str) -> FileStat:
        key = self._key(path)
        try:
            r = self._s3.head_object(Bucket=self.bucket_name, Key=key)
        except self._ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise AdapterNotFound(f"object not found: {key}")
            raise
        return FileStat(
            path="/" + key,
            size=int(r.get("ContentLength", 0)),
            etag=(r.get("ETag") or "").strip('"'),
            mtime_ns=_iso_to_ns(r.get("LastModified")),
            content_type=r.get("ContentType", "") or _guess_ct_for_key(key),
            metadata=r.get("Metadata") or {},
        )

    def list(self, prefix: str = "", recursive: bool = True) -> Iterator[FileStat]:
        kwargs = {"Bucket": self.bucket_name}
        if prefix:
            kwargs["Prefix"] = self._key(prefix)
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(**kwargs):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # S3 "directories" are zero-byte keys ending with /
                if key.endswith("/"):
                    continue
                yield FileStat(
                    path="/" + key,
                    size=int(obj.get("Size", 0)),
                    etag=(obj.get("ETag") or "").strip('"'),
                    mtime_ns=_iso_to_ns(obj.get("LastModified")),
                    content_type=_guess_ct_for_key(key),
                )

    def get(self, path: str, range_: Optional[Tuple[int, int]] = None) -> bytes:
        key = self._key(path)
        kwargs = {"Bucket": self.bucket_name, "Key": key}
        if range_ is not None:
            start, end = range_
            kwargs["Range"] = f"bytes={start}-{end}"
        try:
            r = self._s3.get_object(**kwargs)
        except self._ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise AdapterNotFound(f"object not found: {key}")
            raise
        return r["Body"].read()

    def put(self, path: str, data: Union[bytes, BinaryIO],
           content_type: str = "",
           metadata: Optional[Dict[str, str]] = None) -> FileStat:
        key = self._key(path)
        kwargs = {
            "Bucket": self.bucket_name,
            "Key": key,
            "ContentType": content_type or _guess_ct_for_key(key),
        }
        if metadata:
            kwargs["Metadata"] = metadata
        if isinstance(data, (bytes, bytearray)):
            self._s3.put_object(Body=bytes(data), **kwargs)
        else:
            # single-shot upload for streaming — for >threshold use multipart
            data.seek(0, 2)  # seek to end
            size = data.tell()
            data.seek(0)
            if size >= self._multipart_threshold:
                self._multipart_put(data, size, **kwargs)
            else:
                self._s3.put_object(Body=data.read(), **kwargs)
        return self.stat(path)

    def delete(self, path: str) -> None:
        try:
            self._s3.delete_object(Bucket=self.bucket_name, Key=self._key(path))
        except self._ClientError:
            pass

    # ─── multipart upload (large files) ─────────────────────────────
    def _multipart_put(self, fp, size, **kwargs):
        from boto3.s3.transfer import TransferConfig
        cfg = TransferConfig(
            multipart_threshold=self._multipart_threshold,
            multipart_chunksize=self._multipart_chunksize,
            use_threads=True,
        )
        self._s3.upload_fileobj(
            Fileobj=fp, Bucket=kwargs["Bucket"], Key=kwargs["Key"],
            ExtraArgs={k: v for k, v in kwargs.items() if k not in ("Bucket", "Key")},
            Config=cfg,
        )

    # ─── helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _key(path: str) -> str:
        return path.lstrip("/")


# ─── module-level helpers (import-time only) ─────────────────────────
def _iso_to_ns(dt) -> int:
    if dt is None:
        return 0
    if hasattr(dt, "timestamp"):
        return int(dt.timestamp() * 1_000_000_000)
    return 0


def _guess_ct_for_key(key: str) -> str:
    from pathlib import PurePosixPath
    from .base import _CT_MAP
    return _CT_MAP.get(PurePosixPath(key).suffix.lower(), "application/octet-stream")


def _vendor_hint(endpoint: str) -> str:
    e = endpoint.lower()
    if "amazonaws" in e or "aws.com" in e:
        return "AWS S3"
    if "aliyuncs" in e:
        return "Aliyun OSS"
    if "myqcloud" in e or "tencentyun" in e:
        return "Tencent COS"
    if "googleapis" in e:
        return "Google GCS (S3 compat)"
    if "azure" in e:
        return "Azure Blob (S3 compat)"
    if "min.io" in e or ":9000" in e or ":9001" in e:
        return "MinIO / S3-compatible"
    return "S3-compatible"