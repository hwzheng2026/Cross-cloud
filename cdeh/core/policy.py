"""Policy engine — declarative access policy + rate limit + compliance tag.

A policy is a JSON/YAML-serializable dict:

    {
        "id": "gdpr-strict",
        "require_classification": ["internal", "confidential"],
        "deny_classification": ["restricted"],
        "require_tags": ["gdpr-cleared"],
        "rate_limit_per_minute": 60,
        "max_object_size_bytes": 1073741824,
        "require_https": true,
        "transform_required": ["mask:email,phone"],
    }

`evaluate(policy, context)` returns (allowed, reason).
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PolicyContext:
    """Inputs to `PolicyEngine.evaluate`."""
    user: str
    asset_classification: str = "internal"
    asset_tags: List[str] = field(default_factory=list)
    object_size_bytes: int = 0
    is_https: bool = True
    declared_transforms: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class Policy:
    id: str
    require_classification: List[str] = field(default_factory=list)
    deny_classification: List[str] = field(default_factory=list)
    require_tags: List[str] = field(default_factory=list)
    rate_limit_per_minute: int = 0         # 0 = no limit
    max_object_size_bytes: int = 0         # 0 = no limit
    require_https: bool = False
    transform_required: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Policy":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class PolicyEngine:
    """Evaluate policies against operation contexts.

    Built-in policies (always available, override-able via `add_policy`):
      - "default":     allow everything, rate limit 600/min
      - "gdpr":        require classification in {public, internal, confidential},
                       forbid restricted, require_https
      - "gdpr-strict":  same as gdpr + require mask transform
      - "block-all":   deny everything
    """
    BUILTINS: Dict[str, Policy] = {
        "default": Policy(id="default", rate_limit_per_minute=600),
        "gdpr": Policy(
            id="gdpr",
            require_classification=["public", "internal", "confidential"],
            deny_classification=["restricted"],
            require_https=True,
            rate_limit_per_minute=300,
        ),
        "gdpr-strict": Policy(
            id="gdpr-strict",
            require_classification=["public", "internal", "confidential"],
            deny_classification=["restricted"],
            require_https=True,
            require_tags=[],  # customizable
            transform_required=["mask"],
            rate_limit_per_minute=120,
        ),
        "block-all": Policy(id="block-all"),
    }

    def __init__(self):
        self._policies: Dict[str, Policy] = dict(self.BUILTINS)
        self._rate_windows: Dict[str, list] = {}  # user -> [ts, ...]
        self._lock = threading.RLock()

    def add_policy(self, policy: Policy) -> None:
        with self._lock:
            self._policies[policy.id] = policy

    def get(self, name: str) -> Optional[Policy]:
        return self._policies.get(name)

    def evaluate(self, policy_name: str, ctx: PolicyContext) -> Tuple[bool, str]:
        p = self._policies.get(policy_name)
        if p is None:
            return False, f"unknown policy: {policy_name}"
        if p.deny_classification and ctx.asset_classification in p.deny_classification:
            return False, f"classification '{ctx.asset_classification}' is denied by policy '{policy_name}'"
        if p.require_classification and ctx.asset_classification not in p.require_classification:
            return False, f"classification '{ctx.asset_classification}' not in allow-list {p.require_classification}"
        if p.require_tags:
            missing = set(p.require_tags) - set(ctx.asset_tags)
            if missing:
                return False, f"missing required tags: {sorted(missing)}"
        if p.require_https and not ctx.is_https:
            return False, "policy requires HTTPS transport"
        if p.max_object_size_bytes and ctx.object_size_bytes > p.max_object_size_bytes:
            return False, f"object {ctx.object_size_bytes}B exceeds max {p.max_object_size_bytes}B"
        if p.transform_required:
            missing = [t for t in p.transform_required
                       if not any(t.split(":")[0] in dt for dt in ctx.declared_transforms)]
            if missing:
                return False, f"missing required transforms: {missing}"
        # Rate limit (sliding window)
        if p.rate_limit_per_minute:
            allowed = self._check_rate(ctx.user, p.rate_limit_per_minute)
            if not allowed:
                return False, f"rate limit {p.rate_limit_per_minute}/min exceeded for user {ctx.user!r}"
        return True, "ok"

    def _check_rate(self, user: str, limit_per_min: int) -> bool:
        with self._lock:
            now = time.time()
            window_start = now - 60
            arr = [t for t in self._rate_windows.get(user, []) if t > window_start]
            if len(arr) >= limit_per_min:
                self._rate_windows[user] = arr
                return False
            arr.append(now)
            self._rate_windows[user] = arr
            return True