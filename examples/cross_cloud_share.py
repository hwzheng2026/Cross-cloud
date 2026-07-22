"""End-to-end cross-cloud demo using two local adapters.

This simulates an AWS S3 → Aliyun OSS transfer without needing real
cloud credentials. The two `LocalAdapter` instances point at separate
directories and the TransferEngine uses the same code path that
production uses for S3 / OSS / MinIO.

Run:
    python3 examples/cross_cloud_share.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cdeh import CDEHClient
from cdeh.core.transfer import Share


def main():
    # Isolated config dir so we don't clobber your real ~/.cdeh
    cfg_dir = Path(tempfile.mkdtemp(prefix="cdeh-demo-"))
    print(f"[demo] config dir: {cfg_dir}")

    # Two "clouds" simulated as two local directories
    src_dir = Path(tempfile.mkdtemp(prefix="cdeh-source-"))
    dst_dir = Path(tempfile.mkdtemp(prefix="cdeh-target-"))
    print(f"[demo] source cloud: {src_dir}")
    print(f"[demo] target cloud: {dst_dir}")

    # Seed source cloud
    (src_dir / "orders").mkdir()
    (src_dir / "orders" / "2024-Q1.csv").write_text(
        "id,name,email,phone\n"
        "1,Alice,alice@example.com,555-0100\n"
        "2,Bob,bob@example.com,555-0101\n"
        "3,Carol,carol@example.com,555-0102\n"
    )
    (src_dir / "orders" / "2024-Q2.csv").write_text(
        "id,name,email,phone\n"
        "4,Dave,dave@example.com,555-0103\n"
        "5,Eve,eve@example.com,555-0104\n"
    )

    # Wire up the client
    client = CDEHClient(config_dir=str(cfg_dir))
    client.register_adapter("aws-prod", "local", root=str(src_dir))
    client.register_adapter("oss-prod", "local", root=str(dst_dir))
    client.rbac.add_user("demo-operator", role="operator", api_key="demopass")

    # Define a share: AWS → OSS, mask PII, GDPR-strict policy
    client.share.create(
        name="daily-orders",
        source="aws-prod:/orders",
        dest="oss-prod:/incoming",
        transform=["mask:email,phone"],
        policy="gdpr-strict",
        incremental="etag",
        parallelism=2,
        description="Daily customer order export with PII masking",
    )

    # First run — should transfer 2 files, mask email/phone
    print("\n[demo] === first run (cold) ===")
    res = client.run_share("daily-orders", user="demo-operator")
    print(f"  bytes:   {res.bytes_transferred}")
    print(f"  objects: {res.objects_transferred} transferred, {res.objects_skipped} skipped")
    print(f"  errors:  {res.errors or 'none'}")
    print(f"  duration: {res.finished_at - res.started_at:.2f}s")

    # Show what landed on the target
    print(f"\n[demo] target cloud after first run:")
    for p in sorted(dst_dir.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(dst_dir)}: {p.read_text()[:100]!r}")

    # Second run — incremental: should skip both files (etag unchanged)
    print("\n[demo] === second run (incremental, no source changes) ===")
    res2 = client.run_share("daily-orders", user="demo-operator")
    print(f"  objects: {res2.objects_transferred} transferred, {res2.objects_skipped} skipped")

    # Third run — modify a source file
    print("\n[demo] === third run (after Q1 changed) ===")
    (src_dir / "orders" / "2024-Q1.csv").write_text(
        "id,name,email,phone\n"
        "1,Alice,alice@example.com,555-0100\n"
        "2,Bob,bob@example.com,555-0101\n"
        "3,Carol,carol@example.com,555-0102\n"
        "99,Frank,frank@example.com,555-9999\n"  # new row
    )
    res3 = client.run_share("daily-orders", user="demo-operator")
    print(f"  objects: {res3.objects_transferred} transferred, {res3.objects_skipped} skipped")

    # Audit chain verification
    print("\n[demo] === audit log ===")
    print(f"  chain OK: {client.audit_chain_status()}")
    for entry in client.audit_tail(6):
        print(f"  {entry.get('ts',''):<28} {entry.get('action',''):<22} "
              f"asset={entry.get('asset','')} bytes={entry.get('bytes', 0)}")

    # Cleanup
    shutil.rmtree(cfg_dir, ignore_errors=True)
    shutil.rmtree(src_dir, ignore_errors=True)
    shutil.rmtree(dst_dir, ignore_errors=True)
    print("\n[demo] ✓ done")


if __name__ == "__main__":
    main()