"""Tests for the new C-DEH features added on top of the original
22-test smoke suite:
- checkpoint store (file + SQLite)
- compress / encrypt transformers
- per-share filters (path glob / regex / size / mtime / tag)
- retry policy
- RBAC group + department isolation
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cdeh import CDEHClient
from cdeh.transformers.compress import CompressTransformer
from cdeh.transformers.encrypt import EncryptTransformer, make_key as _test_key
from cdeh.core.checkpoint import FileCheckpointStore, SQLiteCheckpointStore
from cdeh.core.transfer import Share


# ─── checkpoint store ─────────────────────────────────────────────
def test_file_checkpoint_store_basic(tmp_path):
    cp = FileCheckpointStore(tmp_path / "ckpt")
    assert cp.get("s1", "/a.txt") is None
    cp.put_part("s1", "/a.txt", 0, 8 * 1024 * 1024)
    cp.put_part("s1", "/a.txt", 1, 4 * 1024 * 1024)
    state = cp.get("s1", "/a.txt")
    assert state["parts"]["0000"]["bytes"] == 8 * 1024 * 1024
    assert state["parts"]["0001"]["bytes"] == 4 * 1024 * 1024
    cp.mark_completed("s1", "/a.txt")
    state = cp.get("s1", "/a.txt")
    assert state["completed_at"]


def test_file_checkpoint_store_reset(tmp_path):
    cp = FileCheckpointStore(tmp_path / "ckpt")
    cp.put_part("s1", "/a.txt", 0, 1024)
    cp.put_part("s1", "/b.txt", 0, 2048)
    assert cp.reset("s1", "/a.txt") == 1
    assert cp.get("s1", "/a.txt") is None
    assert cp.get("s1", "/b.txt") is not None
    assert cp.reset("s1") == 1  # drop the remaining one
    assert cp.get("s1", "/b.txt") is None


def test_sqlite_checkpoint_store_basic(tmp_path):
    db = tmp_path / "ckpt.db"
    cp = SQLiteCheckpointStore(db)
    cp.put_part("s1", "/x.csv", 0, 100)
    cp.put_part("s1", "/x.csv", 1, 200)
    cp.mark_completed("s1", "/x.csv")
    state = cp.get("s1", "/x.csv")
    assert state["parts"]["0000"]["bytes"] == 100
    assert state["parts"]["0001"]["bytes"] == 200
    assert state["completed_at"]


# ─── compress transformer ────────────────────────────────────────
def test_compress_gzip_round_trip():
    import gzip
    t = CompressTransformer.from_config({"algo": "gzip", "level": 6})
    big = b"hello world " * 1000
    out = t.transform(big, params={})
    assert len(out) < len(big)
    assert gzip.decompress(out) == big


def test_compress_zstd_graceful_degrade_without_pkg():
    """If zstandard isn't installed, fall back to gzip rather than fail."""
    t = CompressTransformer.from_config({"algo": "zstd", "level": 3})
    big = b"hello world " * 1000
    out = t.transform(big, params={})
    # Should still produce a valid gzip blob
    import gzip
    assert gzip.decompress(out) == big


# ─── encrypt transformer ──────────────────────────────────────────
def test_encrypt_round_trip():
    key = _test_key()
    t = EncryptTransformer(key)
    msg = b"top secret PII payload"
    blob = t.transform(msg, params={})
    # ciphertext ≠ plaintext
    assert blob != msg
    # and is at least nonce + tag overhead longer
    assert len(blob) == len(msg) + 12 + 16
    # decrypt restores plaintext
    assert t.inverse(blob) == msg


def test_encrypt_env_key(monkeypatch):
    key_hex = _test_key().hex()
    monkeypatch.setenv("TEST_ENCRYPT_KEY", key_hex)
    t = EncryptTransformer.from_config({"key": "env:TEST_ENCRYPT_KEY"})
    assert t.key.hex() == key_hex


def test_encrypt_bad_key_length_rejected():
    with pytest.raises(ValueError):
        EncryptTransformer(b"short")


# ─── per-share filters (path glob / regex / size / mtime / tag) ──
def test_filter_path_glob_excludes_files():
    """A share with path_glob='*.csv' should only transfer CSVs."""
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    (src / "data").mkdir()
    (src / "data" / "a.csv").write_text("a")
    (src / "data" / "b.json").write_text("{}")
    (src / "data" / "c.csv").write_text("c")

    client = CDEHClient(config_dir=str(Path(tempfile.mkdtemp())))
    client.register_adapter("s", "local", root=str(src))
    client.register_adapter("d", "local", root=str(dst))
    client.rbac.add_user("op", role="operator")
    client.share.create("x", source="s:/data", dest="d:/out",
                         transform=[], policy="default",
                         filters=[{"path_glob": "*.csv"}])
    res = client.run_share("x", user="op")
    assert res.objects_transferred == 2  # only .csv
    assert (dst / "out" / "a.csv").exists()
    assert not (dst / "out" / "b.json").exists()
    assert (dst / "out" / "c.csv").exists()
    # audit recorded the filtered-out files
    actions = [e["action"] for e in client.audit_tail(20)]
    assert actions.count("share.filtered_out") == 1


def test_filter_size_window():
    """size_min / size_max should bound the bytes that move."""
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    (src / "d").mkdir()
    (src / "d" / "tiny.txt").write_text("a")
    (src / "d" / "medium.txt").write_text("x" * 100)
    (src / "d" / "huge.txt").write_text("x" * 1000)
    client = CDEHClient(config_dir=str(Path(tempfile.mkdtemp())))
    client.register_adapter("s", "local", root=str(src))
    client.register_adapter("d", "local", root=str(dst))
    client.rbac.add_user("op", role="operator")
    client.share.create("x", source="s:/d", dest="d:/out",
                         transform=[], policy="default",
                         filters=[{"size_min": 50, "size_max": 500}])
    res = client.run_share("x", user="op")
    assert res.objects_transferred == 1
    assert (dst / "out" / "medium.txt").exists()


def test_filter_mtime_after():
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    (src / "d").mkdir()
    old_file = src / "d" / "old.txt"
    new_file = src / "d" / "new.txt"
    old_file.write_text("old")
    new_file.write_text("new")
    # backdate old_file to last year
    import os as _os
    last_year = time.time() - 365 * 86400
    _os.utime(old_file, (last_year, last_year))
    client = CDEHClient(config_dir=str(Path(tempfile.mkdtemp())))
    client.register_adapter("s", "local", root=str(src))
    client.register_adapter("d", "local", root=str(dst))
    client.rbac.add_user("op", role="operator")
    yesterday = time.strftime("%Y-%m-%d",
                                time.gmtime(time.time() - 86400))
    client.share.create("x", source="s:/d", dest="d:/out",
                         transform=[], policy="default",
                         filters=[{"mtime_after": yesterday}])
    res = client.run_share("x", user="op")
    assert res.objects_transferred == 1
    assert (dst / "out" / "new.txt").exists()
    assert not (dst / "out" / "old.txt").exists()


# ─── retry policy ────────────────────────────────────────────────
def test_retry_policy_eventually_succeeds(monkeypatch):
    """With max_attempts=3 and a flaky adapter, the third attempt
    succeeds; the audit log records the two retries."""
    src = Path(tempfile.mkdtemp()); dst = Path(tempfile.mkdtemp())
    (src / "x").mkdir(); (src / "x" / "a.csv").write_text("a,b\n1,2\n")
    client = CDEHClient(config_dir=str(Path(tempfile.mkdtemp())))
    client.register_adapter("s", "local", root=str(src))
    client.register_adapter("d", "local", root=str(dst))
    client.rbac.add_user("op", role="operator")

    s = Share(name="x", src_adapter="s", src_path="/x", dst_adapter="d",
              dst_path="/out", transforms=[], policy="default",
              retry={"max_attempts": 3, "initial_backoff_seconds": 0.01,
                     "backoff_factor": 1.0})
    client.engine.save_share(s)
    # Patch _transfer_one to fail twice then succeed.
    from cdeh.core.transfer import TransferEngine
    original = TransferEngine._transfer_one
    state = {"calls": 0}
    def flaky(self, src, src_path, dst, dst_path, transformers):
        state["calls"] += 1
        if state["calls"] < 3:
            raise RuntimeError("simulated network glitch")
        return original(self, src, src_path, dst, dst_path, transformers)
    monkeypatch.setattr(TransferEngine, "_transfer_one", flaky)
    res = client.run_share("x", user="op")
    assert state["calls"] == 3
    assert res.objects_transferred == 1
    actions = [e["action"] for e in client.audit_tail(20)]
    assert actions.count("share.transfer.retry") == 2


# ─── RBAC group + department ──────────────────────────────────────
def test_rbac_department_isolation_blocks_cross_dept_access():
    r = client_rbac()
    r.add_user("sales_alice", role="operator",
               department="sales", groups=["data-platform"])
    r.add_user("eng_bob",     role="operator",
               department="engineering", groups=["data-platform"])
    # asset tagged dept:sales — Alice (sales) sees it, Bob (eng) doesn't
    assert r.check("sales_alice", "view_catalog",
                    asset_tags=["dept:sales", "gdpr-cleared"])
    assert not r.check("eng_bob", "view_catalog",
                       asset_tags=["dept:sales"])
    # asset without dept tag — both see it
    assert r.check("sales_alice", "view_catalog", asset_tags=["public"])
    assert r.check("eng_bob", "view_catalog", asset_tags=["public"])


def test_rbac_admin_overrides_department():
    r = client_rbac()
    r.add_user("ceo", role="admin", department="")
    assert r.check("ceo", "view_catalog",
                    asset_tags=["dept:finance", "dept:sales"])


def test_rbac_add_to_group_idempotent():
    r = client_rbac()
    r.add_user("alice", role="viewer")
    r.add_to_group("alice", "data-platform")
    r.add_to_group("alice", "data-platform")  # dup
    u = r.get("alice")
    assert "data-platform" in u.groups


def test_rbac_asset_acl_still_works():
    """Per-asset ACL is unchanged — still applies even if department
    isolation would pass."""
    r = client_rbac()
    r.add_user("alice", role="operator",
               department="sales",
               allowed_assets=["public-asset"])
    # Public asset she's allowed to see
    assert r.check("alice", "view_catalog", asset_name="public-asset",
                    asset_tags=["dept:sales"])
    # Same dept but asset not in her ACL
    assert not r.check("alice", "view_catalog", asset_name="secret-asset",
                       asset_tags=["dept:sales"])


def client_rbac():
    """Helper: build a fresh RBAC backed by a temp file."""
    p = Path(tempfile.mkdtemp())
    from cdeh.core.auth import RBAC
    return RBAC(path=str(p / "users.json"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))