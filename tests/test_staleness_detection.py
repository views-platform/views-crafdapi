"""Prediction staleness detection tests (C-50).

Validates that the staleness check correctly identifies stale cached predictions
and that failures in the check never block data serving.
"""

import time

import pytest
from unittest.mock import MagicMock

from views_crafdapi.managers.api import CrafdApiManager, StalenessResult
from views_crafdapi.data.handlers import ForecastDataset
from tests.conftest import make_fao_df

# A real warm entry: every production writer sets `data` and `dataset` together (#156).
_WARM = ForecastDataset(make_fao_df())

pytestmark = pytest.mark.layer4_infra


def _make_manager(tmp_path, staleness_hours=24.0):
    return CrafdApiManager.from_config(
        {"staleness_threshold_hours": staleness_hours},
        cache_dir=tmp_path / "datasets",
    )


class TestStalenessResult:

    def test_stale_when_old(self, tmp_path):
        mgr = _make_manager(tmp_path, staleness_hours=24.0)
        old_timestamp = time.time() - (48 * 3600)
        result = mgr._check_staleness(old_timestamp)
        assert result.is_stale is True
        assert result.age_hours > 47.0
        assert result.threshold_hours == 24.0

    def test_not_stale_when_fresh(self, tmp_path):
        mgr = _make_manager(tmp_path, staleness_hours=24.0)
        fresh_timestamp = time.time() - (1 * 3600)
        result = mgr._check_staleness(fresh_timestamp)
        assert result.is_stale is False
        assert result.age_hours < 2.0

    def test_threshold_configurable(self, tmp_path):
        mgr = _make_manager(tmp_path, staleness_hours=6.0)
        timestamp = time.time() - (8 * 3600)
        result = mgr._check_staleness(timestamp)
        assert result.is_stale is True
        assert result.threshold_hours == 6.0

    def test_threshold_override_per_call(self, tmp_path):
        mgr = _make_manager(tmp_path, staleness_hours=24.0)
        timestamp = time.time() - (3 * 3600)
        result_default = mgr._check_staleness(timestamp)
        result_tight = mgr._check_staleness(timestamp, threshold_hours=1.0)
        assert result_default.is_stale is False
        assert result_tight.is_stale is True

    def test_staleness_result_dataclass(self, tmp_path):
        mgr = _make_manager(tmp_path)
        result = mgr._check_staleness(time.time())
        assert isinstance(result, StalenessResult)
        assert hasattr(result, "is_stale")
        assert hasattr(result, "age_hours")
        assert hasattr(result, "threshold_hours")

    def test_exact_boundary(self, tmp_path):
        mgr = _make_manager(tmp_path, staleness_hours=24.0)
        just_under = time.time() - (23.9 * 3600)
        just_over = time.time() - (24.1 * 3600)
        assert mgr._check_staleness(just_under).is_stale is False
        assert mgr._check_staleness(just_over).is_stale is True


class TestStalenessDoesNotBlockResponse:

    def test_cache_hit_returns_data_even_when_stale(self, tmp_path):
        """Stale predictions are served — staleness is advisory, not a gate."""
        mgr = _make_manager(tmp_path, staleness_hours=1.0)
        api_key_hash = mgr._get_api_key_hash("test-key")

        old_timestamp = time.time() - (48 * 3600)
        mgr._dataframe_cache[api_key_hash] = {
            "historical": {
                "data": _WARM.dataframe,
                "file_id": "stale_file",
                "timestamp": old_timestamp,
                "dataset": _WARM,
            },
            "forecast": {"data": None, "file_id": None, "timestamp": None, "dataset": None},
        }
        mgr._disk_cache._ttl_seconds = 999999

        result = mgr._get_latest_dataframe(MagicMock(), "test-key", "historical")
        assert result is not None
        assert len(result) == len(_WARM.dataframe)

    def test_staleness_check_exception_does_not_crash_response(self, tmp_path):
        """Any exception in _check_staleness must be caught — CIC Sec 6 guarantees non-blocking."""
        mgr = _make_manager(tmp_path)
        api_key_hash = mgr._get_api_key_hash("test-key")

        mgr._dataframe_cache[api_key_hash] = {
            "historical": {
                "data": _WARM.dataframe,
                "file_id": "file_1",
                "timestamp": time.time() - 3600,
                "dataset": _WARM,
            },
            "forecast": {"data": None, "file_id": None, "timestamp": None, "dataset": None},
        }
        mgr._disk_cache._ttl_seconds = 999999

        del mgr._staleness_threshold_hours

        result = mgr._get_latest_dataframe(MagicMock(), "test-key", "historical")
        assert result is not None
        assert len(result) == len(_WARM.dataframe)
