"""Phase 1 (#88): the extracted ingestion modules behave as standalone units."""

import numpy as np
import pandas as pd
import pytest

from views_crafdapi.forecast.ingestion.dense_grid import fill_dense_grid
from views_crafdapi.forecast.ingestion.parquet_reader import to_array_columns
from views_crafdapi.forecast.ingestion.plausibility import (
    assert_geo_metadata_plausible,
    assert_prediction_samples_plausible,
)
from tests.conftest import make_fao_df

pytestmark = pytest.mark.layer2_data


# ── parquet_reader ──────────────────────────────────────────────────────────────
def test_to_array_columns_converts_lists():
    df = pd.DataFrame({"a": [[1, 2], [3, 4]], "b": [1.0, 2.0]})
    out = to_array_columns(df)
    assert isinstance(out["a"].iloc[0], np.ndarray)
    assert out["b"].iloc[0] == 1.0  # scalar columns untouched


# ── plausibility ────────────────────────────────────────────────────────────────
def test_prediction_samples_ok():
    assert_prediction_samples_plausible("pred_x", np.array([[0.0, 1.0], [2.0, 3.0]]))  # no raise


def test_prediction_samples_reject_negative():
    with pytest.raises(ValueError, match="negative"):
        assert_prediction_samples_plausible("pred_x", np.array([[0.0, -1.0]]))


def test_prediction_samples_reject_nonfinite():
    with pytest.raises(ValueError, match="non-finite"):
        assert_prediction_samples_plausible("pred_x", np.array([[0.0, np.inf]]))


def test_geo_metadata_ok_and_bad():
    gm = make_fao_df().loc[:, ["pg_xcoord", "pg_ycoord", "country_iso_a3",
                               "admin1_gaul1_code", "admin1_gaul0_code", "admin2_gaul2_code"]]
    assert_geo_metadata_plausible(gm)  # no raise
    bad = gm.copy()
    bad.iloc[0, bad.columns.get_loc("pg_xcoord")] = 9999.0
    with pytest.raises(ValueError, match="pg_xcoord"):
        assert_geo_metadata_plausible(bad)


def test_geo_metadata_allows_negative_gaul_disputed_codes():
    """#287 follow-up: GAUL assigns negative unit codes to disputed territories (e.g.
    -3727…-3730 in the global land_gaul grid). These are legitimate, not corruption — a
    non-negative assert here refused the entire global run over 6 cells."""
    gm = make_fao_df().loc[:, ["pg_xcoord", "pg_ycoord", "country_iso_a3",
                               "admin1_gaul1_code", "admin1_gaul0_code", "admin2_gaul2_code"]]
    gm.iloc[0, gm.columns.get_loc("admin1_gaul1_code")] = -3730
    gm.iloc[0, gm.columns.get_loc("admin2_gaul2_code")] = -3730
    assert_geo_metadata_plausible(gm)  # no raise — negative GAUL codes are valid


# ── dense_grid ──────────────────────────────────────────────────────────────────
def _grid_df():
    idx = pd.MultiIndex.from_product([[600, 601], [100, 101]], names=["month_id", "priogrid_id"])
    return pd.DataFrame({"pred_x": [np.arange(3, dtype=float) for _ in range(4)]}, index=idx)


def test_fill_dense_grid_recreates_with_array():
    df = _grid_df().drop(index=(600, 100))  # entity 100 still in last step (601)
    out = fill_dense_grid(df, pd.Index([600, 601]), "month_id", "priogrid_id", 0)
    cell = out.loc[(600, 100), "pred_x"]
    assert isinstance(cell, np.ndarray) and cell.shape == (3,) and np.all(cell == 0.0)


def test_fill_dense_grid_fails_loud_on_dropped_entity():
    df = _grid_df().drop(index=(601, 101))  # entity 101 now absent from last step
    with pytest.raises(ValueError, match="C-87"):
        fill_dense_grid(df, pd.Index([600, 601]), "month_id", "priogrid_id", 0)


def test_prediction_samples_checked_across_chunk_boundaries():
    """C-277 (#168): the check walks the block in row chunks (no float64 copy of a GB-scale
    memmap). A fault anywhere, including the last row of a later chunk, is found and counted
    in full; float32 input is checked as it is."""
    from views_crafdapi.forecast.ingestion import plausibility

    arr = np.ones((10, 3), dtype=np.float32)
    arr[0, 0] = np.nan
    arr[9, 2] = np.inf
    with pytest.raises(ValueError, match="2 non-finite"):
        plausibility.assert_prediction_samples_plausible("pred_x", arr, chunk_rows=4)
    arr = np.ones((10, 3), dtype=np.float32)
    arr[9, 2] = -2.5
    with pytest.raises(ValueError, match=r"min -2\.5"):
        plausibility.assert_prediction_samples_plausible("pred_x", arr, chunk_rows=4)
    plausibility.assert_prediction_samples_plausible("pred_x", np.ones(5), chunk_rows=2)  # 1-D ok
    for row in (0, 3, 7):  # an early chunk's minimum must survive later chunks; each chunk's last row
        arr = np.ones((10, 3), dtype=np.float32)
        arr[row, 1] = -1.0
        with pytest.raises(ValueError, match="negative"):
            plausibility.assert_prediction_samples_plausible("pred_x", arr, chunk_rows=4)
        arr[row, 1] = np.nan
        with pytest.raises(ValueError, match="1 non-finite"):
            plausibility.assert_prediction_samples_plausible("pred_x", arr, chunk_rows=4)
    plausibility.assert_prediction_samples_plausible("pred_x", np.ones((3, 0)), chunk_rows=2)  # no samples
    plausibility.assert_prediction_samples_plausible("pred_x", np.float32(3.0))  # scalar
    with pytest.raises(ValueError, match="1 non-finite"):
        plausibility.assert_prediction_samples_plausible("pred_x", np.float32("nan"))


def test_the_sample_check_never_copies_the_whole_block():
    """C-277 (#168): the disk read runs this over a ~1.2 GB float32 memmap per target, so the
    check must hold at most one chunk at a time — never a float64 copy of the block (the
    ~2.4 GB per target the old upcast cost). 400,000 x 32 float32 is 51 MB; a float64 copy
    would be 102 MB, one chunk's masks are ~2 MB."""
    import tracemalloc

    from views_crafdapi.forecast.ingestion import plausibility

    arr = np.ones((400_000, 32), dtype=np.float32)
    tracemalloc.start()
    try:
        plausibility.assert_prediction_samples_plausible("pred_x", arr)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 20 * 2**20, f"peak {peak / 2**20:.0f} MB: the check copied the block"


def test_the_dataset_hands_the_stored_samples_to_the_check_uncopied(monkeypatch):
    """C-277 (#168): `validate_value_plausibility` passes the float32 store itself, not an
    upcast copy — the chunked check is pointless if its caller materialises the block first."""
    from views_crafdapi.data.handlers import ForecastDataset, grid_dataset

    ds = ForecastDataset(make_fao_df())
    seen = []
    monkeypatch.setattr(grid_dataset, "assert_prediction_samples_plausible",
                        lambda var, samples: seen.append((var, samples)))
    ds.validate_value_plausibility()
    assert [v for v, _ in seen] == list(ds.pred_vars)
    for var, samples in seen:
        assert samples is ds._sample_array(var)
