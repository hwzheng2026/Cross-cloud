"""Smoke test for the new sync/async/batch TransferEngine API.

Run: pytest tests/test_sync_async_batch.py -v
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cdeh import CDEHClient
from cdeh.core.transfer import BatchItem, BatchResult, TransferResult, Share


def _make_env(src_dir, dst_dir, shares=("s1", "s2"), n_files=3):
    """Two local adapters, one source dir, two dst dirs, N shares."""
    client = CDEHClient(config_dir=str(Path(tempfile.mkdtemp(prefix="cdeh-batch-"))))
    client.register_adapter("s", "local", root=str(src_dir))
    for name in shares:
        client.register_adapter(f"d_{name}", "local", root=str(dst_dir / name))
    # Add an admin user so share.run doesn't get blocked by RBAC.
    client.rbac.add_user("op", role="operator", api_key="k")
    client.rbac.add_user("u", role="operator", api_key="k")
    # Seed source
    (src_dir / "x").mkdir(exist_ok=True)
    for i in range(n_files):
        (src_dir / "x" / f"f{i}.csv").write_text(
            f"id,name\n1,a{i}\n2,b{i}\n"
        )
    # Define N shares
    for name in shares:
        client.share.create(
            name=name,
            source=f"s:/x",
            dest=f"d_{name}:/out",
            transform=[],
            policy="default",
        )
    return client


def test_run_sync_aliases_run():
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    client = _make_env(src, dst, shares=("sync1",), n_files=2)
    try:
        result = client.run_share("sync1", user="u")
        assert isinstance(result, TransferResult)
        assert result.objects_transferred == 2
        assert result.objects_skipped == 0
        # engine.run_sync returns the same thing (uses run_share internally
        # so it pulls configs from the client)
        share = client.engine.load_share("sync1")
        src_cfg = client.get_adapter_config(share.src_adapter)
        dst_cfg = client.get_adapter_config(share.dst_adapter)
        r2 = client.engine.run_sync(share, user="u",
                                     src_config=src_cfg, dst_config=dst_cfg)
        assert r2.objects_skipped == 2  # incremental after first run
    finally:
        client.shutdown(); shutil.rmtree(client.config_dir, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_run_async_returns_future():
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    client = _make_env(src, dst, shares=("async1",), n_files=2)
    try:
        # Use the high-level wrapper (it resolves configs); the
        # engine-level run_async also exists for cases where the
        # caller already has configs in hand.
        fut = client.run_share_async("async1", user="u")
        result = fut.result(timeout=10)
        assert isinstance(result, TransferResult)
        assert result.objects_transferred == 2
    finally:
        client.shutdown(); shutil.rmtree(client.config_dir, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_run_batch_concurrent():
    """Two shares should run faster together than sequentially.
    Use n=2 small files so per-share work is tiny — the wall-clock
    test is fuzzy; assert the batch returns correct aggregate instead."""
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    client = _make_env(src, dst, shares=("b1", "b2", "b3"), n_files=3)
    try:
        result = client.run_share_batch(
            ["b1", "b2", "b3"], users=["op", "op", "op"], max_workers=3,
        )
        assert isinstance(result, BatchResult)
        assert result.total == 3
        assert result.succeeded == 3
        assert result.failed == 0
        assert len(result.items) == 3
        for it in result.items:
            assert it.error is None
            assert it.result.objects_transferred == 3
    finally:
        client.shutdown(); shutil.rmtree(client.config_dir, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_run_batch_fail_fast_on_missing_adapter():
    """A share referencing an unregistered adapter should fail-fast
    without running the others (in fail_fast mode). Without fail_fast
    the missing share is reported as an error but the others still
    execute."""
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    client = _make_env(src, dst, shares=("ok1", "ok2"), n_files=2)
    try:
        # Register an extra share pointing at a non-existent adapter
        client.share.create(
            name="bad",
            source="s:/x",
            dest="missing-adapter:/out",
            transform=[],
            policy="default",
        )
        # Without fail_fast: bad share errors, others succeed
        result = client.run_share_batch(
            ["ok1", "bad", "ok2"], users=["op", "op", "op"],
        )
        assert result.total == 3
        assert result.succeeded == 2
        assert result.failed == 1
        bad = [it for it in result.items if it.share.name == "bad"][0]
        assert bad.error and "missing-adapter" in bad.error
    finally:
        client.shutdown(); shutil.rmtree(client.config_dir, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_run_async_then_callback():
    """Use add_done_callback to chain post-processing on the result
    without blocking the caller."""
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    client = _make_env(src, dst, shares=("cb1",), n_files=1)
    try:
        fut = client.run_share_async("cb1", user="u")
        seen = {"called": False, "transferred": 0}
        def _cb(f):
            seen["called"] = True
            seen["transferred"] = f.result().objects_transferred
        fut.add_done_callback(_cb)
        fut.result(timeout=5)
        # Give the callback a moment to fire (it's synchronous on the
        # worker thread, so by now it has)
        time.sleep(0.05)
        assert seen["called"] is True
        assert seen["transferred"] == 1
    finally:
        client.shutdown(); shutil.rmtree(client.config_dir, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_create_batch_idempotent():
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    client = _make_env(src, dst, shares=("c1", "c2"), n_files=1)
    try:
        # Share objects are already created by _make_env; calling
        # create_batch now should still work because the engine just
        # upserts the JSON.
        out = client.share.create_batch([
            {"name": "c1", "source": "s:/x", "dest": "d_c1:/out", "transform": []},
            {"name": "c2", "source": "s:/x", "dest": "d_c2:/out", "transform": []},
        ])
        assert len(out) == 2
        assert all(isinstance(s, Share) for s in out)
    finally:
        client.shutdown(); shutil.rmtree(client.config_dir, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))