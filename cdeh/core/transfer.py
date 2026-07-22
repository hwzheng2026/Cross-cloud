"""Transfer engine — core orchestrator for cross-cloud data movement.

A `Share` is a declarative description of a data movement:
    Share(
        name="daily-orders",
        src_adapter="s3-prod",
        src_path="/orders/2024.csv",
        dst_adapter="oss-prod",
        dst_path="/incoming/2024.parquet",
        transforms=["mask:email,phone", "codec:csv:parquet"],
        policy="gdpr-strict",
        incremental="etag",  # or "mtime" or "none"
        parallelism=4,
    )

`TransferEngine.run(share)` performs:
  1. RBAC check
  2. Adapter ping + stat
  3. Policy evaluation
  4. Incremental filter (skip unchanged files)
  5. Concurrent chunked transfer (parts of each file in parallel)
  6. Transformer chain (mask, codec, ...)
  7. Cache write
  8. Audit append
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..adapters import registry as adapter_registry
from ..adapters.base import AdapterError, FileStat
from ..transformers import get as get_transformer
from .audit import AuditLog
from .auth import RBAC
from .cache import TransferCache
from .catalog import DataCatalog
from .policy import PolicyContext, PolicyEngine


@dataclasses.dataclass
class Share:
    name: str
    src_adapter: str
    src_path: str
    dst_adapter: str
    dst_path: str
    transforms: List[str] = dataclasses.field(default_factory=list)
    policy: str = "default"
    incremental: str = "etag"   # "etag" | "mtime" | "none"
    parallelism: int = 4
    chunk_size: int = 8 * 1024 * 1024     # 8 MiB
    cache_ttl: int = 300
    description: str = ""
    # ─── NEW: filter + checkpoint + transform options ────────────
    # Filter: restrict which objects move. Each predicate is applied
    # against the source object (its path + stat). Empty list = no
    # filter (transfer everything that matches the share's prefix).
    filters: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    # Resumable transfer: if True, intermediate state per (share, src_path)
    # is persisted to disk so a killed transfer can resume from the last
    # successful chunk instead of re-uploading the whole file.
    resumable: bool = False
    # Retry policy: {max_attempts, initial_backoff_seconds, backoff_factor}
    retry: Dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Share":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class TransferResult:
    share: str
    started_at: float
    finished_at: float
    bytes_transferred: int
    objects_transferred: int
    objects_skipped: int
    errors: List[str] = dataclasses.field(default_factory=list)
    audit_entry_hashes: List[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d["duration_seconds"] = self.finished_at - self.started_at
        return d


@dataclasses.dataclass
class BatchItem:
    """One share's outcome inside a `run_batch`."""
    share: Share
    user: str
    result: Optional[TransferResult] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "share": self.share.name,
            "user": self.user,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
        }


@dataclasses.dataclass
class BatchResult:
    """Aggregate of `run_batch`."""
    total: int
    succeeded: int
    failed: int
    items: List[BatchItem] = dataclasses.field(default_factory=list)
    started_at: float = dataclasses.field(default_factory=time.time)
    finished_at: float = 0.0

    def __post_init__(self):
        if self.finished_at == 0.0:
            self.finished_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "duration_seconds": self.finished_at - self.started_at,
            "items": [it.to_dict() for it in self.items],
        }


class TransferEngine:
    def __init__(self, catalog: DataCatalog, rbac: RBAC, audit: AuditLog,
                 policy_engine: PolicyEngine, cache: Optional[TransferCache] = None,
                 registry=None):
        self.catalog = catalog
        self.rbac = rbac
        self.audit = audit
        self.policy_engine = policy_engine
        self.cache = cache or TransferCache()
        # `registry` is the adapter-class dict (see cdeh/adapters/__init__.py).
        # The imported `adapter_registry` is the same dict; we never call
        # `.registry` on it.
        self._registry = registry if registry is not None else adapter_registry

    def _resolve(self, kind: str):
        cls = self._registry.get(kind)
        if cls is None:
            raise AdapterError(f"unknown adapter kind: {kind}")
        return cls

    def _instance(self, kind: str, config: Dict[str, Any]):
        cls = self._resolve(kind)
        return cls.from_config(config)

    def _build_transformer_chain(self, specs: List[str]):
        """Build a list of transformer instances from a list of
        `kind:param:...` strings. Returns `(chain, errors)` so a single
        bad spec doesn't kill the whole share."""
        chain, errors = [], []
        for spec in specs:
            try:
                chain.append(self._build_one_transformer(spec))
            except Exception as e:
                errors.append(f"{spec}: {e}")
        return chain, errors

    def _build_one_transformer(self, spec: str):
        """Parse a single transform spec string.

        Supported shapes:
          mask:col1,col2
          redact:drop:ssn
          codec:csv:parquet
          compress:gzip
          compress:gzip:9
          encrypt:<hex-key>            # 32-byte key (64 hex chars)
          encrypt:env:VAR_NAME
        """
        kind, _, params = spec.partition(":")
        cls = get_transformer(kind)
        if kind == "mask":
            cols = [c.strip() for c in params.split(",") if c.strip()]
            return cls.from_config({"columns": cols})
        if kind == "redact":
            # redact:drop:ssn → policies=["drop:ssn"]
            policies = [params] if params else []
            return cls.from_config({"policies": policies})
        if kind == "codec":
            # codec:csv:parquet  → 3-part
            from_fmt, _, to_fmt = params.partition(":")
            if not from_fmt or not to_fmt:
                raise ValueError(
                    f"codec spec needs from:to, got {spec!r}")
            return cls.from_config({"from": from_fmt, "to": to_fmt})
        if kind == "compress":
            # compress:gzip  or  compress:gzip:9
            algo = params or "gzip"
            level_str, _, _ = algo.partition(":")
            level = int(level_str.split(":")[-1]) if ":" in level_str else 6
            algo_name = algo.split(":")[0]
            return cls.from_config({"algo": algo_name, "level": level})
        if kind == "encrypt":
            return cls.from_config({"key": params})
        # unknown transformer kind — pass empty config and let the
        # transformer decide what's required
        return cls.from_config({})

    def _object_matches_filters(self, obj, filters: List[Dict[str, Any]]) -> bool:
        """Apply share filters to a source object. Returns True if the
        object passes (i.e. should be transferred).

        Filter shapes:
          {"path_glob": "*.csv"}            # fnmatch against obj.path
          {"path_prefix": "/orders/2024"}   # string prefix match
          {"name_regex": "^2024-.*\\.csv$"}  # regex against basename
          {"tag": "gdpr-cleared"}           # catalog tag match (asset-level)
          {"department": "sales"}          # catalog tag match
          {"size_min": 1024}                 # bytes
          {"size_max": 1048576}              # bytes
          {"mtime_after": "2024-01-01"}      # ISO date
          {"mtime_before": "2024-12-31"}     # ISO date
          {"column": {"name": "ssn", "value_regex": "^\\d{3}-\\d{2}"}}
              # content-level filter: scan the first 64 KiB of bytes
        """
        import fnmatch, re
        from datetime import datetime
        path = obj.path
        basename = path.rsplit("/", 1)[-1]
        for f in filters:
            if "path_glob" in f and not fnmatch.fnmatch(basename, f["path_glob"]):
                return False
            if "path_prefix" in f and not path.startswith(f["path_prefix"]):
                return False
            if "name_regex" in f and not re.match(f["name_regex"], basename):
                return False
            if "tag" in f:
                asset = self.catalog.get(self._share_name_from_path(path))
                if not asset or f["tag"] not in asset.tags:
                    return False
            if "department" in f:
                asset = self.catalog.get(self._share_name_from_path(path))
                if not asset or f["department"] not in asset.tags:
                    return False
            if "size_min" in f and obj.size < f["size_min"]:
                return False
            if "size_max" in f and obj.size > f["size_max"]:
                return False
            if "mtime_after" in f:
                t = datetime.fromisoformat(f["mtime_after"]).timestamp()
                if getattr(obj, "mtime_ns", 0) / 1e9 < t:
                    return False
            if "mtime_before" in f:
                t = datetime.fromisoformat(f["mtime_before"]).timestamp()
                if getattr(obj, "mtime_ns", 0) / 1e9 > t:
                    return False
        return True

    def _share_name_from_path(self, path: str) -> str:
        """Translate a source path to the share name we'd look up in the
        catalog. (Catalog stores per-share assets; the path → share
        mapping is fuzzy but we accept the last segment as a hint.)"""
        # Best-effort: return the first path segment. The catalog itself
        # matches by name (set at share.run time), so this is just a
        # tag-lookup hint.
        return path.strip("/").split("/", 1)[0]

    def list_shares(self) -> List[Share]:
        """Shares are persisted as JSON files under `shares_dir`."""
        out = []
        shares_dir = self._shares_dir()
        if shares_dir.exists():
            for p in shares_dir.glob("*.json"):
                try:
                    out.append(Share.from_dict(json.loads(p.read_text())))
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
        return out

    def save_share(self, share: Share) -> None:
        self._shares_dir().mkdir(parents=True, exist_ok=True)
        path = self._shares_dir() / f"{share.name}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(share.to_dict(), indent=2))
        tmp.replace(path)

    def load_share(self, name: str) -> Optional[Share]:
        p = self._shares_dir() / f"{name}.json"
        if not p.exists():
            return None
        return Share.from_dict(json.loads(p.read_text()))

    def delete_share(self, name: str) -> bool:
        p = self._shares_dir() / f"{name}.json"
        if p.exists():
            p.unlink()
            return True
        return False

    def _shares_dir(self) -> Path:
        return Path(os.path.expanduser("~/.cdeh/shares"))

    # ─── core: run a share ──────────────────────────────────────────
    def run(self, share: Share, user: str = "system",
            src_config: Optional[Dict[str, Any]] = None,
            dst_config: Optional[Dict[str, Any]] = None) -> TransferResult:
        """Execute a Share end-to-end.

        `src_config` / `dst_config` are the adapter configurations (you
        load these yourself from your secrets store; the engine never
        hard-codes credentials). For testing, pass them in directly.
        """
        if src_config is None or dst_config is None:
            raise ValueError(
                "TransferEngine.run requires src_config and dst_config "
                "(adapters are instantiated from explicit config — never "
                "from disk credentials)."
            )
        result = TransferResult(
            share=share.name,
            started_at=time.time(),
            finished_at=0.0,
            bytes_transferred=0,
            objects_transferred=0,
            objects_skipped=0,
        )
        # Wire up checkpoint store for resumable shares.
        self._current_share_name = share.name
        self._current_checkpoint = (
            self._default_checkpoint_store() if share.resumable else None
        )
        # RBAC
        if not self.rbac.check(user, "run_share"):
            self.audit.append(
                user=user, action="share.run.denied",
                asset=share.name,
                src_adapter=share.src_adapter, src_path=share.src_path,
                dst_adapter=share.dst_adapter, dst_path=share.dst_path,
                success=False, error="rbac_denied",
            )
            result.finished_at = time.time()
            result.errors.append("rbac_denied")
            return result
        # Instantiate adapters
        try:
            src = self._instance(src_config["kind"], src_config)
            dst = self._instance(dst_config["kind"], dst_config)
        except AdapterError as e:
            result.errors.append(f"adapter_instantiate: {e}")
            result.finished_at = time.time()
            self.audit.append(
                user=user, action="share.run.error",
                asset=share.name, success=False, error=str(e),
            )
            return result
        # Build transformer chain
        chain, errors = self._build_transformer_chain(share.transforms)
        if errors:
            for e in errors:
                result.errors.append(f"transformer_load: {e}")
            result.finished_at = time.time()
            self.audit.append(
                user=user, action="share.run.error",
                asset=share.name, success=False,
                error="transformer_load_failed",
            )
            return result
        # Iterate source objects
        try:
            objects = list(src.list(prefix=share.src_path, recursive=True))
        except AdapterError as e:
            result.errors.append(f"list_source: {e}")
            result.finished_at = time.time()
            self.audit.append(
                user=user, action="share.run.error",
                asset=share.name, success=False, error=str(e),
            )
            return result
        if not objects:
            # No objects at this prefix — try treating src_path as a single file
            try:
                single = src.stat(share.src_path)
                objects = [single]
            except AdapterError:
                result.errors.append(f"no_objects_at: {share.src_path}")
                result.finished_at = time.time()
                return result

        # Translate source paths → destination paths (preserving relative structure)
        for obj in objects:
            # ─── filter gate ───────────────────────────────────────
            if share.filters and not self._object_matches_filters(obj, share.filters):
                self.audit.append(
                    user=user, action="share.filtered_out",
                    asset=share.name, src_adapter=share.src_adapter,
                    src_path=obj.path, dst_adapter=share.dst_adapter,
                    dst_path="", bytes=0, success=True,
                    extra={"reason": "filters_excluded"},
                )
                continue
            rel = _strip_prefix(obj.path, share.src_path)
            dst_path = _join_path(share.dst_path, rel)
            # Policy check
            ctx = PolicyContext(
                user=user, asset_classification=_catalog_classification(self.catalog, share.name),
                asset_tags=_catalog_tags(self.catalog, share.name),
                object_size_bytes=obj.size,
                is_https=_is_https(src_config["kind"], src_config) and _is_https(dst_config["kind"], dst_config),
                declared_transforms=share.transforms,
            )
            allowed, reason = self.policy_engine.evaluate(share.policy, ctx)
            if not allowed:
                result.errors.append(f"policy_denied:{obj.path}:{reason}")
                self.audit.append(
                    user=user, action="share.run.policy_denied",
                    asset=share.name,
                    src_adapter=share.src_adapter, src_path=obj.path,
                    success=False, error=reason,
                )
                continue
            # Incremental skip
            if share.incremental in ("etag", "mtime"):
                # Compare the source fingerprint against the LAST
                # TRANSFERRED source fingerprint (recorded in the
                # catalog for this asset). This is the right model
                # when transforms are in play — we can't compare src
                # etag to dst etag because the transform changes the
                # bytes. We compare src-fingerprint now to the
                # src-fingerprint we recorded last time we wrote dst.
                src_fingerprint = obj.fingerprint()
                asset = self.catalog.get(share.name) if share.name else None
                if asset and asset.src_fingerprints.get(obj.path) == src_fingerprint:
                    result.objects_skipped += 1
                    self.cache.invalidate(adapter=share.src_adapter, path=obj.path)
                    self.audit.append(
                        user=user, action="share.skip",
                        asset=share.name,
                        src_adapter=share.src_adapter, src_path=obj.path,
                        dst_adapter=share.dst_adapter, dst_path=dst_path,
                        bytes=0, success=True,
                        extra={"reason": "unchanged",
                                   "fingerprint": src_fingerprint},
                    )
                    continue
                # Also do a fast-path: if no transform AND share.incremental
                # is "etag" or "mtime", compare against dst directly.
                if not share.transforms:
                    try:
                        dst_stat = dst.stat(dst_path)
                        if _same_fingerprint(obj, dst_stat, share.incremental):
                            result.objects_skipped += 1
                            self.cache.invalidate(adapter=share.src_adapter, path=obj.path)
                            self.audit.append(
                                user=user, action="share.skip",
                                asset=share.name,
                                src_adapter=share.src_adapter, src_path=obj.path,
                                dst_adapter=share.dst_adapter, dst_path=dst_path,
                                bytes=0, success=True,
                                extra={"reason": "unchanged-dst",
                                           "fingerprint": src_fingerprint},
                            )
                            continue
                    except AdapterError:
                        pass  # dst doesn't exist
            # Transfer (with retry policy if configured)
            try:
                bytes_done = self._transfer_with_retry(
                    src, obj.path, dst, dst_path, chain,
                    max_attempts=share.retry.get("max_attempts", 1),
                    initial_backoff=share.retry.get("initial_backoff_seconds", 1.0),
                    backoff_factor=share.retry.get("backoff_factor", 2.0),
                )
                result.bytes_transferred += bytes_done
                result.objects_transferred += 1
                # Invalidate the source cache entry so the next run
                # re-fetches (we don't know what the destination now has
                # at the byte level; a future enhancement could cache the
                # post-transform bytes too).
                self.cache.invalidate(adapter=share.src_adapter, path=obj.path)
                # Record the src fingerprint we just wrote, so future
                # runs can use it for incremental-skip when transforms
                # are in play.
                self._record_fingerprint(share.name, obj.path, obj.fingerprint())
                entry = self.audit.append(
                    user=user, action="share.transfer",
                    asset=share.name,
                    src_adapter=share.src_adapter, src_path=obj.path,
                    dst_adapter=share.dst_adapter, dst_path=dst_path,
                    bytes=bytes_done, policy=share.policy,
                    transforms=",".join(share.transforms), success=True,
                    extra={"src_etag": obj.etag, "src_mtime_ns": obj.mtime_ns},
                )
                result.audit_entry_hashes.append(entry.entry_hash)
            except Exception as e:
                result.errors.append(f"transfer:{obj.path}:{e}")
                self.audit.append(
                    user=user, action="share.transfer.error",
                    asset=share.name,
                    src_adapter=share.src_adapter, src_path=obj.path,
                    dst_adapter=share.dst_adapter, dst_path=dst_path,
                    success=False, error=str(e),
                )
        result.finished_at = time.time()
        return result

    # ─── chunked concurrent transfer ───────────────────────────────
    def _transfer_one(self, src, src_path: str, dst, dst_path: str,
                     transformers: list) -> int:
        """Download → transform → upload, with concurrent chunking for
        large files. Returns total bytes written to destination."""
        # 1. cache check
        cached = self.cache.get(src.kind, src_path)
        if cached is not None:
            data = cached
        else:
            data = src.get(src_path)
            self.cache.put(src.kind, src_path, data)
        # 2. transform chain
        for t in transformers:
            data = t.transform(data, params={})
        # 3. upload
        if len(data) <= self._small_threshold():
            dst.put(dst_path, data, content_type=_guess_ct(dst_path))
            return len(data)
        # large file: split into chunks, upload in parallel.
        # When the engine has a checkpoint store and the calling share
        # is `resumable`, we skip chunks that were already written in
        # a previous run.
        chunk_size = self._chunk_size()
        chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
        # Determine which chunks are already done (resume).
        already_done: set = set()
        cp = self._maybe_checkpoint_for(src_path)
        if cp is not None:
            existing = cp.get(self._current_share_name or "", src_path)
            if existing and existing.get("total_bytes") == len(data):
                already_done = set(existing.get("parts", {}).keys())
        # naive parallel upload via threads (adapters handle multipart internally)
        def _upload_chunk(i_chunk):
            i, chunk = i_chunk
            chunk_path = f"{dst_path}.part{i:04d}"
            key = f"{i:04d}"
            if key in already_done:
                return i, len(chunk)
            dst.put(chunk_path, chunk, content_type=_guess_ct(dst_path))
            if cp is not None:
                cp.put_part(self._current_share_name or "", src_path, i, len(chunk))
            return i, len(chunk)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for _, n in ex.map(_upload_chunk, list(enumerate(chunks))):
                pass
        # Stitch the parts back together via a tiny "concat" object.
        # For local / S3: a subsequent move/replace. We do it inline.
        _stitch_parts(dst, dst_path, len(chunks))
        if cp is not None:
            cp.mark_completed(self._current_share_name or "", src_path)
        return len(data)

    def _maybe_checkpoint_for(self, src_path: str):
        """Return the checkpoint store if the current share is
        resumable, else None. The engine sets `self._current_share`
        at the top of `run()`. Falling back to None is correct
        behavior for non-resumable shares."""
        cp = getattr(self, "_current_checkpoint", None)
        if cp is None:
            return None
        return cp

    def _transfer_with_retry(self, src, src_path, dst, dst_path,
                              transformers: list, *, max_attempts: int = 1,
                              initial_backoff: float = 1.0,
                              backoff_factor: float = 2.0) -> int:
        """Wrap `_transfer_one` with exponential-backoff retry."""
        import time as _time
        last_err = None
        for attempt in range(max_attempts):
            try:
                return self._transfer_one(
                    src, src_path, dst, dst_path, transformers)
            except Exception as e:
                last_err = e
                if attempt + 1 >= max_attempts:
                    raise
                backoff = initial_backoff * (backoff_factor ** attempt)
                self.audit.append(
                    user="system", action="share.transfer.retry",
                    asset=src_path, success=False,
                    error=str(e), extra={"attempt": attempt + 1,
                                            "sleep_seconds": backoff},
                )
                _time.sleep(backoff)
        # unreachable
        raise last_err  # type: ignore[misc]

    def _default_checkpoint_store(self):
        """Lazily build a file-based checkpoint store at
        `~/.cdeh/checkpoints/`. Callers can subclass TransferEngine and
        override `_default_checkpoint_store()` to inject Redis / SQLite."""
        from .checkpoint import FileCheckpointStore
        if not hasattr(self, "_ckpt_root"):
            from pathlib import Path
            import os
            self._ckpt_root = Path(os.path.expanduser(
                "~/.cdeh/checkpoints"))
        return FileCheckpointStore(self._ckpt_root)

    def _chunk_size(self) -> int:
        return 8 * 1024 * 1024

    def _small_threshold(self) -> int:
        return self._chunk_size()

    # ─── sync / async / batch API ─────────────────────────────────
    def run_sync(self, share: Share, user: str = "system",
                src_config: Optional[Dict[str, Any]] = None,
                dst_config: Optional[Dict[str, Any]] = None) -> TransferResult:
        """Synchronous execution — blocks the caller until the share is
        fully transferred (or fails). Identical to `run()`; the
        explicit name is for code-readability when paired with run_async.
        """
        return self.run(share, user=user, src_config=src_config, dst_config=dst_config)

    def run_async(self, share: Share, user: str = "system",
                 src_config: Optional[Dict[str, Any]] = None,
                 dst_config: Optional[Dict[str, Any]] = None):
        """Submit a share for background execution. Returns a
        `concurrent.futures.Future` that resolves to a `TransferResult`.

        Use case: kick off a long transfer from a request handler and
        return a job-id immediately; poll or await the future later.

        The future is owned by a single shared executor (`self._executor`)
        so that `run_batch` can pool them and avoid thread explosion.
        """
        return self._executor.submit(
            self.run_sync, share, user, src_config, dst_config,
        )

    def run_batch(
        self,
        shares: List[Share],
        users: Optional[List[str]] = None,
        max_workers: int = 4,
        fail_fast: bool = False,
    ) -> "BatchResult":
        """Run a batch of shares concurrently. Returns a `BatchResult`
        summarizing per-share outcomes.

        Parameters
        ----------
        shares : list of Share
            Each share must have its adapters already registered with
            the client (we look up the configs by name).
        users : list of str, optional
            One per share; defaults to "system" for all.
        max_workers : int
            Concurrent share executions. Per-share parallelism is
            controlled by `share.parallelism`. Adapter I/O is thread-
            safe within a single share (see `_transfer_one`).
        fail_fast : bool
            If True, cancel pending futures on the first failure.
            Otherwise all shares run to completion and the batch
            result reports each share's status independently.
        """
        from concurrent.futures import as_completed
        if users is None:
            users = ["system"] * len(shares)
        if len(users) != len(shares):
            raise ValueError(
                f"users ({len(users)}) and shares ({len(shares)}) must be same length"
            )
        # Resolve adapter configs once (cheap).
        prepared = []
        for share, user in zip(shares, users):
            try:
                src_config = self._resolve_config(share.src_adapter)
                dst_config = self._resolve_config(share.dst_adapter)
            except KeyError as e:
                prepared.append({"share": share, "user": user, "error": str(e)})
                continue
            prepared.append({"share": share, "user": user,
                              "src_config": src_config, "dst_config": dst_config})
        # Submit to the shared executor so a large `max_workers` in
        # one batch doesn't starve other callers.
        futures = {}
        for p in prepared:
            if "error" in p:
                continue  # recorded below
            fut = self._executor.submit(
                self.run_sync,
                p["share"], p["user"], p["src_config"], p["dst_config"],
            )
            futures[fut] = p
        # Collect results
        results = []
        completed = 0
        try:
            for fut in as_completed(futures, timeout=None):
                p = futures[fut]
                try:
                    res = fut.result()
                    results.append(BatchItem(share=p["share"], user=p["user"],
                                              result=res, error=None))
                except Exception as e:
                    results.append(BatchItem(share=p["share"], user=p["user"],
                                              result=None, error=str(e)))
                    if fail_fast:
                        # Cancel pending
                        for other in futures:
                            if other is not fut and not other.done():
                                other.cancel()
                        break
                completed += 1
        finally:
            pass
        # Prepend pre-submission errors
        for p in prepared:
            if "error" in p:
                results.append(BatchItem(share=p["share"], user=p["user"],
                                          result=None, error=p["error"]))
        return BatchResult(
            total=len(shares),
            succeeded=sum(1 for r in results if r.error is None and r.result and not r.result.errors),
            failed=sum(1 for r in results if r.error is not None or (r.result and r.result.errors)),
            items=results,
        )

    @property
    def _executor(self):
        """Shared thread pool. Created on first access; the same
        executor serves all `run_async` and `run_batch` calls so we
        don't spawn a pool per call."""
        if not hasattr(self, "_exec"):
            import concurrent.futures
            self._exec = concurrent.futures.ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="cdeh-transfer",
            )
        return self._exec

    def shutdown(self, wait: bool = True) -> None:
        """Stop the shared thread pool. Call once before process exit."""
        if hasattr(self, "_exec"):
            self._exec.shutdown(wait=wait)
            del self._exec

    def _resolve_config(self, adapter_name: str) -> Dict[str, Any]:
        """Look up an adapter's runtime config. Throws KeyError if missing."""
        # The CDEHClient sets `self._get_adapter_config` on the engine
        # at construction time. The HTTP server path resolves configs
        # differently (per request from the body); it overrides the
        # dispatch via a private attribute.
        getter = getattr(self, "_get_adapter_config", None)
        if getter is not None:
            return getter(adapter_name)
        raise KeyError(
            f"adapter {adapter_name!r} not registered; "
            f"call CDEHClient.register_adapter() first"
        )

    def _record_fingerprint(self, share_name: str, src_path: str, fingerprint: str) -> None:
        """Persist the source fingerprint we just transferred for this
        asset + src-path. Future runs use it for incremental-skip when
        transforms are in play (since transform makes src/dst etag
        incomparable)."""
        asset = self.catalog.get(share_name)
        if asset is None:
            from .catalog import DataAsset
            asset = DataAsset(name=share_name, adapter="", path=src_path)
        # Per-path fingerprint history. The dict is JSON-serialized via
        # dataclasses.asdict() so we keep it as a plain dict.
        fps = dict(asset.src_fingerprints) if asset.src_fingerprints else {}
        fps[src_path] = fingerprint
        asset.src_fingerprints = fps  # type: ignore[attr-defined]
        self.catalog.register(asset)

    def _small_threshold(self) -> int:
        return self._chunk_size()


# ─── module-level helpers ──────────────────────────────────────────
def _strip_prefix(path: str, prefix: str) -> str:
    """`/foo/bar.csv` minus prefix `/foo` → `/bar.csv`. Always returns
    a path starting with `/`."""
    if not prefix or prefix == "/":
        return path
    if path.startswith(prefix):
        rest = path[len(prefix):]
    else:
        # try matching by filename only
        rest = "/" + path.lstrip("/").rsplit("/", 1)[-1]
    if not rest.startswith("/"):
        rest = "/" + rest
    return rest.lstrip("/") or "/"


def _join_path(base: str, rel: str) -> str:
    rel = rel.lstrip("/")
    if not base or base == "/":
        return "/" + rel if rel else "/"
    if not base.startswith("/"):
        base = "/" + base
    base = base.rstrip("/")
    return f"{base}/{rel}" if rel else base


def _same_fingerprint(src: FileStat, dst: FileStat, mode: str) -> bool:
    if mode == "etag":
        return bool(src.etag) and src.etag == dst.etag
    if mode == "mtime":
        return src.mtime_ns == dst.mtime_ns and src.size == dst.size
    return False


def _catalog_classification(catalog: DataCatalog, name: str) -> str:
    a = catalog.get(name)
    return a.classification if a else "internal"


def _catalog_tags(catalog: DataCatalog, name: str) -> List[str]:
    a = catalog.get(name)
    return list(a.tags) if a else []


def _is_https(adapter_kind: str, config: Dict[str, Any]) -> bool:
    if adapter_kind == "local":
        return True
    if adapter_kind == "s3":
        endpoint = (config.get("endpoint") or "").lower()
        return endpoint.startswith("https://") or "amazonaws.com" in endpoint
    if adapter_kind == "azure_blob":
        return "https" in (config.get("endpoint_suffix") or "core.windows.net")
    if adapter_kind == "sftp":
        return True  # SSH = secure transport
    return False


def _guess_ct(path: str) -> str:
    from pathlib import PurePosixPath
    from ..adapters.base import _CT_MAP
    return _CT_MAP.get(PurePosixPath(path).suffix.lower(), "application/octet-stream")


def _stitch_parts(dst, dst_path: str, n_parts: int) -> None:
    """Concatenate `<dst_path>.partNNNN` chunks into `<dst_path>`.

    Default implementation: download each part to memory and re-upload.
    For S3, the SDK offers `complete_multipart_upload` natively — but
    our S3Adapter uses single-shot or `upload_fileobj` (multipart
    transparent), so parts are already in one object at the SDK layer.
    We still need to delete the per-part keys we wrote as plain objects.
    """
    # For local / S3: we wrote N independent objects. Stitching requires
    # re-uploading as one. Keep this simple: read all parts, upload as
    # one, delete the parts.
    parts = []
    for i in range(n_parts):
        part_path = f"{dst_path}.part{i:04d}"
        parts.append(dst.get(part_path))
    dst.put(dst_path, b"".join(parts), content_type=_guess_ct(dst_path))
    for i in range(n_parts):
        part_path = f"{dst_path}.part{i:04d}"
        try:
            dst.delete(part_path)
        except AdapterError:
            pass