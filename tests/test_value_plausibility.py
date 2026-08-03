"""C-72: the ingestion path rejects schema-valid-but-implausible prediction values
(non-finite or negative fatality samples) before they are cached and served to FAO.

These drive the **legacy download+parse path** (via `/historical`) — post-S1 (#264) a *forecast*
is served only from a manifested wire run, where the same `validate_value_plausibility` /
`validate_metadata_plausibility` guards run in `wire_reader.build_dataset` (see
`test_wire_golden_fixture.py`). The validation logic is shared, so exercising it on the legacy path
covers both."""

import io

import numpy as np
import pytest
from fastapi import HTTPException

from tests.conftest import make_fao_df
from tests.test_format_cascade import _manager_with_bytes

pytestmark = pytest.mark.layer2_data


def _parquet_bytes(df):
    buf = io.BytesIO()
    df.to_parquet(buf)
    return buf.getvalue()


def test_plausible_forecast_passes(tmp_path):
    df = make_fao_df(n_cells=4, n_months=2, n_samples=5, seed=3)  # rng.random -> non-negative
    mgr, mock_pm = _manager_with_bytes(tmp_path, _parquet_bytes(df))
    result = mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
    assert result is not None


def test_negative_prediction_is_rejected(tmp_path):
    df = make_fao_df(n_cells=4, n_months=2, n_samples=5, seed=3)
    idx = df.index[0]
    bad = np.asarray(df.at[idx, "pred_test"], dtype=float).copy()
    bad[0] = -1.0  # a single negative fatality sample
    df.at[idx, "pred_test"] = bad

    mgr, mock_pm = _manager_with_bytes(tmp_path, _parquet_bytes(df))
    with pytest.raises(HTTPException) as exc:
        mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
    assert exc.value.status_code == 500


def test_nonfinite_prediction_is_rejected(tmp_path):
    df = make_fao_df(n_cells=4, n_months=2, n_samples=5, seed=3)
    idx = df.index[1]
    bad = np.asarray(df.at[idx, "pred_other"], dtype=float).copy()
    bad[2] = np.inf
    df.at[idx, "pred_other"] = bad

    mgr, mock_pm = _manager_with_bytes(tmp_path, _parquet_bytes(df))
    with pytest.raises(HTTPException) as exc:
        mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
    assert exc.value.status_code == 500


# ── C-72 metadata facet: geographic-metadata plausibility ───────────────────────
from views_crafdapi.data.handlers import ForecastDataset  # noqa: E402


def _ds(seed=3):
    return ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=5, seed=seed))


def _ds_iso3(iso3_by_pos, seed=3):
    """Construct a dataset after writing ISO3 codes into the SOURCE at the given row positions,
    so the (now `category`-dtype) `country_iso_a3` column legitimately carries the value — the
    same way production reads sentinels/codes from the parquet. Post-construction mutation is
    not possible on a categorical without first extending its categories."""
    df = make_fao_df(n_cells=4, n_months=2, n_samples=5, seed=seed)
    loc = df.columns.get_loc("country_iso_a3")
    for pos, val in iso3_by_pos.items():
        df.iloc[pos, loc] = val
    return ForecastDataset(df)


def test_metadata_plausibility_passes_on_good_fixture():
    _ds().validate_metadata_plausibility()  # must not raise


def test_out_of_range_longitude_rejected():
    ds = _ds()
    ds.geo_metadata.iloc[0, ds.geo_metadata.columns.get_loc("pg_xcoord")] = 999.0
    with pytest.raises(ValueError, match="pg_xcoord"):
        ds.validate_metadata_plausibility()


def test_out_of_range_latitude_rejected():
    ds = _ds()
    ds.geo_metadata.iloc[0, ds.geo_metadata.columns.get_loc("pg_ycoord")] = -120.0
    with pytest.raises(ValueError, match="pg_ycoord"):
        ds.validate_metadata_plausibility()


def test_malformed_iso3_rejected():
    ds = _ds_iso3({0: "USA1"})
    with pytest.raises(ValueError, match="country_iso_a3"):
        ds.validate_metadata_plausibility()


def test_no_country_sentinel_is_accepted():
    """Regression (2026-07-20 production 500): '-99' is the VIEWS "no country" sentinel
    for ocean/ungoverned cells — real historical data carries ~27k of them. It is missing
    metadata, not a malformed code, so plausibility must accept it (rows are dropped later
    at aggregation). A too-strict check here rejected the entire real dataset."""
    ds = _ds_iso3({0: "-99"})
    ds.validate_metadata_plausibility()  # must not raise


def test_sentinel_acceptance_does_not_mask_real_malformed_codes():
    """The sentinel exemption must not weaken the check for genuinely bad codes."""
    ds = _ds_iso3({0: "-99", 1: "1234"})  # 0 = legitimate sentinel, 1 = genuinely malformed
    with pytest.raises(ValueError, match="country_iso_a3"):
        ds.validate_metadata_plausibility()


def test_negative_gaul_code_accepted():
    """#287 follow-up: GAUL assigns negative unit codes to disputed territories (e.g. -3727…-3730
    in the global land_gaul grid). They are legitimate — the old non-negative assert refused the
    entire global run over 6 such cells. Coordinate/ISO plausibility still applies."""
    ds = _ds()
    ds.geo_metadata.iloc[0, ds.geo_metadata.columns.get_loc("admin1_gaul1_code")] = -3730
    ds.validate_metadata_plausibility()  # no raise — negative GAUL codes are valid


def test_bad_metadata_rejected_at_ingestion(tmp_path):
    df = make_fao_df(n_cells=4, n_months=2, n_samples=5, seed=3)
    df.loc[df.index[0], "pg_xcoord"] = 9999.0  # impossible longitude
    mgr, mock_pm = _manager_with_bytes(tmp_path, _parquet_bytes(df))
    with pytest.raises(HTTPException) as exc:
        mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
    assert exc.value.status_code == 500
