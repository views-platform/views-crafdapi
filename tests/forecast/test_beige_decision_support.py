"""Beige decision-support tests (ADR-005) — pin the MEANING of the served output.

ADR-005 mandates beige tests for any decision-facing component and names five
misinterpretation scenarios. These tests pin the properties that protect a
humanitarian decision-maker from each one, so a refactor that silently changes
what the numbers mean to FAO — dropping a credible level, the aggregation grain,
the category, or the robust worst-case — fails a test (register C-73).

The five ADR-005 scenarios and the property that guards each (post #222/S4, ADR-025):
  1. MAP-as-point-prediction      -> every served MAP ships with HDI bounds.
  2. HDI-as-confidence-interval   -> the credible MASS is encoded in the column NAME
                                     (`hdi50`/`hdi90`/`hdi95`), so a consumer reads the
                                     level off the column, not a frequentist coverage claim.
  3. severe-as-hard-bound         -> `severe_scenario` is the mean of the worst 5% of draws
                                     (a robust tail severity), bracketed above the MAP — not a
                                     guaranteed ceiling; raw min/max are deliberately not served.
  4. aggregate-over-trust         -> the response self-describes `aggregate`.
  5. GAUL-2 small-area over-trust  -> the response self-describes the spatial `level`.
"""
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import FAO_PGMDataset
from views_faoapi.forecast.serialize import schema
from views_faoapi.managers.api import FAOApiManager

pytestmark = pytest.mark.layer2_data

HEADERS = {"X-API-Key": "test-api-key"}

# Conforming forecast targets so the forecast endpoint's consumer rename (schema.series_of)
# resolves sb/ns/os — the served columns are then the ADR-025 consumer names.
_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


@pytest.fixture
def beige_client(tmp_path):
    """TestClient over a real cached dataset, so analysis endpoints return
    genuinely-computed responses (mirrors test_api_endpoints.app_client)."""
    mgr = FAOApiManager.from_config(
        {"deployment": {"host": "0.0.0.0", "port": 80}},
        cache_dir=tmp_path / "cache",
    )
    mgr._prediction_bucket_id = "test-bucket"
    mgr.app = FastAPI()
    mgr._register_routes()

    dataset = FAO_PGMDataset(make_fao_df(n_samples=100, targets=_TARGETS))
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
        "type": "sampled_forecast_manifest", "category": "forecast", "name": "un_fao"}
    mgr.app.dependency_overrides[mgr._get_prediction_manager] = lambda: pm
    mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: MagicMock()
    return TestClient(mgr.app)


class TestResponseSelfDescribes:
    """The served response carries the metadata that prevents misreading it."""

    def test_credible_mass_is_encoded_in_the_column_names(self, beige_client):
        """Scenario 2 (HDI-as-confidence-interval): the credible level is legible from the
        column NAME (`hdi50`/`hdi90`/`hdi95`) — a consumer reads the exact mass off the column,
        never inferring frequentist coverage. Dropping a level fails this test."""
        resp = beige_client.get("/pg/analysis/forecast/hdi-map", headers=HEADERS)
        assert resp.status_code == 200
        cols = resp.json()["data"]["columns"]
        for pct in (50, 90, 95):
            assert any(c.endswith(f"_hdi{pct}_lower") for c in cols), f"missing hdi{pct}"
            assert any(c.endswith(f"_hdi{pct}_upper") for c in cols)

    def test_response_states_aggregation_and_level(self, beige_client):
        """Scenarios 4 + 5 (aggregate-over-trust, GAUL-2 small-area over-trust):
        the response self-describes the spatial `level` and the `aggregate`
        flag, so an aggregated / small-area estimate is labelled as such."""
        resp = beige_client.get(
            "/gaul2/analysis/forecast/hdi-map?aggregate=true", headers=HEADERS
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["level"] == "gaul2"
        assert data["parameters"]["aggregate"] is True

    def test_response_states_category(self, beige_client):
        resp = beige_client.get("/pg/analysis/historical/hdi-map", headers=HEADERS)
        assert resp.json()["data"]["category"] == "historical"

    def test_map_is_never_served_without_uncertainty(self, beige_client):
        """Scenario 1 (MAP-as-point-prediction): every served MAP is accompanied by HDI bounds
        (and the severe-outcome summary) in the same payload — the point estimate is never
        naked, and the retired raw min/max never reappear."""
        resp = beige_client.get("/pg/analysis/forecast/hdi-map", headers=HEADERS)
        cols = resp.json()["data"]["columns"]
        assert any(c.endswith("_map") for c in cols)
        assert any(c.endswith("_hdi90_lower") for c in cols)
        assert any(c.endswith("_hdi90_upper") for c in cols)
        assert any(c.endswith("_severe_scenario") for c in cols)
        assert not any(c.endswith("_min") or c.endswith("_max") for c in cols)


class TestSevereScenarioIsARobustTailNotAHardBound:
    """Scenario 3: `severe_scenario` (mean of the worst 5% of draws) sits above the MAP as a
    robust tail severity — not a guaranteed ceiling — and the three nested HDIs (50⊆90⊆95)
    bracket the MAP. Raw min/max were deliberately dropped in #222/S4."""

    @pytest.mark.parametrize("aggregate,level", [(False, None), (True, "gaul0")])
    def test_nested_hdis_bracket_map_and_severe_is_upper_tail(self, aggregate, level):
        ds = FAO_PGMDataset(make_fao_df(n_cells=4, n_months=2, n_samples=200, seed=7))
        df = ds.calculate_hdi_map(level=level, aggregate=aggregate)  # var-keyed (no endpoint rename)
        var_cols = [c[: -len("_map")] for c in df.columns if c.endswith("_map")]
        assert var_cols, "expected at least one variable with a _map column"
        for v in var_cols:
            assert f"{v}_min" not in df.columns and f"{v}_max" not in df.columns
            mp = df[f"{v}_map"]
            # nested: hdi50 ⊆ hdi90 ⊆ hdi95, each bracketing the MAP (element-wise).
            for lo, hi in [(50, 90), (90, 95)]:
                assert (df[schema.hdi_col(v, lo / 100, "lower")] >= df[schema.hdi_col(v, hi / 100, "lower")] - 1e-9).all()
                assert (df[schema.hdi_col(v, lo / 100, "upper")] <= df[schema.hdi_col(v, hi / 100, "upper")] + 1e-9).all()
            assert (df[schema.hdi_col(v, 0.90, "lower")] <= mp + 1e-9).all()
            assert (mp <= df[schema.hdi_col(v, 0.90, "upper")] + 1e-9).all()
            # severe_scenario (worst-5% mean) sits at or above the MAP — a tail severity.
            severe = df[f"{v}_severe_scenario"]
            assert (severe >= mp - 1e-9).all() or np.isnan(severe).all()
