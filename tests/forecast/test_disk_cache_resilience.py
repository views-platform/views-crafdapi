"""Safety net for the disk cache: a value entry that cannot be reconstructed — e.g. a
truncated/partial value directory — must degrade gracefully to a re-download (`read()`
returns None), never crash the request. Post-S5 the cache reconstructs from a VALUE
directory (no `pickle.load`, C-149)."""

import json
import time

import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.managers import disk_cache as dc
from views_crafdapi.managers.disk_cache import CACHE_SCHEMA_VERSION, CrafdDiskCacheManager

pytestmark = pytest.mark.layer4_infra


def test_read_returns_none_on_corrupt_value_dir(tmp_path):
    mgr = CrafdDiskCacheManager(tmp_path)
    key, cat = "apikeyhash", "forecast"

    # Valid metadata (passes the schema-version + TTL gates) ...
    mgr._meta_path(key, cat).write_text(
        json.dumps({"schema_version": CACHE_SCHEMA_VERSION, "timestamp": time.time(), "file_id": "file_001"})
    )
    # ... but a value directory with no manifest (stands in for a torn/partial write).
    mgr._value_dir(key, cat).mkdir()

    assert mgr.read(key, cat) is None  # graceful, no exception


def test_read_returns_none_on_schema_version_mismatch(tmp_path):
    mgr = CrafdDiskCacheManager(tmp_path)
    key, cat = "apikeyhash", "forecast"
    mgr._meta_path(key, cat).write_text(
        json.dumps({"schema_version": CACHE_SCHEMA_VERSION + 999, "timestamp": time.time(), "file_id": "f"})
    )
    mgr._value_dir(key, cat).mkdir()  # exists so the version gate is reached
    assert mgr.read(key, cat) is None  # version bump invalidates -> re-download


def test_disk_cache_does_not_import_pickle():
    """C-149: the cache read path must carry no `pickle.load` surface."""
    assert not hasattr(dc, "pickle"), "disk_cache must not import pickle"


def test_read_never_calls_pickle_load(tmp_path, monkeypatch):
    """Even with a populated cache, a real read reconstructs from the value dir and never
    touches `pickle.load` (C-149 regression guard)."""
    import pickle

    def _boom(*a, **k):
        raise AssertionError("pickle.load must never be called on the cache read path")

    monkeypatch.setattr(pickle, "load", _boom)
    mgr = CrafdDiskCacheManager(tmp_path)
    ds = ForecastDataset(make_fao_df(seed=4))
    assert mgr.write("k", "forecast", ds, "fid")
    result = mgr.read("k", "forecast")  # would raise if pickle.load were called
    assert result is not None and result["file_id"] == "fid"


def test_salt_lock_timeout_degrades_to_ephemeral_never_raises(tmp_path):
    """#341 review finding 1: if the partition salt cannot be read/created (disk read-only, ENOSPC,
    or a lock timeout), `_salt` must degrade to a process-local ephemeral salt and NOT raise — so
    `_partition` (and therefore read/check_file_id/write on the serve path) can never 500 on a mere
    cache-infra problem. Pre-fix this raised out of a pure path computation."""
    import filelock
    from unittest.mock import patch

    cache = CrafdDiskCacheManager(tmp_path)
    cache._salt_cache = None  # force a fresh resolution under the fault
    with patch.object(filelock.FileLock, "acquire", side_effect=filelock.Timeout("salt")):
        salt = cache._salt()  # must not raise
        assert isinstance(salt, bytes) and len(salt) == 32
        # and the public serve-path methods still degrade to a graceful miss, never raise
        assert cache.read("some_key", "forecast") is None
        assert cache.check_file_id("some_key", "forecast", "fid") is False


def test_a_crash_between_replacing_the_data_and_writing_its_meta_never_relabels_a_run(
    tmp_path, monkeypatch
):
    """C-361 (#157). Run A is cached. Run B's adopt is killed after its value-dir is in place but
    before its meta.json is written. The slot must then read as a miss — never as B's arrays under
    A's file id and provenance, which the last-good fallback (no identity check) would serve."""
    cache = CrafdDiskCacheManager(tmp_path)
    a = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=8, seed=1))
    b = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=12, seed=2))
    assert cache.write("h", "forecast", a, "file_A", source_kind="wire",
                       provenance={"file_id": "file_A"})

    real_replace = dc.os.replace

    def replace_then_die(src, dst):
        real_replace(src, dst)
        if str(dst).endswith("_value"):        # B's arrays are now in the slot ...
            raise OSError("process killed")   # ... and its meta.json was never written

    monkeypatch.setattr(dc.os, "replace", replace_then_die)
    assert cache.write("h", "forecast", b, "file_B", source_kind="wire",
                       provenance={"file_id": "file_B"}) is False
    monkeypatch.undo()

    assert cache.read("h", "forecast", enforce_ttl=False) is None
    assert cache.check_file_id("h", "forecast", "file_A") is False


def test_the_same_crash_through_the_wire_adopt_path_never_relabels_a_run(tmp_path, monkeypatch):
    """C-361 (#157) on the path production actually uses: `write_value_dir`, which adopts the wire
    assembler's staged value-dir. Kill it after the data swap, before the meta write."""
    cache = CrafdDiskCacheManager(tmp_path)
    a = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=8, seed=1))
    b = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=12, seed=2))
    assert cache.write("h", "forecast", a, "file_A", source_kind="wire",
                       provenance={"file_id": "file_A"})
    staged = cache.staging_dir("h", "forecast")
    b.to_value(staged)

    real_replace = dc.os.replace

    def replace_then_die(src, dst):
        real_replace(src, dst)
        if str(dst).endswith("_value"):
            raise OSError("process killed")

    monkeypatch.setattr(dc.os, "replace", replace_then_die)
    assert cache.write_value_dir("h", "forecast", staged, "file_B", rows=len(b.dataframe),
                                 source_kind="wire", provenance={"file_id": "file_B"}) is False
    monkeypatch.undo()

    assert cache.read("h", "forecast", enforce_ttl=False) is None
    assert cache.check_file_id("h", "forecast", "file_A") is False


def test_a_meta_write_that_fails_halfway_is_never_visible_and_leaves_nothing_behind(
    tmp_path, monkeypatch
):
    """The sidecar is written to a temporary name and renamed into place. While it is being written
    nothing exists under the final name (so the lock-free `check_file_id` can never read a torn
    file), and if the write fails the temporary file is removed."""
    cache = CrafdDiskCacheManager(tmp_path)
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=8, seed=1))
    meta_path = cache._meta_path("h", "forecast")
    seen_mid_write = []

    def torn_dump(obj, f):
        f.write('{"schema_version": ')                 # half a document ...
        f.flush()
        seen_mid_write.append(meta_path.exists())       # ... visible under the final name?
        raise OSError("No space left on device")

    monkeypatch.setattr(dc.json, "dump", torn_dump)
    assert cache.write("h", "forecast", ds, "file_A") is False
    monkeypatch.undo()

    assert seen_mid_write == [False]
    assert not list(tmp_path.glob("*_meta.json.tmp"))
    assert cache.read("h", "forecast", enforce_ttl=False) is None


# ── C-277 (#168): a disk read re-runs the C-72 gate ingest ran ─────────────────────────────────
def _written_slot(tmp_path):
    mgr = CrafdDiskCacheManager(tmp_path)
    assert mgr.write("k", "forecast", ForecastDataset(make_fao_df(seed=5)), "fid")
    return mgr, mgr._value_dir("k", "forecast")


def _poke_sample(value_dir, value):
    import numpy as np

    path = next((value_dir / "frames").glob("*/values.npy"))
    block = np.load(path, mmap_mode="r+")
    block[-1, -1] = value
    block.flush()
    del block


def _poke_geo(value_dir, col, value):
    import pandas as pd

    geo_path = value_dir / "geo.parquet"
    geo = pd.read_parquet(geo_path)
    geo.iloc[0, geo.columns.get_loc(col)] = value
    geo.to_parquet(geo_path)


@pytest.mark.parametrize("corrupt, reason", [
    (lambda vd: _poke_sample(vd, float("nan")), "non-finite"),
    (lambda vd: _poke_sample(vd, -1.0), "negative"),
    (lambda vd: _poke_geo(vd, "pg_ycoord", 999.0), "pg_ycoord"),
], ids=["nan-sample", "negative-sample", "bad-coordinate"])
def test_an_implausible_slot_is_never_served_and_is_kept(tmp_path, caplog, corrupt, reason):
    """The bytes on disk passed C-72 when written; nothing re-checked them on read. A slot that
    now fails it is refused (None) on every read and logged at error with the reason. It is
    kept: the next ingest overwrites it, and until then it is the evidence (and, if a rule was
    tightened, the only copy of the last-good run)."""
    import logging

    mgr, value_dir = _written_slot(tmp_path)
    assert mgr.read("k", "forecast") is not None  # the untouched slot is served
    corrupt(value_dir)
    with caplog.at_level(logging.ERROR, logger="views_crafdapi.managers.disk_cache"):
        assert mgr.read("k", "forecast", enforce_ttl=False) is None
        assert mgr.read("k", "forecast") is None   # refused again, not served on a second read
    assert any(r.levelno == logging.ERROR and reason in r.getMessage()
               and "plausibility gate" in r.getMessage() and "kept for inspection" in r.getMessage()
               for r in caplog.records)
    assert value_dir.exists() and mgr._meta_path("k", "forecast").exists()
