"""S6 (#222): the ADR-025 admin-1 bulk parquet writer (33 columns, incl. historical `actual`)."""

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.serialize.bulk_parquet import build_bulk_table, write_bulk_parquet

pytestmark = pytest.mark.layer2_data

_FC = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")
_HT = ("lr_ged_sb", "lr_ged_ns", "lr_ged_os")


def _forecast(seed=1, **kw):
    return ForecastDataset(make_fao_df(n_samples=32, seed=seed, targets=_FC, **kw))


def _historical(seed=2, **kw):
    df = make_fao_df(n_samples=1, seed=seed, targets=_HT, **kw)
    return ForecastDataset(df, targets=list(_HT))


def test_table_is_exactly_the_33_column_adr025_schema():
    table = build_bulk_table(_forecast(), _historical())
    assert list(table.columns) == schema.bulk_columns()
    assert len(table.columns) == 36


def test_one_row_per_month_and_admin1_with_consumer_identity():
    table = build_bulk_table(_forecast(n_cells=4, n_months=2), _historical(n_cells=4, n_months=2))
    assert len(table) == 4  # 2 admin-1 units × 2 months
    assert not table.duplicated(subset=["month_id", "admin1_code"]).any()
    row = table.sort_values(["month_id", "admin1_code"]).iloc[0]
    # consumer identity names (GAUL admin-0 country, not M49)
    assert row["admin1_code"] == 1 and row["admin1_name"] == "Region1"
    assert row["country_code"] == 10 and row["country_name"] == "Province1"
    assert row["country_iso3"] == "AAA"


def test_actual_is_the_admin1_sum_of_observed_counts():
    hist = _historical(n_cells=4, n_months=2)
    table = build_bulk_table(_forecast(n_cells=4, n_months=2), hist)
    observed = hist._sample_array("lr_ged_sb")[:, 0]
    geo = hist.geo_metadata
    manual = (
        pd.DataFrame({
            "m": geo.index.get_level_values("month_id"),
            "a": geo["admin1_gaul1_code"],
            "v": observed,
        })
        .groupby(["m", "a"])["v"].sum()
    )
    for _, r in table.iterrows():
        assert r["sb_actual"] == pytest.approx(float(manual.loc[(r["month_id"], r["admin1_code"])]))


def test_actual_is_nan_when_no_historical_is_supplied():
    table = build_bulk_table(_forecast())  # forecast horizon: no observed
    assert list(table.columns) == schema.bulk_columns()
    for s in schema.SERIES:
        assert table[f"{s}_actual"].isna().all()


def test_forecast_values_are_finite_and_nested():
    table = build_bulk_table(_forecast(), _historical())
    for s in schema.SERIES:
        assert np.isfinite(table[f"{s}_map"]).all()
        # nested 50 ⊆ 90 ⊆ 95
        assert (table[f"{s}_hdi50_lower"] >= table[f"{s}_hdi90_lower"] - 1e-9).all()
        assert (table[f"{s}_hdi90_lower"] >= table[f"{s}_hdi95_lower"] - 1e-9).all()


def test_parquet_round_trip_preserves_schema(tmp_path):
    p = write_bulk_parquet(tmp_path / "bulk.parquet", _forecast(), _historical())
    back = pd.read_parquet(p)
    assert list(back.columns) == schema.bulk_columns()
    assert len(back) == len(build_bulk_table(_forecast(), _historical()))


def test_two_targets_mapping_to_one_series_fails_loud():
    # a `_best` + `_prob` style pair both resolving to `sb` must not silently collide
    fds = ForecastDataset(
        make_fao_df(n_samples=16, targets=("pred_lr_ged_sb_best", "pred_lr_ged_sb_prob"))
    )
    with pytest.raises(ValueError, match="same series"):
        build_bulk_table(fds)


def test_broken_admin_hierarchy_fails_loud():
    # an admin-1 code mapping to two different countries is a broken hierarchy
    df = make_fao_df(n_cells=4, n_months=1, n_samples=16, targets=_FC)
    df["admin1_gaul1_code"] = [1, 1, 1, 1]  # same admin1...
    df["admin1_gaul0_code"] = [10, 20, 10, 20]  # ...but two countries
    with pytest.raises(ValueError, match="broken hierarchy"):
        build_bulk_table(ForecastDataset(df))


# --- endpoint: GET /data/forecast/bulk returns the parquet -----------------------------------
def test_bulk_endpoint_returns_the_parquet(tmp_path):
    import io
    import time
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from views_crafdapi.data.handlers import ForecastDataset
    from views_crafdapi.managers.api import CrafdApiManager

    mgr = CrafdApiManager.from_config(
        {"deployment": {"host": "0.0.0.0", "port": 80}}, cache_dir=tmp_path / "cache"
    )
    mgr._prediction_bucket_id = "test-bucket"
    mgr.app = FastAPI()
    mgr._register_routes()

    fds = ForecastDataset(make_fao_df(n_samples=32, seed=1, targets=_FC))
    hds = ForecastDataset(make_fao_df(n_samples=1, seed=2, targets=_HT), targets=list(_HT))
    h = mgr._get_api_key_hash("test-api-key")
    def mk(d):
        # S1 (#264): a warm forecast serves only as a manifested wire entry (source_kind="wire").
        return {"data": d.dataframe, "file_id": "f", "timestamp": time.time(),
                "dataset": d, "source_kind": "wire"}

    mgr._dataframe_cache[h] = {"forecast": mk(fds), "historical": mk(hds)}
    mgr._validate_api_key = MagicMock(return_value=MagicMock())
    pm = MagicMock()
    pm.get_latest_manifest.return_value = {"fileId": "f", "filename": "m.json",
        "type": "sampled_forecast_manifest", "category": "forecast", "name": "un_crafd"}
    mgr.app.dependency_overrides[mgr._get_prediction_manager] = lambda: pm
    mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: MagicMock()

    resp = TestClient(mgr.app).get("/data/forecast/bulk", headers={"X-API-Key": "test-api-key"})
    assert resp.status_code == 200, resp.text
    back = pd.read_parquet(io.BytesIO(resp.content))
    assert list(back.columns) == schema.bulk_columns()
    assert back["sb_actual"].notna().any()  # historical present → actuals populated
