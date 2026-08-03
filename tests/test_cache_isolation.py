"""Per-API-key cache isolation tests (C-58).

Validates that the three-tier in-memory cache (manager, dataframe, file)
correctly isolates data between different API keys, and that eviction in one
key's partition does not corrupt another's.
"""

import hashlib

import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.managers.api import CrafdApiManager
from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager

pytestmark = pytest.mark.layer4_infra


def _make_manager(tmp_path):
    return CrafdApiManager.from_config({}, cache_dir=tmp_path / "datasets")


def _make_dataset(seed=1):
    """A real cacheable dataset (the disk cache round-trips it via to_value/from_value)."""
    return ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=16, seed=seed))


class TestApiKeyHashContract:

    def test_hash_is_deterministic(self, tmp_path):
        mgr = _make_manager(tmp_path)
        h1 = mgr._get_api_key_hash("test-key-alpha")
        h2 = mgr._get_api_key_hash("test-key-alpha")
        assert h1 == h2

    def test_different_keys_produce_different_hashes(self, tmp_path):
        mgr = _make_manager(tmp_path)
        h1 = mgr._get_api_key_hash("key-one")
        h2 = mgr._get_api_key_hash("key-two")
        assert h1 != h2

    def test_hash_format_is_16_char_hex(self, tmp_path):
        mgr = _make_manager(tmp_path)
        h = mgr._get_api_key_hash("any-key")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_matches_sha256_prefix(self, tmp_path):
        mgr = _make_manager(tmp_path)
        key = "verify-against-stdlib"
        expected = hashlib.sha256(key.encode()).hexdigest()[:16]
        assert mgr._get_api_key_hash(key) == expected


class TestDataframeCacheIsolation:

    def test_different_keys_get_separate_entries(self, tmp_path):
        mgr = _make_manager(tmp_path)
        hash_a = mgr._get_api_key_hash("tenant-A")
        hash_b = mgr._get_api_key_hash("tenant-B")

        mgr._dataframe_cache[hash_a] = {
            "historical": {"data": pd.DataFrame({"x": [1]}), "file_id": "f1", "timestamp": 1.0}
        }

        assert hash_a in mgr._dataframe_cache
        assert hash_b not in mgr._dataframe_cache

    def test_same_key_retrieves_cached_data(self, tmp_path):
        mgr = _make_manager(tmp_path)
        hash_a = mgr._get_api_key_hash("tenant-A")
        entry = {"historical": {"data": pd.DataFrame({"v": [42]}), "file_id": "f1", "timestamp": 1.0}}
        mgr._dataframe_cache[hash_a] = entry

        retrieved = mgr._dataframe_cache[hash_a]
        assert retrieved["historical"]["data"]["v"].iloc[0] == 42


class TestManagerCacheEviction:

    def test_lru_evicts_oldest_entry(self, tmp_path):
        """Filling the cache beyond capacity evicts the oldest entry (LRU policy)."""
        mgr = _make_manager(tmp_path)
        maxsize = mgr._manager_cache.maxsize

        mgr._manager_cache["first"] = {"appwrite": "mgr_first", "prediction": None}
        for i in range(maxsize):
            mgr._manager_cache[f"filler_{i}"] = {"appwrite": f"mgr_{i}", "prediction": None}

        assert "first" not in mgr._manager_cache
        assert len(mgr._manager_cache) == maxsize

    def test_lru_access_refreshes_entry(self, tmp_path):
        """Accessing an entry prevents it from being evicted next."""
        mgr = _make_manager(tmp_path)
        maxsize = mgr._manager_cache.maxsize

        mgr._manager_cache["keep_me"] = {"appwrite": "mgr_keep", "prediction": None}
        for i in range(maxsize - 2):
            mgr._manager_cache[f"filler_{i}"] = {"appwrite": f"mgr_{i}", "prediction": None}

        _ = mgr._manager_cache["keep_me"]

        for i in range(maxsize - 2, maxsize + 10):
            mgr._manager_cache[f"filler_{i}"] = {"appwrite": f"mgr_{i}", "prediction": None}

        assert "keep_me" in mgr._manager_cache


class TestFileCacheSharedByDesign:

    def test_file_cache_keyed_by_file_id_not_api_key(self, tmp_path):
        """_file_cache is intentionally shared across API keys — files are immutable
        prediction artifacts identified by file_id, not tenant-specific."""
        mgr = _make_manager(tmp_path)

        mgr._file_cache["file_001"] = {"data": b"parquet-bytes", "timestamp": 1.0}

        assert "file_001" in mgr._file_cache
        assert mgr._file_cache["file_001"]["data"] == b"parquet-bytes"


class TestDiskPartitionIsolation:
    """On-disk partition invariants (register C-177 / #327, story S2).

    These assert *properties* of the partitioning, not the literal directory-label
    format, so they survive S5 (#323) replacing the ``sha256(key)[:16]`` label with a
    salted one. S5 adds the stronger invariant that the label is not *derivable* from
    the key (label != the api_key_hash); the three here hold both before and after.
    """

    @staticmethod
    def _partition_dirs(cache_dir):
        return sorted(
            p.name for p in cache_dir.iterdir() if p.is_dir() and p.name.endswith("_value")
        )

    def test_distinct_keys_get_distinct_partitions(self, tmp_path):
        cache = CrafdDiskCacheManager(tmp_path)
        cache.write("hash-alpha", "forecast", _make_dataset(1), "file-A")
        cache.write("hash-beta", "forecast", _make_dataset(2), "file-B")
        dirs = self._partition_dirs(tmp_path)
        assert len(dirs) == 2, f"expected two distinct partitions, got {dirs}"
        assert dirs[0] != dirs[1]

    def test_key_b_never_serves_key_a_content(self, tmp_path):
        """The core isolation guarantee: caller B never receives caller A's cached data."""
        cache = CrafdDiskCacheManager(tmp_path)
        cache.write("hash-alpha", "forecast", _make_dataset(1), "file-A")
        # B has written nothing under this category → a clean miss, never A's data.
        assert cache.read("hash-beta", "forecast") is None
        # And once both have written, each reads back only its own artifact.
        cache.write("hash-beta", "forecast", _make_dataset(2), "file-B")
        assert cache.read("hash-alpha", "forecast")["file_id"] == "file-A"
        assert cache.read("hash-beta", "forecast")["file_id"] == "file-B"

    def test_partition_label_never_contains_the_raw_key(self, tmp_path):
        """Going through the real key -> hash -> partition chain, the caller's raw key
        must never appear in any on-disk cache path (the label is not the secret)."""
        mgr = _make_manager(tmp_path)
        raw_key = "super-secret-caller-key-DO-NOT-LEAK"
        api_key_hash = mgr._get_api_key_hash(raw_key)
        mgr._disk_cache.write(api_key_hash, "forecast", _make_dataset(), "fid")
        for path in (tmp_path / "datasets").rglob("*"):
            assert raw_key not in path.name, f"raw key leaked into cache path: {path}"

    def test_partition_label_is_not_derivable_from_the_key_hash(self, tmp_path):
        """S5 (#323): the on-disk label is salted (HMAC) — not equal to the api_key_hash the
        caller's key maps to, so a filesystem observer cannot tie a partition to a key."""
        cache = CrafdDiskCacheManager(tmp_path)
        key_hash = "deadbeefdeadbeef"
        cache.write(key_hash, "forecast", _make_dataset(), "fid")
        label_dirs = [
            p.name for p in tmp_path.iterdir() if p.is_dir() and p.name.endswith("_value")
        ]
        assert len(label_dirs) == 1
        assert not label_dirs[0].startswith(key_hash), (
            f"partition label {label_dirs[0]!r} derives from the key hash {key_hash!r}"
        )
