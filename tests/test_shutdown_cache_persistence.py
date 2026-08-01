"""C-66: shutdown must NOT delete the durable on-disk dataset cache.

`FAODiskCacheManager` (`cache/datasets/`) is a 3.5-week cross-restart cache (ADR-011).
`_shutdown` previously ran `shutil.rmtree(self._model_path.cache)`, wiping it on every
SIGTERM — under a `systemd Restart=always` service that meant a full cold rebuild
(~2 min for the 5.7M-row historical set) on *every* restart.

These tests pin the corrected contract: shutdown clears only the **ephemeral in-memory**
caches and leaves the **durable on-disk** cache intact, and that cache survives a process
"restart". Deliberate purging stays available via the explicit `_maintenance(clear_cache=True)`
path (unchanged), not as a shutdown side-effect.
"""

import types
from pathlib import Path

import pytest

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import ForecastDataset
from views_faoapi.managers.api import FAOApiManager
from views_faoapi.managers.disk_cache import FAODiskCacheManager

pytestmark = pytest.mark.layer4_infra


def _dataset(seed: int = 1) -> ForecastDataset:
    return ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=16, seed=seed))


def _manager_with_disk_cache(cache_root: Path) -> FAOApiManager:
    """Test manager whose durable cache lives at ``<cache_root>/datasets``, with
    ``_model_path.cache == cache_root`` — reproducing the production layout where the
    old ``_shutdown`` rmtree'd the parent and took the ``datasets/`` store with it."""
    mgr = FAOApiManager.from_config({}, cache_dir=cache_root / "datasets")
    mgr._model_path = types.SimpleNamespace(cache=cache_root)
    return mgr


class TestShutdownPreservesDurableCache:

    def test_shutdown_keeps_disk_cache_and_clears_memory(self, tmp_path):
        mgr = _manager_with_disk_cache(tmp_path)

        # durable on-disk entry
        mgr._disk_cache.write("hash1", "forecast", _dataset(), "file_1")
        value_dir = mgr._disk_cache._value_dir("hash1", "forecast")
        assert value_dir.is_dir()

        # ephemeral in-memory entries
        mgr._manager_cache["k"] = {"x": 1}
        mgr._dataframe_cache["k"] = {"x": 1}
        mgr._file_cache["k"] = b"bytes"

        mgr._shutdown()

        # the durable disk cache survives (the C-66 fix) and is still usable
        assert value_dir.is_dir(), "shutdown must not delete the durable disk cache"
        assert mgr._disk_cache.read("hash1", "forecast") is not None
        # ephemeral caches are cleared
        assert len(mgr._manager_cache) == 0
        assert len(mgr._dataframe_cache) == 0
        assert len(mgr._file_cache) == 0


class TestDiskCacheSurvivesRestart:

    def test_fresh_manager_reads_prior_write(self, tmp_path):
        """Write via one cache manager, then read via a fresh one on the same dir
        (a new process after a restart) — a hit, not a cold rebuild."""
        datasets_dir = tmp_path / "datasets"
        FAODiskCacheManager(datasets_dir).write("hash1", "forecast", _dataset(), "file_1")

        fresh = FAODiskCacheManager(datasets_dir)  # simulates the post-restart process
        hit = fresh.read("hash1", "forecast")
        assert hit is not None and hit["file_id"] == "file_1"
