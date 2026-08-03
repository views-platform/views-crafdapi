"""S5 (#159 / epic #154): the value round-trip is byte-identical.

`ForecastDataset.to_value(dir)` → `from_value(dir)` must reconstruct a dataset that
**serves byte-identical output** — without pickling the object (C-149) and without
resurrecting object-dtype sample cells (C-148). This is the gate that must be green
before the disk cache is rewritten onto the value format. Synthetic data only.
"""
import os
import tempfile

import numpy as np
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset

pytestmark = pytest.mark.layer2_data


def _ragged_wire_dataset(with_nan_geo):
    """A *wire*-assembled dataset (via wire_reader.build_dataset, NOT the constructor path) with
    two targets over disjoint cell sets — so each target is zero-filled on the other's cells —
    and an optionally-NaN GAUL sidecar. Exercises the target-raggedness zero-fill + NaN geography
    through to_value/from_value, which the make_fao_df corpus never covers (S6a / #208)."""
    from views_frames.io import arrow

    from views_crafdapi.forecast.ingestion import wire_reader

    from ._wire_fixtures import _build_sidecar, _sha256

    s, month = 4, 543
    units_a = np.array([100001, 100002, 100003], dtype=np.int64)
    units_b = np.array([100004, 100005, 100006], dtype=np.int64)  # disjoint → cross-target zero-fill
    all_units = np.arange(100001, 100007, dtype=np.int64)

    def shard(target, units_):
        vals = np.ones((len(units_), s), dtype=np.float32) * (7.0 if target == "lr_ged_sb" else 9.0)
        header = {"contract_version": "1.5", "sample_count": s, "target": target, "time_id": month,
                  "id_semantics": {"time": "views_month_id", "unit": "priogrid_id"}}
        p = tempfile.mktemp(suffix=".parquet")
        arrow.save(p, values=vals, time=np.full(len(units_), month, dtype=np.int64),
                   unit=units_, level="pgm", metadata=header)
        b = open(p, "rb").read()
        os.unlink(p)
        return b

    b_a, b_b = shard("lr_ged_sb", units_a), shard("lr_ged_ns", units_b)
    manifest = wire_reader.parse_run_manifest({
        "run_id": "r", "targets": ["lr_ged_sb", "lr_ged_ns"], "expected_months": [month],
        "expected_cell_count": 3,
        "shards": [
            {"name": "a", "target": "lr_ged_sb", "time_id": month, "sha256": _sha256(b_a)},
            {"name": "b", "target": "lr_ged_ns", "time_id": month, "sha256": _sha256(b_b)},
        ],
        "sidecar": {"name": "sc", "sha256": ""},
    })
    _, sc_bytes = _build_sidecar(all_units, "r", with_nan_geo=with_nan_geo)
    states = [wire_reader.load_shard_state(b_a), wire_reader.load_shard_state(b_b)]
    return wire_reader.build_dataset(states, wire_reader.read_sidecar(sc_bytes), manifest)


@pytest.mark.parametrize("with_nan_geo", [False, True])
def test_wire_ragged_value_roundtrip_store_geo_and_hdi_identical(with_nan_geo, tmp_path):
    """S6a (#208) GATE before wire runs are trusted on disk: a build_dataset output with
    target-raggedness zero-fills and NaN GAUL geography must survive to_value → from_value
    byte-identically (npz store + geo.parquet), including served HDI/MAP across aggregate × level."""
    ds = _ragged_wire_dataset(with_nan_geo)
    ds.to_value(tmp_path / "v")
    out = ForecastDataset.from_value(tmp_path / "v")

    assert out.is_prediction and out.targets == ds.targets
    assert out.dataframe.index.equals(ds.dataframe.index)
    for var in ds.targets:
        assert out._sample_store[var].dtype == np.float32
        assert np.array_equal(out._sample_store[var], ds._sample_store[var], equal_nan=True)
    # NaN GAUL geography must survive the geo.parquet round-trip
    assert out.geo_metadata.equals(ds.geo_metadata)
    for level in [None, "country", "gaul0", "gaul1", "gaul2"]:
        kw = {"aggregate": True, "level": level} if level is not None else {}
        assert ds.calculate_hdi_map(**kw).equals(out.calculate_hdi_map(**kw)), f"hdi diverges (level={level})"

# (n_cells, n_months, n_samples, seed) — even/odd S, varied grid (matches the parity corpus).
_CORPUS = [(4, 2, 64, 1), (4, 2, 65, 2), (3, 3, 100, 3), (4, 1, 257, 4), (2, 2, 2, 5)]

_LEVELS = [None, "country", "gaul0", "gaul1", "gaul2"]


def _make_feature_df(n_cells=4, n_months=2, seed=7):
    """A historical/scalar (feature) source: the 9 geo-metadata columns + a float64
    scalar target, no pred_* columns (so the dataset is is_prediction=False)."""
    base = make_fao_df(n_cells=n_cells, n_months=n_months, n_samples=1, seed=seed)
    feat = base.drop(columns=[c for c in base.columns if c.startswith("pred_")])
    rng = np.random.default_rng(seed)
    feat["ged_sb"] = rng.gamma(2.0, 3.0, size=len(feat)).astype(np.float64)
    return feat


@pytest.mark.parametrize("n_cells,n_months,n_samples,seed", _CORPUS)
def test_prediction_value_roundtrip_store_and_geo_identical(
    n_cells, n_months, n_samples, seed, tmp_path
):
    ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=n_months,
                                     n_samples=n_samples, seed=seed))
    ds.to_value(tmp_path / "v")
    out = ForecastDataset.from_value(tmp_path / "v")

    assert out.is_prediction and out.targets == ds.targets
    assert out.sample_size == ds.sample_size
    assert out.dataframe.index.equals(ds.dataframe.index)
    for var in ds.targets:
        assert out._sample_store[var].dtype == np.float32
        assert np.array_equal(out._sample_store[var], ds._sample_store[var], equal_nan=True)
    assert out.geo_metadata.equals(ds.geo_metadata)
    # C-155 alignment survives the round-trip
    assert all(len(out._sample_store[v]) == len(out.dataframe) for v in out.targets)


@pytest.mark.parametrize("n_cells,n_months,n_samples,seed", _CORPUS)
def test_prediction_value_roundtrip_serves_identical_hdi_map(
    n_cells, n_months, n_samples, seed, tmp_path
):
    """The real serving contract: HDI/MAP across aggregate × level × with_metadata."""
    ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=n_months,
                                     n_samples=n_samples, seed=seed))
    ds.to_value(tmp_path / "v")
    out = ForecastDataset.from_value(tmp_path / "v")

    for with_metadata in (True, False):
        # cell-level (no aggregation)
        a = ds.calculate_hdi_map(with_metadata=with_metadata)
        b = out.calculate_hdi_map(with_metadata=with_metadata)
        assert a.equals(b), f"cell-level hdi diverges (with_metadata={with_metadata})"
        # aggregated to each geographic level
        for level in [lv for lv in _LEVELS if lv is not None]:
            a = ds.calculate_hdi_map(aggregate=True, level=level, with_metadata=with_metadata)
            b = out.calculate_hdi_map(aggregate=True, level=level, with_metadata=with_metadata)
            assert a.equals(b), f"aggregated hdi diverges (level={level}, meta={with_metadata})"


def test_prediction_value_roundtrip_subset_identical(tmp_path):
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=32, seed=9))
    ds.to_value(tmp_path / "v")
    out = ForecastDataset.from_value(tmp_path / "v")
    t0 = ds._time_values.tolist()[0]
    for kwargs in ({}, {"time_ids": t0}, {"sample_idx": [0, 5, 31]}):
        a = ds.get_subset_dataframe(**kwargs)
        b = out.get_subset_dataframe(**kwargs)
        assert a.index.equals(b.index) and list(a.columns) == list(b.columns)
        for var in ds.targets:
            for x, y in zip(a[var].to_numpy(), b[var].to_numpy()):
                assert np.array_equal(np.asarray(x), np.asarray(y), equal_nan=True)


def test_feature_value_roundtrip_identical(tmp_path):
    """Historical/scalar path (S4e): the float64 values round-trip bit-for-bit through the
    contiguous `_feature_store` (NOT resurrected as object cells, NOT frame-ized to float32)."""
    ds = ForecastDataset(_make_feature_df(), targets=["ged_sb"])
    assert not ds.is_prediction and ds._sample_store == {}
    assert "ged_sb" in ds._feature_store and "ged_sb" not in ds.dataframe.columns
    ds.to_value(tmp_path / "v")
    out = ForecastDataset.from_value(tmp_path / "v")

    assert not out.is_prediction and out.targets == ["ged_sb"]
    assert out.dataframe.index.equals(ds.dataframe.index)
    assert set(out._feature_store) == set(ds._feature_store)
    for col in ds._feature_store:
        x, y = ds._feature_store[col], out._feature_store[col]
        assert x.dtype == np.dtype(np.float64) and np.asarray(y).dtype == np.dtype(np.float64)
        np.testing.assert_array_equal(np.asarray(y), x)  # bit-for-bit, float64 preserved
    # and the served subset is byte-identical object-cell-for-object-cell
    a = ds.get_subset_dataframe(with_metadata=False)
    b = out.get_subset_dataframe(with_metadata=False)
    assert list(a.columns) == list(b.columns)
    for col in a.columns:
        for x, y in zip(a[col].to_numpy(), b[col].to_numpy()):
            assert np.array_equal(np.asarray(x), np.asarray(y), equal_nan=True)
    assert out.geo_metadata.equals(ds.geo_metadata)


def test_value_dir_has_no_pickle(tmp_path):
    """The value layout is npz/parquet/json — never a pickle blob."""
    ds = ForecastDataset(make_fao_df(seed=2))
    ds.to_value(tmp_path / "v")
    written = {p.suffix for p in (tmp_path / "v").rglob("*") if p.is_file()}
    assert ".pkl" not in written and ".pickle" not in written
    assert {".json", ".parquet", ".npz", ".npy"} & written
