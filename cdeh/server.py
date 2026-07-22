"""HTTP gateway — exposes CDEHClient via REST. Stdlib only (no Flask
dependency).

Routes:
  GET    /adapters
  POST   /adapters
  DELETE /adapters/{name}
  GET    /adapters/{name}
  GET    /shares
  POST   /shares
  GET    /shares/{name}
  DELETE /shares/{name}
  POST   /shares/{name}/run
  GET    /catalog
  GET    /audit?n=20
  GET    /audit/verify
  GET    /healthz
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .core.gateway import CDEHClient


def _json(obj: Any, status: int = 200) -> Tuple[bytes, int, str]:
    return json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"), status, "application/json"


def _err(msg: str, status: int = 400) -> Tuple[bytes, int, str]:
    return _json({"error": msg}, status=status)


class _Handler(BaseHTTPRequestHandler):
    # injected from run_server
    client: CDEHClient = None
    lock: threading.Lock = None

    def log_message(self, fmt, *args):
        pass  # silence default stderr logging

    def do_GET(self):
        path = urlparse(self.path).path
        with self.lock:
            try:
                if path == "/healthz":
                    body, status, ctype = _json({"ok": True})
                elif path == "/adapters":
                    body, status, ctype = _json(self.client.list_adapters())
                elif path.startswith("/adapters/"):
                    name = path[len("/adapters/"):]
                    body, status, ctype = _json(self.client.get_adapter_config(name))
                elif path == "/shares":
                    body, status, ctype = _json([s.to_dict() for s in self.client.share.list()])
                elif path.startswith("/shares/") and not path.endswith("/run"):
                    name = path[len("/shares/"):]
                    body, status, ctype = _json(self.client.share.get(name).to_dict())
                elif path == "/catalog":
                    body, status, ctype = _json([a.to_dict() for a in self.client.catalog.list()])
                elif path == "/audit/verify":
                    body, status, ctype = _json(self.client.audit_chain_status())
                elif path.startswith("/audit"):
                    qs = dict(x.split("=", 1) for x in urlparse(self.path).query.split("&") if "=" in x)
                    n = int(qs.get("n", 20))
                    body, status, ctype = _json(self.client.audit_tail(n))
                else:
                    body, status, ctype = _err(f"not found: {path}", 404)
            except KeyError as e:
                body, status, ctype = _err(f"not found: {e}", 404)
            except Exception as e:
                body, status, ctype = _err(str(e), 500)
        self._respond(body, status, ctype)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError as e:
            return self._respond(*_err(f"bad json: {e}", 400))
        with self.lock:
            try:
                if path == "/adapters":
                    self.client.register_adapter(data["name"], data["kind"],
                                                **{k: v for k, v in data.items()
                                                   if k not in ("name", "kind")})
                    body, status, ctype = _json({"ok": True, "name": data["name"]})
                elif path == "/shares":
                    self.client.share.create(
                        name=data["name"], source=data["source"], dest=data["dest"],
                        transform=data.get("transform", []),
                        policy=data.get("policy", "default"),
                        incremental=data.get("incremental", "etag"),
                        parallelism=data.get("parallelism", 4),
                        description=data.get("description", ""),
                    )
                    body, status, ctype = _json({"ok": True, "name": data["name"]})
                elif path.endswith("/run"):
                    name = path.split("/")[-2]
                    user = data.get("user", "http")
                    res = self.client.run_share(name, user=user)
                    body, status, ctype = _json(res.to_dict())
                else:
                    body, status, ctype = _err(f"not found: {path}", 404)
            except Exception as e:
                body, status, ctype = _err(str(e), 500)
        self._respond(body, status, ctype)

    def do_DELETE(self):
        path = urlparse(self.path).path
        with self.lock:
            try:
                if path.startswith("/adapters/"):
                    name = path[len("/adapters/"):]
                    ok = self.client.remove_adapter(name)
                    body, status, ctype = _json({"ok": ok, "name": name})
                elif path.startswith("/shares/"):
                    name = path[len("/shares/"):]
                    ok = self.client.engine.delete_share(name)
                    body, status, ctype = _json({"ok": ok, "name": name})
                else:
                    body, status, ctype = _err(f"not found: {path}", 404)
            except Exception as e:
                body, status, ctype = _err(str(e), 500)
        self._respond(body, status, ctype)

    def _respond(self, body: bytes, status: int, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(client: CDEHClient, host: str = "0.0.0.0", port: int = 8080) -> int:
    _Handler.client = client
    _Handler.lock = threading.Lock()
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"[cdeh-server] listening on http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[cdeh-server] shutting down")
    return 0