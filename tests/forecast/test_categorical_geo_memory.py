"""Memory fix (v1.3.10): the geography name/ISO3 columns are stored as `category`.

At global-historical scale (~28M rows) the four low-cardinality string metadata columns
(`country_iso_a3`, `admin1_gaul1_name`, `admin1_gaul0_name`, `admin2_gaul2_name`) held as
pandas `object` cost ~6-7 GB and OOM-killed the worker. Cast to `category` they are a small
codes array + a tiny dictionary. These tests lock:

1. the dtype invariant (names categorical, codes numeric),
2. the memory reduction (bounded synthetic — never the live dataset),
3. that `country`-level aggregation still returns ONLY observed countries — the categorical
   group key must be `observed=True` or a single-country request injects phantom rows / a
   KeyError in the frame-native sum (the exact scale bug this fix could have introduced),
4. that plausibility, the historical subset values, and the value round-trip are unchanged.
"""
import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset

pytestmark = pytest.mark.layer2_data

_CAT_COLS = ["country_iso_a3", "admin1_gaul1_name", "admin1_gaul0_name", "admin2_gaul2_name"]
_CODE_COLS = ["admin1_gaul1_code", "admin1_gaul0_code", "admin2_gaul2_code"]


def _make_feature_df(n_cells=4, n_months=2, seed=7):
    """A historical/scalar (feature) source: geo-metadata + a float64 scalar target."""
    base = make_fao_df(n_cells=n_cells, n_months=n_months, n_samples=1, seed=seed)
    feat = base.drop(columns=[c for c in base.columns if c.startswith("pred_")])
    rng = np.random.default_rng(seed)
    feat["ged_sb"] = rng.gamma(2.0, 3.0, size=len(feat)).astype(np.float64)
    return feat


def test_geo_name_columns_are_categorical_forecast_and_historical():
    for ds in (ForecastDataset(make_fao_df()),
               ForecastDataset(_make_feature_df(), targets=["ged_sb"])):
        for col in _CAT_COLS:
            assert isinstance(ds.geo_metadata[col].dtype, pd.CategoricalDtype), col
        for col in _CODE_COLS:
            assert not isinstance(ds.geo_metadata[col].dtype, pd.CategoricalDtype), col
            assert pd.api.types.is_numeric_dtype(ds.geo_metadata[col]), col


def test_categorical_geo_uses_far_less_memory_than_object():
    """Bounded synthetic (never the 28M-row live set): the repeated low-cardinality strings
    cost a fraction of the object representation."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=6000, n_samples=1))  # 24k rows, 2 countries
    for col in _CAT_COLS:
        cat_bytes = ds.geo_metadata[col].memory_usage(deep=True)
        obj_bytes = ds.geo_metadata[col].astype(object).memory_usage(deep=True)
        assert cat_bytes < 0.2 * obj_bytes, f"{col}: {cat_bytes} not << {obj_bytes}"


def test_single_country_aggregation_returns_only_that_country():
    """Regression: `country` level groups by the categorical `country_iso_a3`. Requesting ONE
    country must not surface phantom rows for the other categories still carried in the dtype
    (the `observed=True` guard). Fixture has AAA + BBB; we ask for AAA."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=8,
                                     targets=("pred_lr_ged_sb",)))
    agg = ds.get_subset_dataframe(entity_ids=["AAA"], level="country", aggregate=True)
    countries = set(agg.index.get_level_values("country_iso_a3"))
    assert countries == {"AAA"}, countries
    assert not agg.isna().any().any()  # no phantom NaN rows
    assert len(agg) == 2  # one row per month, no cartesian blow-up


def test_all_countries_aggregation_covers_every_observed_country():
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=8,
                                     targets=("pred_lr_ged_sb",)))
    agg = ds.get_subset_dataframe(level="country", aggregate=True)
    assert set(agg.index.get_level_values("country_iso_a3")) == {"AAA", "BBB"}
    assert len(agg) == 4  # 2 countries x 2 months, no empty groups


def test_plausibility_passes_on_categorical_geo():
    ForecastDataset(make_fao_df()).validate_metadata_plausibility()  # must not raise


def test_historical_subset_values_unchanged_by_categorical():
    """The wire-facing historical subset still serves the exact float64 observed values —
    the categorical cast touches only geo_metadata, not the target column. The reconstructed
    subset cells (float64 size-1 arrays) match the canonical `_sample_array` read row-for-row."""
    ds = ForecastDataset(_make_feature_df(n_cells=4, n_months=2), targets=["ged_sb"])
    sub = ds.get_subset_dataframe(features=["ged_sb"])
    got = np.array([np.asarray(v).ravel()[0] for v in sub["ged_sb"].to_numpy()])
    want = ds._sample_array("ged_sb")[:, 0].astype(np.float64)
    assert got.shape == want.shape
    assert np.allclose(got, want, equal_nan=True)


def test_construction_from_already_categorical_source_with_geo_backfill():
    """Production path: dataset_service casts the geo columns to `category` BEFORE constructing
    the dataset, so __init__'s metadata-backfill loop runs against a categorical geo_metadata
    (assigning an entity's values into rows that dense-grid fill added). A dropped (month,cell)
    forces that fill + backfill — it must not raise on the categorical and must recover the
    correct label, not NaN."""
    df = make_fao_df(n_cells=4, n_months=2, n_samples=1, targets=("pred_lr_ged_sb",))
    for c in _CAT_COLS:
        df[c] = df[c].astype("category")
    df = df.drop(df.index[3])  # drop (600, 103) → dense-grid re-adds it → geo backfill fires
    ds = ForecastDataset(df)
    assert len(ds.dataframe) == 8  # the missing cell was re-added
    assert not ds.geo_metadata[_CAT_COLS].isna().any().any()
    assert ds.geo_metadata.loc[(600, 103), "country_iso_a3"] == "BBB"
    for c in _CAT_COLS:
        assert isinstance(ds.geo_metadata[c].dtype, pd.CategoricalDtype), c
    ds.validate_metadata_plausibility()  # must not raise


def test_categorical_geo_roundtrips_through_value(tmp_path):
    ds = ForecastDataset(_make_feature_df(), targets=["ged_sb"])
    ds.to_value(tmp_path / "v")
    out = ForecastDataset.from_value(tmp_path / "v")
    for col in _CAT_COLS:
        assert isinstance(out.geo_metadata[col].dtype, pd.CategoricalDtype), col
    assert out.geo_metadata.equals(ds.geo_metadata)
