"""RBAC — role-based access control over adapters and assets.

Roles: viewer / operator / admin. Permissions:
  - view_catalog
  - run_share
  - manage_adapter
  - manage_policy
  - manage_users

Users are loaded from a JSON file at `~/.cdeh/users.json`. In a real
deployment this would be backed by LDAP / OIDC.
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


PERMISSIONS = {
    "admin":     {"view_catalog", "run_share", "manage_adapter", "manage_policy", "manage_users", "audit"},
    "operator":  {"view_catalog", "run_share", "manage_adapter", "audit"},
    "viewer":    {"view_catalog"},
}


@dataclasses.dataclass
class User:
    name: str
    role: str = "viewer"
    api_key: str = ""
    allowed_assets: Set[str] = dataclasses.field(default_factory=set)  # for row-level access
    # ─── NEW: groups + department ──────────────────────────────────
    # Groups are logical collections (e.g. "data-platform", "finance")
    # used for bulk permission grants. Departments are an org-chart
    # tag (e.g. "sales", "engineering") used for data-isolation:
    # assets tagged with a department are only visible to users in
    # that department unless explicitly granted.
    groups: Set[str] = dataclasses.field(default_factory=set)
    department: str = ""

    def has(self, perm: str) -> bool:
        return perm in PERMISSIONS.get(self.role, set())

    def can_see_asset(self, asset_name: str) -> bool:
        """Per-user asset ACL — empty set means 'all assets visible'."""
        return not self.allowed_assets or asset_name in self.allowed_assets

    def in_group(self, name: str) -> bool:
        return name in self.groups

    def in_department(self, name: str) -> bool:
        """Department match. Admins see all departments (override)."""
        if self.role == "admin":
            return True
        return self.department == name


class RBAC:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or os.path.expanduser("~/.cdeh/users.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._users: Dict[str, User] = {}
        self._load()

    def _load(self):
        with self._lock:
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text())
                    for name, d in raw.items():
                        self._users[name] = User(
                            name=name,
                            role=d.get("role", "viewer"),
                            api_key=d.get("api_key", ""),
                            allowed_assets=set(d.get("allowed_assets", [])),
                            groups=set(d.get("groups", [])),
                            department=d.get("department", ""),
                        )
                except (json.JSONDecodeError, KeyError, TypeError):
                    self._users = {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        with self._lock:
            data = {
                n: {"role": u.role, "api_key": u.api_key,
                    "allowed_assets": sorted(u.allowed_assets),
                    "groups": sorted(u.groups),
                    "department": u.department}
                for n, u in self._users.items()
            }
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def add_user(self, name: str, role: str = "viewer", api_key: str = "",
                 allowed_assets: Optional[List[str]] = None,
                 groups: Optional[List[str]] = None,
                 department: str = "") -> User:
        if role not in PERMISSIONS:
            raise ValueError(f"unknown role: {role}. valid: {list(PERMISSIONS)}")
        user = User(
            name=name, role=role, api_key=api_key,
            allowed_assets=set(allowed_assets or []),
            groups=set(groups or []),
            department=department,
        )
        with self._lock:
            self._users[name] = user
            self._save()
        return user

    def add_to_group(self, user_name: str, group: str) -> None:
        """Add a user to a group. Used by admin tooling or SSO
        role-claim sync (e.g. `admin` group from LDAP)."""
        with self._lock:
            u = self._users.get(user_name)
            if not u:
                raise KeyError(f"no such user: {user_name}")
            u.groups.add(group)
            self._save()

    def set_department(self, user_name: str, department: str) -> None:
        with self._lock:
            u = self._users.get(user_name)
            if not u:
                raise KeyError(f"no such user: {user_name}")
            u.department = department
            self._save()

    def get(self, name: str) -> Optional[User]:
        with self._lock:
            return self._users.get(name)

    def by_api_key(self, key: str) -> Optional[User]:
        with self._lock:
            for u in self._users.values():
                if u.api_key and u.api_key == key:
                    return u
        return None

    def check(self, user_name: str, perm: str, asset_name: Optional[str] = None,
              asset_tags: Optional[List[str]] = None) -> bool:
        """Three-layer permission check.

        1. Permission: does the user's role grant `perm`?
        2. Asset ACL: is the asset in user's `allowed_assets`?
        3. Department isolation: if the asset carries a `department`
           tag, does the user belong to that department?
        Admins always pass.
        """
        u = self.get(user_name)
        if not u:
            return False
        if u.role == "admin":
            return True
        if not u.has(perm):
            return False
        if asset_name and not u.can_see_asset(asset_name):
            return False
        if asset_tags:
            asset_dept = next((t[len("dept:"):] for t in asset_tags
                                if t.startswith("dept:")), "")
            if asset_dept and not u.in_department(asset_dept):
                return False
        return True

    def list_users(self) -> List[User]:
        with self._lock:
            return list(self._users.values())