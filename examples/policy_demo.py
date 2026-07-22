"""Demonstrate RBAC + policy enforcement.

Three users with different roles hit the same share. We expect:
  - viewer Alice: blocked at RBAC (no run_share permission)
  - operator Bob: allowed by RBAC, but gdpr-strict requires `mask`
                  transform — if we drop the transform, policy denies
  - admin Carol: allowed, with full audit

Run:
    python3 examples/policy_demo.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cdeh import CDEHClient


def main():
    cfg_dir = Path(tempfile.mkdtemp(prefix="cdeh-poldemo-"))
    src = Path(tempfile.mkdtemp(prefix="src-"))
    dst = Path(tempfile.mkdtemp(prefix="dst-"))
    (src / "data").mkdir()
    (src / "data" / "users.csv").write_text(
        "id,email,phone\n1,a@x.com,555\n2,b@x.com,666\n")

    client = CDEHClient(config_dir=str(cfg_dir))
    client.register_adapter("a", "local", root=str(src))
    client.register_adapter("b", "local", root=str(dst))
    client.rbac.add_user("alice", role="viewer", api_key="k1")
    client.rbac.add_user("bob",   role="operator", api_key="k2")
    client.rbac.add_user("carol", role="admin", api_key="k3")

    # share 1: with mask transform (compliant)
    client.share.create("compliant", source="a:/data", dest="b:/out",
                        transform=["mask:email,phone"], policy="gdpr-strict")
    # share 2: NO transform (will be policy-blocked under gdpr-strict)
    client.share.create("uncompliant", source="a:/data", dest="b:/out",
                        transform=[], policy="gdpr-strict")

    print("=== Alice (viewer) tries to run compliant share ===")
    res = client.run_share("compliant", user="alice")
    print(f"  errors: {res.errors}")
    print(f"  objects: {res.objects_transferred}")

    print("\n=== Bob (operator) runs uncompliant share (no mask) ===")
    res = client.run_share("uncompliant", user="bob")
    print(f"  errors: {res.errors}")
    print(f"  objects: {res.objects_transferred}")

    print("\n=== Carol (admin) runs compliant share ===")
    res = client.run_share("compliant", user="carol")
    print(f"  errors: {res.errors}")
    print(f"  bytes:   {res.bytes_transferred}")
    print(f"  objects: {res.objects_transferred}")

    print("\n=== Audit tail ===")
    for e in client.audit_tail(5):
        print(f"  {e.get('ts',''):<28} {e.get('action',''):<26} "
              f"user={e.get('user',''):<6} ok={e.get('success', True)}")
    print(f"\n  chain integrity: {client.audit_chain_status()}")

    shutil.rmtree(cfg_dir, ignore_errors=True)
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(dst, ignore_errors=True)


if __name__ == "__main__":
    main()