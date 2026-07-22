"""Adapter subpackage — concrete storage backends and the registry."""
from .base import BaseAdapter, AdapterError, AdapterNotFound
from . import local, s3, azure_blob, sftp, mysql

registry: dict = {}


def register(adapter_cls):
    """Decorator to register an adapter class by its `kind` attribute."""
    registry[adapter_cls.kind] = adapter_cls
    return adapter_cls


def get(kind: str) -> "BaseAdapter":
    if kind not in registry:
        raise AdapterNotFound(f"no adapter registered for kind='{kind}'. "
                               f"Available: {sorted(registry)}")
    return registry[kind]  # class, not instance — call .from_config() on it


# Manual registration (avoids the chicken-and-egg between decorator and
# the registry dict — keeps the import order simple).
register(local.LocalAdapter)
register(s3.S3Adapter)
register(azure_blob.AzureBlobAdapter)
register(sftp.SFTPAdapter)
register(mysql.MySQLAdapter)

__all__ = ["BaseAdapter", "AdapterError", "AdapterNotFound",
           "registry", "register", "get"]