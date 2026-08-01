"""ADR-011: Disk cache embeds schema_version in a metadata sidecar.

Post-S5 (#154) the cache persists the dataset VALUE (npz/parquet/json via
`ForecastDataset.to_value`/`from_value`), not a pickled object — so these tests use a
real `ForecastDataset` and assert on the value directory, and the schema version is
derived from the value format + meta layout (decoupled from the class signature, C-138).
"""

import json
import pickle
import time

import filelock
import pytest

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import ForecastDataset
from views_faoapi.managers.disk_cache import FAODiskCacheManager, CACHE_SCHEMA_VERSION

pytestmark = pytest.mark.layer4_infra


def _make_dataset(seed=1):
    """A real cacheable dataset (the cache now calls `to_value`/`from_value`)."""
    return ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=16, seed=seed))


class TestDiskCacheSchemaVersion:

    def test_cache_schema_version_constant_exists(self):
        assert isinstance(CACHE_SCHEMA_VERSION, int)
        assert CACHE_SCHEMA_VERSION >= 1

    def test_write_disk_cache_includes_schema_version(self, tmp_path):
        cache = FAODiskCacheManager(tmp_path)
        cache.write("testhash", "forecast", _make_dataset(), "file_123")

        meta_path = cache._meta_path("testhash", "forecast")
        assert meta_path.exists()
        assert cache._value_dir("testhash", "forecast").is_dir()  # value dir, not a .pkl
        meta = json.loads(meta_path.read_text())
        assert meta["schema_version"] == CACHE_SCHEMA_VERSION

    def test_read_disk_cache_rejects_version_mismatch(self, tmp_path):
        cache = FAODiskCacheManager(tmp_path)
        cache.write("testhash", "forecast", _make_dataset(), "file_123")

        meta_path = cache._meta_path("testhash", "forecast")
        meta = json.loads(meta_path.read_text())
        meta["schema_version"] = 0
        meta_path.write_text(json.dumps(meta))

        result = cache.read("testhash", "forecast")
        assert result is None, "Cache with mismatched schema_version should be rejected"

    def test_stray_pickle_is_never_deserialized(self, tmp_path):
        """A stray pre-value-store `.pkl` on disk is never loaded: the read path carries no
        `pickle.load` surface (C-149), and with no value dir the read is a clean miss. The
        pre-S5 pickle janitor was retired in S6/#331 — a stray pickle is simply ignored, never
        deserialized (the safety property that matters), never a crash."""
        cache = FAODiskCacheManager(tmp_path)
        # A real pickle at the old raw-hash label; deserializing it would be the C-149 regression.
        pkl_path = tmp_path / "testhash_forecast_dataset.pkl"
        with open(pkl_path, "wb") as f:
            pickle.dump({"placeholder": True}, f)
        # A salted meta but no value dir -> the read must miss (never touch the .pkl).
        cache._meta_path("testhash", "forecast").write_text(json.dumps({
            "schema_version": CACHE_SCHEMA_VERSION, "file_id": "file_123",
            "timestamp": time.time(), "rows": 100, "columns": ["col1"], "ttl_days": 24.5,
        }))

        result = cache.read("testhash", "forecast")
        assert result is None, "no value dir -> a clean miss; the .pkl is never deserialized"


class TestCacheSchemaAutoDerivation:
    """C-42: CACHE_SCHEMA_VERSION is auto-derived; C-138: decoupled from class identity."""

    def test_version_is_deterministic(self):
        from views_faoapi.managers.disk_cache import _derive_cache_schema_version
        assert _derive_cache_schema_version() == _derive_cache_schema_version()

    def test_version_changes_if_meta_fields_change(self):
        from views_faoapi.managers.disk_cache import _derive_cache_schema_version
        import views_faoapi.managers.disk_cache as dc
        original = _derive_cache_schema_version()
        saved = dc._META_FIELDS
        try:
            dc._META_FIELDS = saved + ("new_field",)
            changed = _derive_cache_schema_version()
        finally:
            dc._META_FIELDS = saved
        assert original != changed, "Adding a meta field must change the schema version"

    def test_version_changes_if_value_schema_version_changes(self):
        """An actual on-disk value-format change must invalidate old caches."""
        from views_faoapi.managers.disk_cache import _derive_cache_schema_version
        import views_faoapi.data.value_format as vf
        original = _derive_cache_schema_version()
        saved = vf._VALUE_SCHEMA_VERSION
        try:
            vf._VALUE_SCHEMA_VERSION = "value-store-vNEXT"
            changed = _derive_cache_schema_version()
        finally:
            vf._VALUE_SCHEMA_VERSION = saved
        assert original != changed, "A value-format bump must change the schema version"

    def test_version_independent_of_init_signature(self):
        """C-138 fix: a dataset-constructor signature change no longer churns the cache
        version (the old fingerprint coupled to `inspect.signature(__init__)`)."""
        from views_faoapi.managers.disk_cache import _derive_cache_schema_version
        from views_faoapi.data.handlers import FAO_PGMDataset
        original = _derive_cache_schema_version()
        orig_init = FAO_PGMDataset.__init__
        try:
            FAO_PGMDataset.__init__ = lambda self, source, targets=None, extra=None: None
            assert _derive_cache_schema_version() == original
        finally:
            FAO_PGMDataset.__init__ = orig_init

    def test_meta_fields_match_write_output(self, tmp_path):
        cache = FAODiskCacheManager(tmp_path)
        cache.write("testhash", "forecast", _make_dataset(), "file_123")
        meta_path = cache._meta_path("testhash", "forecast")
        meta = json.loads(meta_path.read_text())
        written_fields = sorted(k for k in meta if k != "schema_version")
        from views_faoapi.managers.disk_cache import _META_FIELDS
        assert tuple(written_fields) == tuple(sorted(_META_FIELDS))


class TestConcurrentDiskCacheAccess:
    """C-38: file-locking holds under concurrent access (now over a value directory)."""

    def test_concurrent_writes_no_corruption(self, tmp_path):
        """Threads writing the same key — verify no corruption; the value dir is never torn."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        cache = FAODiskCacheManager(tmp_path)

        def write_dataset(thread_id):
            return cache.write("same_key", "forecast", _make_dataset(thread_id), f"file_{thread_id}")

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(write_dataset, i) for i in range(4)]
            results = [f.result() for f in as_completed(futures)]

        assert all(results), "All concurrent writes must succeed"
        result = cache.read("same_key", "forecast")
        assert result is not None, "the value dir is complete (never torn)"
        assert result["file_id"].startswith("file_")
        # no stray temp dir left behind
        assert not list(tmp_path.glob("*_value.tmp"))

    def test_reader_during_active_write(self, tmp_path):
        """Reader during write — must get a clean reconstruction or None, never partial."""
        import threading

        cache = FAODiskCacheManager(tmp_path)
        cache.write("rw_key", "forecast", _make_dataset(1), "initial")

        barrier = threading.Barrier(2, timeout=5)
        read_results = []

        def writer():
            barrier.wait()
            cache.write("rw_key", "forecast", _make_dataset(2), "updated")

        def reader():
            barrier.wait()
            read_results.append(cache.read("rw_key", "forecast"))

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=reader)
        t_w.start()
        t_r.start()
        t_w.join(timeout=10)
        t_r.join(timeout=10)

        assert len(read_results) == 1
        result = read_results[0]
        assert result is not None
        assert result["file_id"] in ("initial", "updated")

    def test_lock_timeout_degrades_gracefully(self, tmp_path):
        """Simulate held lock — reader returns None (fallback), not crash."""
        from unittest.mock import patch

        cache = FAODiskCacheManager(tmp_path)
        cache.write("lock_key", "forecast", _make_dataset(), "file_1")

        with patch.object(filelock.FileLock, "acquire", side_effect=filelock.Timeout("lock_key")):
            result = cache.read("lock_key", "forecast")

        assert result is None, "Lock timeout must return None, not crash"

    def test_check_file_id_after_concurrent_write(self, tmp_path):
        """Staleness detection after overwrites."""
        cache = FAODiskCacheManager(tmp_path)
        cache.write("stale_key", "forecast", _make_dataset(), "original_file")
        assert cache.check_file_id("stale_key", "forecast", "original_file") is True

        cache.write("stale_key", "forecast", _make_dataset(), "new_file")
        assert cache.check_file_id("stale_key", "forecast", "original_file") is False
        assert cache.check_file_id("stale_key", "forecast", "new_file") is True


class TestFAOApiManagerDiskCacheIntegration:
    """FAOApiManager.from_config exposes the disk cache; round-trip serves identically."""

    def test_from_config_creates_disk_cache_manager(self, tmp_path):
        from views_faoapi.managers.api import FAOApiManager
        manager = FAOApiManager.from_config({}, cache_dir=tmp_path / "datasets")
        assert isinstance(manager._disk_cache, FAODiskCacheManager)

    def test_round_trip_through_api_manager(self, tmp_path):
        from views_faoapi.managers.api import FAOApiManager
        manager = FAOApiManager.from_config({}, cache_dir=tmp_path / "datasets")
        ds = _make_dataset()
        manager._disk_cache.write("keyhash", "forecast", ds, "fid_1")
        result = manager._disk_cache.read("keyhash", "forecast")
        assert result is not None
        assert result["file_id"] == "fid_1"
        # the reconstructed dataset serves byte-identical HDI/MAP
        assert ds.calculate_hdi_map().equals(result["dataset"].calculate_hdi_map())
