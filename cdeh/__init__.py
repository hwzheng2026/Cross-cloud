"""C-DEH — Cross-Cloud Data Exchange Hub.

A plugin-based, secure, high-performance data sharing component for
industrial-internet cross-cloud environments.

Public API:

    from cdeh import CDEHClient

    client = CDEHClient("http://127.0.0.1:8080")
    client.adapters.register(...)
    client.share.create(...)
    client.share.run(...)

See README.md for the full quick start.
"""
__version__ = "0.1.0"

from .core.gateway import CDEHClient
from .core.catalog import DataCatalog
from .core.transfer import TransferEngine
from .core.auth import RBAC
from .core.policy import PolicyEngine
from .core.audit import AuditLog
from .core.cache import TransferCache
from .adapters import registry
from .adapters.base import BaseAdapter
from . import adapters
from . import transformers

__all__ = [
    "CDEHClient",
    "DataCatalog",
    "TransferEngine",
    "RBAC",
    "PolicyEngine",
    "AuditLog",
    "TransferCache",
    "BaseAdapter",
    "registry",
    "adapters",
    "transformers",
]