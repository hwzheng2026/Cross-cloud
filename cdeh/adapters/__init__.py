"""Adapter subpackage — concrete storage backends and the registry.

Adapter modules are imported lazily (on first `register_adapter` call
for that kind) so that `pip install cdeh` works without boto3,
azure-storage-blob, paramiko, or pymysql installed. The user only
needs the cloud SDKs for the adapters they actually register.

`local` is the only adapter that has no external SDK dependency, so
it's always loaded eagerly.
"""
from .base import BaseAdapter, AdapterError, AdapterNotFound

registry: dict = {}


def register(adapter_cls):
    """Decorator to register an adapter class by its `kind` attribute."""
    registry[adapter_cls.kind] = adapter_cls
    return adapter_cls


# Eager-load `local` so the registry is populated for the most common
# adapter out of the box. Other adapters are loaded lazily via `get()`.
from . import local as _local_mod
register(_local_mod.LocalAdapter)


def get(kind: str) -> "BaseAdapter":
    """Look up an adapter class by `kind`. If the kind isn't loaded yet,
    import the corresponding module (which triggers its own registration).

    Returns the *class* (call `.from_config(cfg)` on it to construct an
    instance)."""
    if kind not in registry:
        _try_import(kind)
    if kind not in registry:
        raise AdapterNotFound(
            f"no adapter registered for kind={kind!r}. "
            f"Available: {sorted(registry)}. "
            f"Install the relevant extras: "
            f"pip install cdeh[s3|azure|sftp|mysql|all]"
        )
    return registry[kind]


def _try_import(kind: str) -> None:
    """Import the adapter module that provides `kind`. Side-effects
    populate `registry` (modules register themselves in their top-level
    code). Raises ImportError with a friendly message if the cloud SDK
    isn't installed."""
    import importlib
    mod_name = {
        "local":       "cdeh.adapters.local",
        "s3":          "cdeh.adapters.s3",
        "azure_blob":  "cdeh.adapters.azure_blob",
        "sftp":        "cdeh.adapters.sftp",
        "mysql":       "cdeh.adapters.mysql",
    }.get(kind)
    if mod_name is None:
        return
    try:
        importlib.import_module(mod_name)
    except ImportError as e:
        # Wrap the error so the user sees "you need pip install cdeh[s3]"
        # instead of "No module named 'boto3'".
        extras_hint = {
            "s3":         "cdeh[s3]",
            "azure_blob": "cdeh[azure]",
            "sftp":       "cdeh[sftp]",
            "mysql":      "cdeh[mysql]",
        }.get(kind, "cdeh[all]")
        raise ImportError(
            f"adapter {kind!r} requires optional cloud SDK. "
            f"Install with `pip install {extras_hint}`. "
            f"(Original error: {e})"
        ) from e


__all__ = ["BaseAdapter", "AdapterError", "AdapterNotFound",
           "registry", "register", "get"]