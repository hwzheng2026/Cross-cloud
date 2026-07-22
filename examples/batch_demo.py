"""Batch-mode demo: 3 independent share jobs running in parallel.

Demonstrates the new sync/async/batch API surface. The batch runs
all three shares concurrently (max_workers=3) and reports per-share
outcomes + aggregate counts.

Run:
    python3 examples/batch_demo.py
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cdeh import CDEHClient


def main():
    cfg_dir = Path(tempfile.mkdtemp(prefix="cdeh-batch-demo-"))
    src = Path(tempfile.mkdtemp(prefix="cdeh-batch-src-"))
    dst_root = Path(tempfile.mkdtemp(prefix="cdeh-batch-dst-"))

    # Source cloud: 3 files
    (src / "orders").mkdir()
    for i, name in enumerate(["monday", "tuesday", "wednesday"]):
        (src / "orders" / f"{name}.csv").write_text(
            f"order_id,customer,amount\n1,alice,{100 + i}\n2,bob,{200 + i}\n"
        )

    client = CDEHClient(config_dir=str(cfg_dir))
    client.register_adapter("src-cloud", "local", root=str(src))
    for tgt in ("alicloud", "aws", "azure"):
        client.register_adapter(tgt, "local",
                                 root=str(dst_root / tgt))
    client.rbac.add_user("batch-op", role="operator", api_key="k")

    # Three independent shares — one per target cloud
    print("[demo] defining 3 shares (one per target cloud)...")
    client.share.create_batch([
        {"name": "to-alicloud",  "source": "src-cloud:/orders",
         "dest": "alicloud:/incoming",  "transform": ["mask:customer"], "policy": "gdpr-strict"},
        {"name": "to-aws",      "source": "src-cloud:/orders",
         "dest": "aws:/incoming",       "transform": ["mask:customer"], "policy": "gdpr-strict"},
        {"name": "to-azure",    "source": "src-cloud:/orders",
         "dest": "azure:/incoming",     "transform": ["mask:customer"], "policy": "gdpr-strict"},
    ])
    print(f"[demo] shares: {client.share.list()}")

    # 1. Sync: run them sequentially
    print("\n[demo] === sync: run sequentially ===")
    t0 = time.time()
    for name in ("to-alicloud", "to-aws", "to-azure"):
        r = client.run_share(name, user="batch-op")
        print(f"  {name}: {r.objects_transferred}t / {r.objects_skipped}s / {len(r.errors)}err")
    print(f"  total sync: {time.time() - t0:.2f}s")

    # 2. Batch: run concurrently (max_workers=3, fully parallel)
    print("\n[demo] === batch: run concurrently (max_workers=3) ===")
    t0 = time.time()
    batch = client.run_share_batch(
        ["to-alicloud", "to-aws", "to-azure"],
        users=["batch-op"] * 3,
        max_workers=3,
    )
    print(f"  total: {batch.total}  succeeded: {batch.succeeded}  failed: {batch.failed}")
    print(f"  duration: {batch.finished_at - batch.started_at:.2f}s")
    for it in batch.items:
        r = it.result
        print(f"  {it.share.name}: {r.objects_transferred}t / {r.objects_skipped}s / "
              f"{len(r.errors)}err  (user={it.user})")
    print(f"  total batch: {time.time() - t0:.2f}s")

    # 3. Show one masked file
    print(f"\n[demo] sample masked content ({dst_root}/alicloud/incoming/monday.csv):")
    print((dst_root / "alicloud" / "incoming" / "monday.csv").read_text())

    # 4. Audit chain integrity
    print(f"[demo] audit chain integrity: {client.audit_chain_status()}")

    # cleanup
    client.shutdown()
    for p in (cfg_dir, src, dst_root):
        shutil.rmtree(p, ignore_errors=True)
    print("\n[demo] ✓ done")


if __name__ == "__main__":
    main()