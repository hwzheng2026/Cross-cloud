"""CDEHClient — top-level façade.

Two modes:
  1. Embedded (in-process): used by the CLI and examples. Pass
     components (or let them default) and call methods directly.
  2. HTTP client: when you have a `cdeh-server` running somewhere.
     `CDEHClient("http://host:8080")` becomes a thin REST wrapper.

The public surface is the same either way.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib.request
import urllib.error

from .audit import AuditLog
from .auth import RBAC
from .cache import TransferCache
from .catalog import DataAsset, DataCatalog
from .policy import Policy, PolicyContext, PolicyEngine
from .transfer import Share, TransferEngine, TransferResult


# ─── In-process client (the common case) ───────────────────────────
class CDEHClient:
    """Embedded-mode C-DEH client. For HTTP-mode, see `CDEHClientHTTP` below."""

    def __init__(self, base_url: Optional[str] = None,
                 catalog_path: Optional[str] = None,
                 rbac_path: Optional[str] = None,
                 audit_path: Optional[str] = None,
                 config_dir: Optional[str] = None):
        self.base_url = base_url
        self.config_dir = Path(config_dir or os.path.expanduser("~/.cdeh"))
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.catalog = DataCatalog(catalog_path or str(self.config_dir / "catalog.json"))
        self.rbac = RBAC(rbac_path or str(self.config_dir / "users.json"))
        self.audit = AuditLog(audit_path or str(self.config_dir / "audit.log"))
        self.policy = PolicyEngine()
        self.cache = TransferCache()
        self.engine = TransferEngine(
            catalog=self.catalog, rbac=self.rbac,
            audit=self.audit, policy_engine=self.policy, cache=self.cache,
        )
        # Wire up the engine's adapter-config lookup so run_async /
        # run_batch can resolve configs by adapter name. The HTTP
        # server overrides this with its own dispatch.
        self.engine._get_adapter_config = self.get_adapter_config  # type: ignore[attr-defined]
        # Adapter configs are stored at ~/.cdeh/adapters.json (separate
        # from catalog because they contain secrets)
        self._adapters_file = self.config_dir / "adapters.json"
        self._adapters: Dict[str, Dict[str, Any]] = self._load_adapters()

    # ─── adapters ──────────────────────────────────────────────────
    @property
    def adapters(self):
        return _AdaptersAPI(self)

    def list_adapters(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._adapters)

    def register_adapter(self, name: str, kind: str, **config) -> None:
        from ..adapters import get as _get_adapter
        # Force-load the adapter module so the registry is populated.
        # The lazy loader raises a friendly ImportError if the cloud
        # SDK isn't installed.
        try:
            _get_adapter(kind)
        except Exception as e:
            raise ValueError(
                f"unknown adapter kind: {kind}. {e}"
            ) from e
        cfg = {"kind": kind, **config}
        # Sanity-check the config builds an adapter
        _get_adapter(kind).from_config({k: v for k, v in cfg.items() if k != "kind"})
        self._adapters[name] = cfg
        self._save_adapters()
        self.audit.append(user="system", action="adapter.register",
                          asset=name, extra={"kind": kind})

    def remove_adapter(self, name: str) -> bool:
        if name in self._adapters:
            del self._adapters[name]
            self._save_adapters()
            self.audit.append(user="system", action="adapter.remove", asset=name)
            return True
        return False

    def get_adapter_config(self, name: str) -> Dict[str, Any]:
        if name not in self._adapters:
            raise KeyError(f"adapter not registered: {name}")
        return self._adapters[name]

    def _load_adapters(self) -> Dict[str, Dict[str, Any]]:
        if not self._adapters_file.exists():
            return {}
        try:
            return json.loads(self._adapters_file.read_text())
        except json.JSONDecodeError:
            return {}

    def _save_adapters(self) -> None:
        tmp = self._adapters_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._adapters, indent=2))
        tmp.replace(self._adapters_file)

    # ─── shares ────────────────────────────────────────────────────
    @property
    def share(self):
        return _SharesAPI(self)

    def define_share(self, share: Share) -> Share:
        self.engine.save_share(share)
        self.audit.append(user="system", action="share.define",
                          asset=share.name,
                          extra=share.to_dict())
        return share

    def run_share(self, name: str, user: str = "system") -> TransferResult:
        s = self.engine.load_share(name)
        if s is None:
            raise KeyError(f"share not found: {name}")
        src = self.get_adapter_config(s.src_adapter)
        dst = self.get_adapter_config(s.dst_adapter)
        result = self.engine.run(s, user=user, src_config=src, dst_config=dst)
        # Catalog metadata update
        try:
            asset = self.catalog.get(name)
            if asset is None:
                asset = DataAsset(
                    name=name, adapter=s.dst_adapter, path=s.dst_path,
                    description=s.description,
                )
            asset.last_sync = result.finished_at and _iso_now()
            self.catalog.register(asset)
        except Exception:
            pass
        return result

    def run_share_async(self, name: str, user: str = "system"):
        """Background-execute a share. Returns a `concurrent.futures.Future`
        that resolves to a `TransferResult`."""
        from concurrent.futures import Future
        s = self.engine.load_share(name)
        if s is None:
            raise KeyError(f"share not found: {name}")
        src = self.get_adapter_config(s.src_adapter)
        dst = self.get_adapter_config(s.dst_adapter)
        # Pass src/dst configs to run_async so the worker thread doesn't
        # need to re-resolve them (and the run_async signature
        # defaults src/dst_config to None, which would error).
        return self.engine.run_async(s, user=user, src_config=src, dst_config=dst)

    def run_share_batch(self, names, users=None, max_workers=4, fail_fast=False):
        """Run several shares concurrently. Returns a `BatchResult`."""
        from .transfer import BatchResult
        shares = []
        for n in names:
            s = self.engine.load_share(n)
            if s is None:
                raise KeyError(f"share not found: {n}")
            shares.append(s)
        return self.engine.run_batch(shares, users=users,
                                      max_workers=max_workers,
                                      fail_fast=fail_fast)

    def shutdown(self, wait: bool = True) -> None:
        """Stop background executors. Call before process exit."""
        self.engine.shutdown(wait=wait)

    # ─── audit ─────────────────────────────────────────────────────
    def audit_chain_status(self) -> Dict[str, Any]:
        return self.audit.verify_chain()

    def audit_tail(self, n: int = 20) -> List[Dict[str, Any]]:
        return self.audit.tail(n)

    # ─── policy ────────────────────────────────────────────────────
    def add_policy(self, policy: Policy) -> None:
        self.policy.add_policy(policy)


# ─── HTTP-mode client (thin wrapper) ───────────────────────────────
class CDEHClientHTTP:
    """Talks to a running `cdeh-server` over HTTP. Same surface as
    `CDEHClient` but every call becomes an HTTP request."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._http = _Http(base_url)

    @property
    def adapters(self):
        return _AdaptersAPIRemote(self)

    @property
    def share(self):
        return _SharesAPIRemote(self)


# ─── internal helper mixins (small typed namespaces) ────────────────
class _AdaptersAPI:
    def __init__(self, client: CDEHClient):
        self._c = client

    def list(self): return self._c.list_adapters()
    def register(self, name, kind, **config): self._c.register_adapter(name, kind, **config)
    def remove(self, name): return self._c.remove_adapter(name)
    def get(self, name): return self._c.get_adapter_config(name)


class _SharesAPI:
    def __init__(self, client: CDEHClient):
        self._c = client

    def list(self): return self._c.engine.list_shares()
    def create(self, name, source, dest, transform=None, policy="default",
               incremental="etag", parallelism=4, description="",
               filters=None, resumable=False, retry=None):
        src_name, _, src_path = source.partition(":")
        dst_name, _, dst_path = dest.partition(":")
        s = Share(
            name=name, src_adapter=src_name, src_path="/" + src_path.lstrip("/"),
            dst_adapter=dst_name, dst_path="/" + dst_path.lstrip("/"),
            transforms=transform or [],
            policy=policy, incremental=incremental,
            parallelism=parallelism, description=description,
            filters=filters or [],
            resumable=resumable, retry=retry or {},
        )
        return self._c.define_share(s)

    def create_batch(self, shares: list):
        """Bulk-create shares. `shares` is a list of dicts with the
        same shape as `create()`'s kwargs. Returns the list of
        persisted `Share` objects."""
        out = []
        for s in shares:
            out.append(self.create(**s))
        return out

    def get(self, name): return self._c.engine.load_share(name)
    def delete(self, name): return self._c.engine.delete_share(name)
    def run(self, name, user="system"):
        """Synchronous share run. Returns a TransferResult dict."""
        return self._c.run_share(name, user=user).to_dict()
    def run_async(self, name, user="system"):
        """Background share run. Returns a `concurrent.futures.Future`;
        call `.result()` to block, or `.add_done_callback(fn)` to
        handle the result asynchronously."""
        return self._c.run_share_async(name, user=user)
    def run_batch(self, names, users=None, max_workers=4, fail_fast=False):
        """Run several shares concurrently. Returns a `BatchResult`
        dict with per-share outcomes + aggregate success count."""
        return self._c.run_share_batch(
            names, users=users, max_workers=max_workers, fail_fast=fail_fast,
        ).to_dict()


class _Http:
    def __init__(self, base_url: str):
        self.base = base_url

    def get(self, path): return self._call("GET", path)
    def post(self, path, data): return self._call("POST", path, data)
    def delete(self, path): return self._call("DELETE", path)

    def _call(self, method, path, data=None):
        url = f"{self.base}{path}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": "application/json"} if body else {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}") from e


class _AdaptersAPIRemote:
    def __init__(self, client: CDEHClientHTTP):
        self._c = client

    def list(self): return self._c._http.get("/adapters")
    def register(self, name, kind, **config):
        self._c._http.post("/adapters", {"name": name, "kind": kind, **config})
    def remove(self, name): self._c._http.delete(f"/adapters/{name}")
    def get(self, name): return self._c._http.get(f"/adapters/{name}")


class _SharesAPIRemote:
    def __init__(self, client: CDEHClientHTTP):
        self._c = client

    def list(self): return self._c._http.get("/shares")
    def create(self, name, source, dest, transform=None, policy="default",
               incremental="etag", parallelism=4, description=""):
        self._c._http.post("/shares", {
            "name": name, "source": source, "dest": dest,
            "transform": transform or [], "policy": policy,
            "incremental": incremental, "parallelism": parallelism,
            "description": description,
        })
    def get(self, name): return self._c._http.get(f"/shares/{name}")
    def delete(self, name): return self._c._http.delete(f"/shares/{name}")
    def run(self, name, user="system"):
        return self._c._http.post(f"/shares/{name}/run", {"user": user})


def _iso_now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()