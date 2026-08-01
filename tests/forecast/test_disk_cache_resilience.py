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
