"""Beige decision-support tests (ADR-005) — pin the MEANING of the served output.

ADR-005 mandates beige tests for any decision-facing component and names five
misinterpretation scenarios. These tests pin the properties that protect a
humanitarian decision-maker from each one, so a refactor that silently changes
what the numbers mean to FAO — dropping a credible level, the aggregation grain,
the category, or the robust worst-case — fails a test (register C-73).

The ADR-005 misreadings and the property that guards each, for the summary columns decided
on 2026-11-17 (#204, data dictionary):
  1. expected-value-as-the-forecast -> the expected number (`s_mean`) never comes alone: the
                                     chance of any deaths, the deaths if any, the chances of
                                     larger numbers and the severe case come with it.
  2. a chance read without its threshold -> each chance names its threshold in the column name
                                     (`s_p_gt25`), so it is read as "more than 25", not "about 25".
  3. severe-as-hard-bound         -> `severe_scenario` is the mean of the worst 5% of draws: at
                                     or above the expected number, not a ceiling; it is empty
                                     below 20 draws, where it would be the single largest draw.
  4. aggregate-over-trust         -> the response states its `level`.
  5. small-area over-trust        -> the response states its `level`.
"""
import time
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.serialize import schema
from views_crafdapi.managers.api import CrafdApiManager

pytestmark = pytest.mark.layer2_data

HEADERS = {"X-API-Key": "test-api-key"}

# Conforming forecast targets so the forecast endpoint's consumer rename (schema.series_of)
# resolves sb/ns/os — the served columns are then the ADR-025 consumer names.
_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


@pytest.fixture
def beige_client(tmp_path):
    """TestClient over a real cached dataset, so analysis endpoints return
    genuinely-computed responses (mirrors test_api_endpoints.app_client)."""
    mgr = CrafdApiManager.from_config(
        {"deployment": {"host": "0.0.0.0", "port": 80}},
        cache_dir=tmp_path / "cache",
    )
    mgr._prediction_bucket_id = "test-bucket"
    mgr.app = FastAPI()
    mgr._register_routes()

    dataset = ForecastDataset(make_fao_df(n_samples=100, targets=_TARGETS))
    h = mgr._get_api_key_hash("test-api-key")
    entry = {
        "data": dataset.dataframe,
        "file_id": "f",
        "timestamp": time.time(),
        "dataset": dataset,
        "source_kind": "wire",  # S1 (#264): a warm forecast serves only as a manifested wire entry
    }
    mgr._dataframe_cache[h] = {"historical": dict(entry), "forecast": dict(entry)}

    mgr._validate_api_key = MagicMock(return_value=MagicMock())
    pm = MagicMock()
    pm.get_latest_manifest.return_value = {"fileId": "f", "filename": "m.json",
        "type": "sampled_forecast_manifest", "category": "forecast", "name": "un_crafd"}
    mgr.app.dependency_overrides[mgr._get_prediction_manager] = lambda: pm
    mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: MagicMock()
    return TestClient(mgr.app)


class TestResponseSelfDescribes:
    """The served response carries the metadata that prevents misreading it."""

    def test_each_chance_names_its_threshold(self, beige_client):
        """Scenario 2: the threshold is legible from the column name; dropping one fails."""
        resp = beige_client.get("/slice/grid/sb", headers=HEADERS)
        assert resp.status_code == 200
        cols = resp.json()["columns"]
        for c in (0, 25, 100, 500, 1000):
            assert f"sb_p_gt{c}" in cols, f"missing the chance of more than {c}"

    @pytest.mark.parametrize("level", ["grid", "admin1", "country"])
    def test_response_states_level_and_outcome(self, beige_client, level):
        """Scenarios 4 + 5 (aggregate-over-trust, small-area over-trust): the response states
        its level, so a summed (admin-1, country) or small-area (grid) estimate is labelled
        as such. Above the grid cell a slice is always summed: there is no switch for it."""
        resp = beige_client.get(f"/slice/{level}/ns", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["level"] == level
        assert body["outcome"] == "ns"

    def test_the_expected_number_never_comes_alone(self, beige_client):
        """Scenario 1: unless the partner names fewer columns, the expected number comes with
        the chance of any deaths, the deaths if any, and the severe case; the retired columns
        (most likely value, intervals, minimum and maximum) never reappear."""
        resp = beige_client.get("/slice/grid/sb", headers=HEADERS)
        cols = resp.json()["columns"]
        for col in ("sb_mean", "sb_p_gt0", "sb_mean_if_any", "sb_median_if_any", "sb_severe_scenario", "n_draws"):
            assert col in cols
        assert not any(c.endswith(("_map", "_min", "_max", "_lower", "_upper")) for c in cols)


class TestSevereScenarioIsARobustTailNotAHardBound:
    """Scenario 3: `severe_scenario`, the mean of the worst 5% of draws, sits at or above the
    expected number, as a tail severity, not a ceiling; and the chances fall as the threshold
    rises. Cell level and summed to admin 1."""

    @pytest.mark.parametrize("aggregate,level", [(False, None), (True, "gaul1")])
    def test_severe_is_at_or_above_the_mean_and_chances_fall(self, aggregate, level):
        ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=200, seed=7))
        df = ds.summarize(level=level, aggregate=aggregate)  # named by target (no rename)
        for v in ds.targets:
            severe, mean = df[f"{v}_severe_scenario"], df[f"{v}_mean"]
            assert severe.notna().all()                         # 200 draws: enough for a severe case
            assert (severe >= mean - 1e-9).all()
            chances = [df[f"{v}_p_gt{c}"] for c in schema.EXCEEDANCE_THRESHOLDS]
            for higher, lower in zip(chances, chances[1:]):
                assert (lower <= higher).all()
