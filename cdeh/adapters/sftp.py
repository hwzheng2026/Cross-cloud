"""SFTP adapter — for legacy / on-prem / partner integrations.

Lazy paramiko import: only when the user actually registers an SFTP
adapter does the dep get loaded.
"""
from __future__ import annotations

import os
from typing import Any, BinaryIO, Dict, Iterator, Optional, Tuple, Union

from .base import AdapterError, AdapterNotFound, BaseAdapter, FileStat


class SFTPAdapter(BaseAdapter):
    kind = "sftp"

    def __init__(self, host: str, port: int = 22, user: str = "cdeh",
                 key_path: Optional[str] = None,
                 password: Optional[str] = None,
                 root: str = "/",
                 host_key_policy: str = "auto"):
        try:
            import paramiko
        except ImportError as e:
            raise AdapterError(
                "SFTPAdapter requires `pip install paramiko`"
            ) from e
        self._paramiko = paramiko
        self.host = host
        self.port = port
        self.user = user
        self.key_path = key_path
        self.password = password
        self.root = root.rstrip("/")
        self.host_key_policy = host_key_policy
        self._client = None  # lazy-connect

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "SFTPAdapter":
        for k in ("host",):
            if k not in cfg:
                raise AdapterError(f"SFTPAdapter requires config['{k}']")
        return cls(
            host=cfg["host"],
            port=cfg.get("port", 22),
            user=cfg.get("user", "cdeh"),
            key_path=cfg.get("key_path"),
            password=cfg.get("password"),
            root=cfg.get("root", "/"),
        )

    def _connect(self):
        if self._client is not None:
            return
        p = self._paramiko
        client = p.SSHClient()
        policy = self._paramiko.AutoAddPolicy() if self.host_key_policy == "auto" else p.RejectPolicy()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(policy)
        kwargs = {
            "hostname": self.host, "port": self.port, "username": self.user,
            "look_for_keys": False, "allow_agent": False,
        }
        if self.key_path:
            kwargs["key_filename"] = self.key_path
        if self.password:
            kwargs["password"] = self.password
        client.connect(**kwargs)
        self._client = client

    def ping(self) -> Dict[str, Any]:
        try:
            self._connect()
            sftp = self._client.open_sftp()
            sftp.listdir(self.root or "/")
            sftp.close()
        except Exception as e:
            raise AdapterError(f"ping failed: {e}") from e
        return {"kind": "sftp", "host": self.host, "port": self.port, "root": self.root}

    def _abspath(self, path: str) -> str:
        rel = path.lstrip("/")
        if self.root:
            return f"{self.root}/{rel}" if rel else self.root
        return "/" + rel if rel else "/"

    def _sftp(self):
        self._connect()
        return self._client.open_sftp()

    def stat(self, path: str) -> FileStat:
        sftp = self._sftp()
        try:
            st = sftp.stat(self._abspath(path))
        except IOError as e:
            if "No such file" in str(e):
                raise AdapterNotFound(f"file not found: {path}")
            raise AdapterError(f"stat failed: {e}") from e
        finally:
            sftp.close()
        rel = path.lstrip("/")
        return FileStat(
            path="/" + rel,
            size=st.st_size,
            etag=f"sftp-{st.st_mtime}-{st.st_size}",
            mtime_ns=int(st.st_mtime * 1_000_000_000),
        )

    def list(self, prefix: str = "", recursive: bool = True) -> Iterator[FileStat]:
        sftp = self._sftp()
        try:
            base = self._abspath(prefix) if prefix else (self.root or "/")
            for entry in sftp.listdir_attr(base):
                rel = entry.filename
                if prefix:
                    rel = prefix.rstrip("/") + "/" + rel
                full = self._abspath(rel)
                if _is_dir_attr(entry):
                    if recursive:
                        yield from self.list("/" + entry.filename if not prefix else
                                            prefix.rstrip("/") + "/" + entry.filename,
                                            recursive=recursive)
                else:
                    yield FileStat(
                        path="/" + rel.lstrip("/"),
                        size=entry.st_size,
                        etag=f"sftp-{entry.st_mtime}-{entry.st_size}",
                        mtime_ns=int(entry.st_mtime * 1_000_000_000),
                    )
        finally:
            sftp.close()

    def get(self, path: str, range_: Optional[Tuple[int, int]] = None) -> bytes:
        sftp = self._sftp()
        try:
            with sftp.open(self._abspath(path), "rb") as f:
                if range_ is not None:
                    start, end = range_
                    f.seek(start)
                    return f.read(end - start + 1)
                return f.read()
        finally:
            sftp.close()

    def put(self, path: str, data: Union[bytes, BinaryIO],
           content_type: str = "",
           metadata: Optional[Dict[str, str]] = None) -> FileStat:
        sftp = self._sftp()
        try:
            target = self._abspath(path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with sftp.open(target, "wb") as f:
                if isinstance(data, (bytes, bytearray)):
                    f.write(bytes(data))
                else:
                    while True:
                        chunk = data.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
        finally:
            sftp.close()
        return self.stat(path)

    def delete(self, path: str) -> None:
        sftp = self._sftp()
        try:
            target = self._abspath(path)
            try:
                sftp.remove(target)
            except IOError:
                pass
        finally:
            sftp.close()


def _is_dir_attr(attr) -> bool:
    """SFTPAttributes: longname looks like 'drwxr-xr-x' if it's a dir."""
    ln = getattr(attr, "longname", "") or ""
    return ln.startswith("d") if ln else bool(getattr(attr, "st_mode", 0) & 0o40000)