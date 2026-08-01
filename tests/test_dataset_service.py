"""Unit tests for DatasetService (epic #144 / S2) — the data-fetch pipeline in isolation.

Exercises the three cache tiers (memory → disk → remote download), force_refresh,
the plausibility/provenance path, and the no-forecast 404 — without the HTTP stack,
by injecting fake caches + a mock PredictionStoreManager.
"""
import time
import types
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import ForecastDataset
from views_faoapi.managers.dataset_service import DatasetService

pytestmark = pytest.mark.layer4_infra

HASH = "deadbeef00000000"


def _service(*, dataframe_cache=None, file_cache=None, disk_cache=None, historical_targets=None):
    disk = disk_cache or MagicMock(ttl_seconds=10**9)
    if disk_cache is None:
        disk.read.return_value = None
        disk.write.return_value = True
    return DatasetService(
        dataframe_cache=dataframe_cache if dataframe_cache is not None else {},
        file_cache=file_cache if file_cache is not None else {},
        disk_cache=disk,
        prediction_bucket_id="test-bucket",
        configs_getter=lambda: ({"historical_targets": historical_targets} if historical_targets else {}),
        api_key_hash_fn=lambda _k: HASH,
        check_staleness_fn=lambda _ts: types.SimpleNamespace(
            is_stale=False, age_hours=0.0, threshold_hours=24.0
        ),
    )


def _mock_manager(file_bytes, *, has_forecast=True):
    mgr = MagicMock()
    if not has_forecast:
        mgr.get_latest_provenance.return_value = None
        return mgr
    prov = MagicMock(
        file_id="file_xyz", source="datafactory", methodology_version="v1",
        file_hash="abc", created_at="2026", filename="f.parquet",
    )
    prov.to_dict.return_value = {"file_id": "file_xyz", "source": "datafactory"}
    mgr.get_latest_provenance.return_value = prov
    dl = MagicMock(success=True, data={"file_bytes": file_bytes})
    mgr.download_prediction.return_value = dl
    return mgr


class TestCacheTiers:
    def test_memory_cache_hit_skips_download(self):
        df = make_fao_df()
        cache = {HASH: {"historical": {"data": df, "file_id": "f", "timestamp": time.time(), "dataset": None}}}
        svc = _service(dataframe_cache=cache)
        mgr = MagicMock()
        mgr.get_latest_file_id.return_value = "f"  # C-172: cached entry IS still the newest historical
        out = svc.get_latest_dataframe(mgr, "key", "historical")
        mgr.get_latest_provenance.assert_not_called()
        mgr.download_prediction.assert_not_called()
        assert out.equals(df) and out is not df  # returns a copy

    def test_disk_cache_fallback_skips_download(self):
        ds = ForecastDataset(make_fao_df())
        disk = MagicMock(ttl_seconds=10**9)
        disk.read.return_value = {"dataset": ds, "file_id": "f", "timestamp": time.time()}
        disk.check_file_id.return_value = True  # C-172: cached entry IS still the newest historical
        svc = _service(disk_cache=disk)
        mgr = MagicMock()
        mgr.get_latest_file_id.return_value = "f"
        out = svc.get_latest_dataframe(mgr, "key", "historical")
        mgr.download_prediction.assert_not_called()
        assert len(out) == len(ds.dataframe)

    def test_stale_warm_historical_reselects_on_newer_file(self, parquet_bytes):
        """C-172: a warm historical entry that is no longer the newest artifact is NOT served — the
        service re-selects and downloads the newer file (mirrors the forecast manifest re-key)."""
        df = make_fao_df()
        cache = {HASH: {"historical": {"data": df, "file_id": "old", "timestamp": time.time(), "dataset": None}}}
        svc = _service(dataframe_cache=cache)
        mgr = _mock_manager(parquet_bytes)
        mgr.get_latest_file_id.return_value = "new"  # a newer historical was uploaded
        svc.get_latest_dataframe(mgr, "key", "historical")
        mgr.download_prediction.assert_called_once()  # did NOT serve the stale warm entry

    def test_stale_disk_historical_reselects_on_newer_file(self, parquet_bytes):
        """C-172: a disk historical entry superseded by a newer file is skipped BEFORE the value-dir
        is loaded (via the cheap check_file_id meta probe), and the newer file is downloaded."""
        ds = ForecastDataset(make_fao_df())
        disk = MagicMock(ttl_seconds=10**9)
        disk.read.return_value = {"dataset": ds, "file_id": "old", "timestamp": time.time()}
        disk.check_file_id.return_value = False  # cached meta file_id != newest historical
        svc = _service(disk_cache=disk)
        mgr = _mock_manager(parquet_bytes)
        mgr.get_latest_file_id.return_value = "new"
        svc.get_latest_dataframe(mgr, "key", "historical")
        mgr.download_prediction.assert_called_once()
        disk.read.assert_not_called()  # skipped before loading the value-dir

    def test_remote_download_parses_caches_and_records_provenance(self, parquet_bytes):
        cache, disk = {}, MagicMock(ttl_seconds=10**9)
        disk.read.return_value = None
        svc = _service(dataframe_cache=cache, disk_cache=disk)
        mgr = _mock_manager(parquet_bytes)
        out = svc.get_latest_dataframe(mgr, "key", "historical")
        assert len(out) > 0
        mgr.download_prediction.assert_called_once_with("file_xyz")
        disk.write.assert_called_once()                      # written for other workers
        assert cache[HASH]["historical"]["dataset"] is not None  # memory populated
        assert cache[HASH]["historical"]["provenance"]["source"] == "datafactory"  # C-86

    def test_force_refresh_bypasses_memory(self, parquet_bytes):
        df = make_fao_df()
        cache = {HASH: {"historical": {"data": df, "file_id": "old", "timestamp": time.time(), "dataset": None}}}
        svc = _service(dataframe_cache=cache)
        mgr = _mock_manager(parquet_bytes)
        svc.get_latest_dataframe(mgr, "key", "historical", force_refresh=True)
        mgr.download_prediction.assert_called_once()  # ignored the warm memory cache


class TestErrors:
    def test_no_forecast_raises_404(self):
        svc = _service()
        mgr = _mock_manager(None, has_forecast=False)
        with pytest.raises(HTTPException) as exc:
            svc.get_latest_dataframe(mgr, "key", "historical")
        assert exc.value.status_code == 404

    def test_empty_download_raises_500(self):
        svc = _service()
        mgr = MagicMock()
        mgr.get_latest_provenance.return_value = MagicMock(
            file_id="f", source="s", methodology_version="v", file_hash="h",
            created_at="c", filename="n",
        )
        mgr.download_prediction.return_value = MagicMock(success=True, data={"file_bytes": None})
        with pytest.raises(HTTPException) as exc:
            svc.get_latest_dataframe(mgr, "key", "historical")
        assert exc.value.status_code == 500


class TestGetLatestDataset:
    def test_returns_dataset_copy(self, parquet_bytes):
        svc = _service()
        mgr = _mock_manager(parquet_bytes)
        ds = svc.get_latest_dataset(mgr, "key", "historical")
        assert isinstance(ds, ForecastDataset)
