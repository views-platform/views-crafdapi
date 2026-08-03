"""S4 (#222): the consumer sb/ns/os rename — the boundary transform + its end-to-end effect.

Two layers:
- Unit: `schema.consumer_column` / `json_contract.to_consumer_columns` rename forecast-quantity
  columns structurally (rebuild `{series}_{quantity}`, not a substring swap), pass identity /
  geo / historical `actual` through untouched, and fail loud only on a genuine quantity column
  whose var has no series.
- Integration: the forecast hdi-map endpoint serves the exact ADR-025 consumer value columns at
  every level, min/max are gone, and NO internal pipeline prefix survives on any served column.
"""
import time
from unittest.mock import MagicMock

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.serialize.json_contract import to_consumer_columns
from views_crafdapi.managers.api import CrafdApiManager

pytestmark = pytest.mark.layer2_data

HEADERS = {"X-API-Key": "test-api-key"}
_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


# --- unit: the structured, quantity-scoped rename -------------------------------------------
def test_consumer_column_renames_quantities_structurally():
    assert schema.consumer_column("pred_lr_ged_sb_map") == "sb_map"
    assert schema.consumer_column("pred_lr_ged_sb_hdi90_lower") == "sb_hdi90_lower"
    assert schema.consumer_column("pred_ln_ns_best_severe_scenario") == "ns_severe_scenario"


def test_consumer_column_passes_non_quantity_columns_through():
    for col in ("month_id", "admin1_gaul1_code", "country_iso_a3", "pg_xcoord", "sb_actual"):
        assert schema.consumer_column(col) == col  # identity / geo / actual untouched


def test_consumer_column_fails_loud_only_on_a_series_less_quantity():
    with pytest.raises(ValueError):
        schema.consumer_column("pred_test_map")  # a real quantity col, no series token


def test_to_consumer_columns_renames_only_quantity_columns():
    df = pd.DataFrame(
        {c: [0.0] for c in ("month_id", "pred_lr_ged_sb_map", "pred_lr_ged_ns_hdi95_upper")}
    )
    assert list(to_consumer_columns(df).columns) == ["month_id", "sb_map", "ns_hdi95_upper"]


# --- integration: the served forecast response carries the exact ADR-025 consumer columns ----
@pytest.fixture
def client(tmp_path):
    mgr = CrafdApiManager.from_config(
        {"deployment": {"host": "0.0.0.0", "port": 80}}, cache_dir=tmp_path / "cache"
    )
    mgr._prediction_bucket_id = "test-bucket"
    mgr.app = FastAPI()
    mgr._register_routes()
    dataset = ForecastDataset(make_fao_df(n_samples=100, targets=_TARGETS))
    h = mgr._get_api_key_hash("test-api-key")
    # S1 (#264): a warm forecast serves only as a WIRE entry whose identity matches the current
    # manifest — tag it and return a matching manifest from the prediction manager.
    entry = {"data": dataset.dataframe, "file_id": "f", "timestamp": time.time(),
             "dataset": dataset, "source_kind": "wire"}
    mgr._dataframe_cache[h] = {"forecast": dict(entry), "historical": dict(entry)}
    mgr._validate_api_key = MagicMock(return_value=MagicMock())
    pm = MagicMock()
    pm.get_latest_manifest.return_value = {"fileId": "f", "filename": "m.json",
        "type": "sampled_forecast_manifest", "category": "forecast", "name": "un_crafd"}
    mgr.app.dependency_overrides[mgr._get_prediction_manager] = lambda: pm
    mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: MagicMock()
    return TestClient(mgr.app)


_EXPECTED_PER_SERIES = [  # the 9 ADR-025 forecast value columns per series (actual is S6/bulk)
    "{s}_map",
    "{s}_hdi50_lower", "{s}_hdi50_upper",
    "{s}_hdi90_lower", "{s}_hdi90_upper",
    "{s}_hdi95_lower", "{s}_hdi95_upper",
    "{s}_severe_scenario",
    "{s}_bimodality_flag",
]


@pytest.mark.parametrize("path", [
    "/pg/analysis/forecast/hdi-map",
    "/country/analysis/forecast/hdi-map?aggregate=true",
    "/gaul0/analysis/forecast/hdi-map?aggregate=true",
    "/gaul1/analysis/forecast/hdi-map?aggregate=true",
    "/gaul2/analysis/forecast/hdi-map?aggregate=true",
])
def test_forecast_response_has_the_exact_consumer_value_columns(client, path):
    resp = client.get(path, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    cols = set(resp.json()["data"]["columns"])
    for s in schema.SERIES:
        for tmpl in _EXPECTED_PER_SERIES:
            assert tmpl.format(s=s) in cols, f"missing {tmpl.format(s=s)} in {sorted(cols)}"
    # min/max retired; and NO internal pipeline prefix survives on ANY served column (C-α guard).
    assert not any(c.endswith("_min") or c.endswith("_max") for c in cols)
    assert not any(c.startswith(("pred_", "lr_", "ln_", "ged_")) for c in cols)
