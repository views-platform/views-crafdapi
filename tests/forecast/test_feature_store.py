"""Fix A / S4e (#154 follow-on): the historical/scalar **feature** path stores its values in a
compact contiguous ``_feature_store`` (float64 ``(N,1)`` per column) instead of per-cell object
numpy arrays — mirroring the S4d ``_sample_store`` for predictions. The object cells are dropped
from ``.dataframe`` (which becomes index-only), and subsets are rebuilt on demand.

Why: at global-historical scale (~28M rows) the per-cell object arrays cost ~10 GB (~15x the raw
float64), which OOM-killed the worker. The store is the raw float64 block. Serving is byte-identical:
the reconstructed subset cells are float64 size-1 arrays, which the serializer flattens to scalars
exactly as before.
"""
import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset

pytestmark = pytest.mark.layer2_data


def _feature_df(n_cells=4, n_months=3, seed=7):
    """Historical/scalar source: the 9 geo-metadata cols + two float64 scalar targets, no pred_*."""
    base = make_fao_df(n_cells=n_cells, n_months=n_months, n_samples=1, seed=seed)
    feat = base.drop(columns=[c for c in base.columns if c.startswith("pred_")])
    rng = np.random.default_rng(seed)
    feat["lr_ged_sb"] = rng.gamma(2.0, 3.0, size=len(feat)).astype(np.float64)
    feat["lr_ged_ns"] = rng.gamma(1.5, 2.0, size=len(feat)).astype(np.float64)
    return feat


def _targets():
    return ["lr_ged_sb", "lr_ged_ns"]


def test_feature_values_live_in_feature_store_not_object_cells():
    ds = ForecastDataset(_feature_df(), targets=_targets())
    assert not ds.is_prediction
    assert ds._sample_store == {}
    # target columns are no longer resident object cells in .dataframe
    for t in _targets():
        assert t not in ds.dataframe.columns
        assert t in ds._feature_store
        block = ds._feature_store[t]
        assert block.dtype == np.float64
        assert block.shape == (len(ds.dataframe), 1)
        assert block.flags["C_CONTIGUOUS"]
    # nothing object-dtype left in the frame (it is index-only for pure-scalar historical)
    assert not any(pd.api.types.is_object_dtype(dt) for dt in ds.dataframe.dtypes)


def test_feature_store_is_memory_lean_vs_object_cells():
    """Bounded synthetic: the contiguous store is a small fraction of the per-cell object form."""
    import sys

    ds = ForecastDataset(_feature_df(n_cells=4, n_months=4000), targets=_targets())  # 16k rows
    n = len(ds.dataframe)
    store_bytes = sum(b.nbytes for b in ds._feature_store.values())
    assert store_bytes == n * len(_targets()) * 8  # exact float64 (N,1) per target
    # The pre-S4e form was one size-1 ndarray object per cell — measure its true footprint
    # (Python object overhead + an 8 B pointer), not just the payload.
    per_cell = sys.getsizeof(np.array([1.0])) + 8
    obj_equiv = n * len(_targets()) * per_cell
    assert store_bytes < 0.15 * obj_equiv


def test_historical_subset_is_byte_identical_object_cells():
    ds = ForecastDataset(_feature_df(n_cells=4, n_months=3), targets=_targets())
    sub = ds.get_subset_dataframe(with_metadata=False)  # feature columns only
    assert list(sub.columns) == _targets()
    for t in _targets():
        cells = sub[t].to_numpy()
        for i, v in enumerate(cells):
            a = np.asarray(v)
            assert a.dtype == np.float64 and a.shape == (1,)
            assert a[0] == ds._feature_store[t][i, 0]


def test_historical_subset_row_and_feature_filters():
    ds = ForecastDataset(_feature_df(n_cells=4, n_months=3), targets=_targets())
    t0 = int(ds.dataframe.index.get_level_values(0)[0])
    sub_t = ds.get_subset_dataframe(time_ids=t0, with_metadata=False)
    assert (sub_t.index.get_level_values(0) == t0).all()
    assert len(sub_t) == int((ds.dataframe.index.get_level_values(0) == t0).sum())
    one = ds.get_subset_dataframe(features="lr_ged_sb", with_metadata=False)
    assert list(one.columns) == ["lr_ged_sb"]


def test_sample_array_feature_branch_returns_float32_contract():
    """`_sample_array` must keep its float32 contract (bulk_parquet / to_frames rely on it)."""
    ds = ForecastDataset(_feature_df(), targets=_targets())
    arr = ds._sample_array("lr_ged_sb")
    assert arr.dtype == np.float32 and arr.shape == (len(ds.dataframe), 1)
    np.testing.assert_allclose(arr[:, 0], ds._feature_store["lr_ged_sb"][:, 0], rtol=1e-5)


def test_value_roundtrip_via_feature_store(tmp_path):
    ds = ForecastDataset(_feature_df(), targets=_targets())
    ds.to_value(tmp_path / "v")
    out = ForecastDataset.from_value(tmp_path / "v")
    assert not out.is_prediction
    for t in _targets():
        assert t not in out.dataframe.columns and t in out._feature_store
        np.testing.assert_array_equal(out._feature_store[t], ds._feature_store[t])
        assert out._feature_store[t].dtype == np.float64
    # subset served identically after the round-trip
    a, b = ds.get_subset_dataframe(), out.get_subset_dataframe()
    assert list(a.columns) == list(b.columns)
    for t in _targets():
        for x, y in zip(a[t].to_numpy(), b[t].to_numpy()):
            assert np.array_equal(np.asarray(x), np.asarray(y), equal_nan=True)
    assert out.geo_metadata.equals(ds.geo_metadata)


def test_from_value_does_not_resurrect_object_cells(tmp_path):
    ds = ForecastDataset(_feature_df(), targets=_targets())
    ds.to_value(tmp_path / "v")
    out = ForecastDataset.from_value(tmp_path / "v")
    assert not any(pd.api.types.is_object_dtype(dt) for dt in out.dataframe.dtypes)
    assert out._sample_store == {}


def test_value_dir_has_no_pickle_feature(tmp_path):
    ds = ForecastDataset(_feature_df(), targets=_targets())
    ds.to_value(tmp_path / "v")
    written = {p.suffix for p in (tmp_path / "v").rglob("*") if p.is_file()}
    assert ".pkl" not in written and ".pickle" not in written
    assert {".json", ".parquet", ".npz", ".npy"} & written
