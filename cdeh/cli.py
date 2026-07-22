"""CDEH CLI — `cdeh <subcommand>`.

Subcommands:
  adapter list | register | remove | get | ping
  share    list | create | get | delete | run
  catalog  list | register | show | delete
  policy   list | add | show
  user     list | add | show
  audit    tail | verify
  serve              (start the HTTP gateway)
"""
import argparse
import json
import sys
from typing import Any, Dict

from . import CDEHClient


def _pp(obj: Any) -> str:
    if isinstance(obj, list):
        return "\n".join(_pp(o) for o in obj)
    if isinstance(obj, dict):
        return json.dumps(obj, indent=2, ensure_ascii=False)
    return str(obj)


def cmd_adapter_register(args, client: CDEHClient) -> int:
    cfg = _parse_kv_args(args.adapter_arg)
    client.register_adapter(args.name, args.kind, **cfg)
    print(f"registered adapter {args.name!r} (kind={args.kind})")
    return 0


def cmd_adapter_list(args, client: CDEHClient) -> int:
    rows = client.list_adapters()
    if not rows:
        print("(no adapters registered)")
        return 0
    print(f"{'NAME':<24} {'KIND':<16} ENDPOINT")
    for name, cfg in rows.items():
        print(f"{name:<24} {cfg.get('kind',''):<16} {cfg.get('endpoint') or cfg.get('host') or ''}")
    return 0


def cmd_adapter_remove(args, client: CDEHClient) -> int:
    if client.remove_adapter(args.name):
        print(f"removed {args.name!r}")
    else:
        print(f"no such adapter: {args.name}")
    return 1


def cmd_adapter_get(args, client: CDEHClient) -> int:
    print(_pp(client.get_adapter_config(args.name)))
    return 0


def cmd_adapter_ping(args, client: CDEHClient) -> int:
    from .adapters import registry
    cfg = client.get_adapter_config(args.name)
    adapter = registry.get(cfg["kind"]).from_config({k: v for k, v in cfg.items() if k != "kind"})
    print(_pp(adapter.ping()))
    return 0


def cmd_share_create(args, client: CDEHClient) -> int:
    transforms = _split_csv(args.transform) if args.transform else []
    client.share.create(
        name=args.name, source=args.source, dest=args.dest,
        transform=transforms, policy=args.policy or "default",
        incremental=args.incremental or "etag",
        parallelism=args.parallelism or 4,
        description=args.description or "",
    )
    print(f"defined share {args.name!r}")
    return 0


def cmd_share_list(args, client: CDEHClient) -> int:
    for s in client.share.list():
        print(f"  {s.name:<28} {s.src_adapter}:{s.src_path}  →  {s.dst_adapter}:{s.dst_path}  "
              f"[policy={s.policy}, inc={s.incremental}, transforms={s.transforms}]")
    return 0


def cmd_share_run(args, client: CDEHClient) -> int:
    res = client.share.run(args.name, user=args.user or "cli")
    print(_pp(res))
    return 0 if not res.get("errors") else 1


def cmd_share_delete(args, client: CDEHClient) -> int:
    if client.engine.delete_share(args.name):
        print(f"deleted {args.name!r}")
    else:
        print(f"no such share: {args.name}")
        return 1
    return 0


def cmd_catalog_list(args, client: CDEHClient) -> int:
    items = client.catalog.list()
    if not items:
        print("(catalog empty)")
        return 0
    for a in items:
        print(f"  {a.name:<32} {a.classification:<12} tags={a.tags}")
    return 0


def cmd_catalog_register(args, client: CDEHClient) -> int:
    from .core.catalog import DataAsset
    asset = DataAsset(
        name=args.name, adapter=args.adapter, path=args.path,
        classification=args.classification or "internal",
        tags=_split_csv(args.tags) if args.tags else [],
    )
    client.catalog.register(asset)
    print(f"registered {args.name!r}")
    return 0


def cmd_audit_tail(args, client: CDEHClient) -> int:
    entries = client.audit_tail(args.n or 20)
    for e in entries:
        print(f"  {e.get('ts','')}  {e.get('action',''):<22} "
              f"user={e.get('user','')}  asset={e.get('asset','')}  "
              f"hash={e.get('entry_hash','')}")
    return 0


def cmd_audit_verify(args, client: CDEHClient) -> int:
    status = client.audit_chain_status()
    print(_pp(status))
    return 0 if status.get("ok") else 1


def cmd_user_add(args, client: CDEHClient) -> int:
    from .core.auth import RBAC
    user = client.rbac.add_user(
        name=args.name, role=args.role or "viewer",
        api_key=args.api_key or "", allowed_assets=_split_csv(args.assets) if args.assets else None,
    )
    print(f"added user {user.name!r} (role={user.role})")
    return 0


def cmd_user_list(args, client: CDEHClient) -> int:
    for u in client.rbac.list_users():
        print(f"  {u.name:<24} role={u.role:<10} assets={sorted(u.allowed_assets) or '*'}")
    return 0


def cmd_serve(args, client: CDEHClient) -> int:
    from .server import run_server
    return run_server(client, host=args.host, port=args.port)


# ─── helpers ────────────────────────────────────────────────────────
def _split_csv(s: str):
    return [x.strip() for x in s.split(",") if x.strip()]


def _parse_kv_args(items) -> Dict[str, Any]:
    """Parse `--adapter-arg key=value` repeated flags."""
    out = {}
    for kv in items or []:
        if "=" in kv:
            k, _, v = kv.partition("=")
            out[k.strip()] = v.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cdeh", description="Cross-Cloud Data Exchange Hub CLI")
    p.add_argument("--config-dir", default=None, help="override ~/.cdeh")
    sub = p.add_subparsers(dest="cmd", required=True)

    # adapter
    a = sub.add_parser("adapter", help="manage adapters").add_subparsers(dest="sub", required=True)
    a_add = a.add_parser("register", help="register a new adapter")
    a_add.add_argument("name")
    a_add.add_argument("kind", choices=["s3", "azure_blob", "sftp", "mysql", "local"])
    a_add.add_argument("--adapter-arg", action="append", default=[],
                       help="key=value (repeatable). e.g. --adapter-arg endpoint=https://s3.amazonaws.com --adapter-arg bucket=my-bucket")
    a_add.set_defaults(func=cmd_adapter_register)
    a.add_parser("list").set_defaults(func=cmd_adapter_list)
    a_rm = a.add_parser("remove"); a_rm.add_argument("name"); a_rm.set_defaults(func=cmd_adapter_remove)
    a_get = a.add_parser("get"); a_get.add_argument("name"); a_get.set_defaults(func=cmd_adapter_get)
    a_ping = a.add_parser("ping"); a_ping.add_argument("name"); a_ping.set_defaults(func=cmd_adapter_ping)

    # share
    s = sub.add_parser("share", help="manage data-share jobs").add_subparsers(dest="sub", required=True)
    s_new = s.add_parser("create", help="create a new share")
    s_new.add_argument("name")
    s_new.add_argument("--source", required=True, help="adapter:/path")
    s_new.add_argument("--dest", required=True, help="adapter:/path")
    s_new.add_argument("--transform", help="comma list, e.g. 'mask:email,phone,codec:csv:parquet'")
    s_new.add_argument("--policy", default="default")
    s_new.add_argument("--incremental", default="etag", choices=["etag", "mtime", "none"])
    s_new.add_argument("--parallelism", type=int, default=4)
    s_new.add_argument("--description", default="")
    s_new.set_defaults(func=cmd_share_create)
    s.add_parser("list").set_defaults(func=cmd_share_list)
    s_run = s.add_parser("run"); s_run.add_argument("name"); s_run.add_argument("--user", default="cli")
    s_run.set_defaults(func=cmd_share_run)
    s_del = s.add_parser("delete"); s_del.add_argument("name"); s_del.set_defaults(func=cmd_share_delete)

    # catalog
    c = sub.add_parser("catalog", help="data catalog").add_subparsers(dest="sub", required=True)
    c.add_parser("list").set_defaults(func=cmd_catalog_list)
    c_new = c.add_parser("register")
    c_new.add_argument("name"); c_new.add_argument("--adapter", required=True)
    c_new.add_argument("--path", required=True)
    c_new.add_argument("--classification", default="internal",
                       choices=["public", "internal", "confidential", "restricted"])
    c_new.add_argument("--tags", help="comma list")
    c_new.set_defaults(func=cmd_catalog_register)

    # user
    u = sub.add_parser("user", help="users & RBAC").add_subparsers(dest="sub", required=True)
    u.add_parser("list").set_defaults(func=cmd_user_list)
    u_new = u.add_parser("add")
    u_new.add_argument("name"); u_new.add_argument("--role", default="viewer",
                       choices=["admin", "operator", "viewer"])
    u_new.add_argument("--api-key", default="")
    u_new.add_argument("--assets", help="comma list of asset names this user can see")
    u_new.set_defaults(func=cmd_user_add)

    # audit
    au = sub.add_parser("audit", help="audit log").add_subparsers(dest="sub", required=True)
    au_tail = au.add_parser("tail"); au_tail.add_argument("-n", type=int, default=20)
    au_tail.set_defaults(func=cmd_audit_tail)
    au.add_parser("verify").set_defaults(func=cmd_audit_verify)

    # serve
    sv = sub.add_parser("serve", help="start the HTTP gateway")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = CDEHClient(config_dir=args.config_dir)
    return args.func(args, client)


if __name__ == "__main__":
    sys.exit(main())