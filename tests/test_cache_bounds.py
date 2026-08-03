"""ADR-011: All in-memory caches must have size limits and LRU eviction."""

import pytest
from collections.abc import MutableMapping

pytestmark = pytest.mark.layer4_infra


class TestCacheBounds:

    def _make_manager(self, tmp_path):
        from views_crafdapi.managers.api import CrafdApiManager
        return CrafdApiManager.from_config({}, cache_dir=tmp_path / "datasets")

    def test_manager_cache_is_bounded(self, tmp_path):
        manager = self._make_manager(tmp_path)
        cache = manager._manager_cache
        assert isinstance(cache, MutableMapping)
        assert hasattr(cache, "maxsize"), "_manager_cache must have maxsize"
        assert cache.maxsize > 0

    def test_dataframe_cache_has_ttl(self, tmp_path):
        manager = self._make_manager(tmp_path)
        cache = manager._dataframe_cache
        assert hasattr(cache, "maxsize"), "_dataframe_cache must have maxsize"
        assert hasattr(cache, "ttl"), "_dataframe_cache must have TTL"
        assert cache.ttl > 0

    def test_file_cache_is_bounded(self, tmp_path):
        manager = self._make_manager(tmp_path)
        cache = manager._file_cache
        assert hasattr(cache, "maxsize"), "_file_cache must have maxsize"
        assert cache.maxsize > 0

    def test_lru_eviction_works(self):
        from cachetools import LRUCache
        cache = LRUCache(maxsize=2)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        assert "a" not in cache
        assert "b" in cache
        assert "c" in cache

    def test_caches_support_dict_operations(self, tmp_path):
        """Bounded caches must support standard dict API (Liskov Substitution)."""
        manager = self._make_manager(tmp_path)
        for cache in [manager._manager_cache, manager._dataframe_cache, manager._file_cache]:
            cache["test_key"] = {"data": "value"}
            assert "test_key" in cache
            assert cache["test_key"]["data"] == "value"
            cache.clear()
            assert len(cache) == 0
