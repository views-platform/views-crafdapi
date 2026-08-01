"""_GridDataset and FAO_PGMDataset invariant tests (C-57).

Validates CIC guarantees for the core domain class hierarchy:
the generic `_GridDataset` base → the `ForecastDataset` (FAO_PGMDataset) facade.
"""

import copy

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import _GridDataset, FAO_PGMDataset

pytestmark = pytest.mark.layer2_data


def _make_raw_multiindex_df(n_cells=4, n_months=2, n_samples=10, seed=42):
    """Minimal 2-level MultiIndex DataFrame with array-valued columns (no metadata cols)."""
    rng = np.random.default_rng(seed)
    rows = n_cells * n_months
    data = {
        "pred_x": [rng.random(n_samples) for _ in range(rows)],
        "pred_y": [rng.random(n_samples) for _ in range(rows)],
    }
    df = pd.DataFrame(data)
    month_ids = []
    cell_ids = list(range(100, 100 + n_cells))
    for m in range(600, 600 + n_months):
        month_ids.extend([m] * n_cells)
    df.index = pd.MultiIndex.from_arrays(
        [month_ids, cell_ids * n_months],
        names=["month_id", "priogrid_id"],
    )
    return df


# ============================================================
# Group A: Construction & Preprocessing
# ============================================================


class TestConstruction:

    def test_valid_multiindex_accepted(self):
        df = _make_raw_multiindex_df()
        ds = _GridDataset(df)
        assert ds.dataframe is not None

    def test_single_index_rejected(self):
        df = pd.DataFrame({"pred_x": [[1, 2], [3, 4]]}, index=[0, 1])
        with pytest.raises((ValueError, IndexError)):
            _GridDataset(df)

    def test_wrong_multiindex_levels_rejected(self):
        idx = pd.MultiIndex.from_tuples([(1, 2, 3), (4, 5, 6)], names=["a", "b", "c"])
        df = pd.DataFrame({"pred_x": [[1, 2], [3, 4]]}, index=idx)
        with pytest.raises(ValueError):
            _GridDataset(df)

    def test_fill_value_replaces_nan(self):
        """A missing (month, entity) cell is recreated by the dense grid with fill_value,
        so the canonical sample store is finite — no NaN survives construction. (A
        scalar-NaN-poisoned *row* is non-rectangular and now fails loud at construction,
        per ADR-030 §3 — the frame store cannot hold ragged cells.)"""
        df = _make_raw_multiindex_df(n_cells=3, n_months=2)
        df = df.drop(index=df.index[0])  # drop one cell; the dense grid recreates it
        ds = _GridDataset(df, fill_value=0)
        for var in ds.targets:
            assert not np.isnan(ds._sample_array(var)).any()

    def test_dense_grid_on_complete_input(self):
        """A complete grid (all time×entity combinations present) must stay dense."""
        df = make_fao_df(n_cells=4, n_months=3, n_samples=10)
        ds = FAO_PGMDataset(df)
        time_vals = ds.dataframe.index.get_level_values(0).unique()
        entity_vals = ds.dataframe.index.get_level_values(1).unique()
        expected_rows = len(time_vals) * len(entity_vals)
        assert len(ds.dataframe) == expected_rows

    def test_empty_dataframe_rejected(self):
        df = pd.DataFrame()
        with pytest.raises(ValueError):
            _GridDataset(df)

    def test_non_dataframe_rejected(self):
        with pytest.raises(ValueError):
            _GridDataset("not_a_dataframe")


# ============================================================
# Group B: Tensor Operations
# ============================================================


class TestTensorOperations:

    def test_to_tensor_shape(self):
        ds = _GridDataset(_make_raw_multiindex_df(n_cells=4, n_months=2, n_samples=10))
        tensor = ds.to_tensor()
        assert tensor.shape == (2, 4, 10, 2)  # (time, entities, samples, features)

    def test_tensor_dataframe_roundtrip(self):
        ds = _GridDataset(_make_raw_multiindex_df(n_cells=4, n_months=2, n_samples=10))
        assert ds.check_integrity()

    def test_check_integrity_passes_valid(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        assert ds.check_integrity() is True

    def test_check_integrity_on_fao_subset_does_not_raise(self):
        # C-68 regression: FAO_PGMDataset.get_subset_dataframe defaults to
        # with_metadata=True, so the integrity round-trip used to re-wrap the 9
        # geo-metadata columns as features and raise ValueError. The
        # _subset_for_integrity seam excludes them; subset check_integrity must
        # now return a bool without raising.
        ds = FAO_PGMDataset(make_fao_df(n_cells=4, n_months=2, n_samples=10))
        months = ds.dataframe.index.get_level_values(0).unique().tolist()
        result = ds.check_integrity(time_ids=months[:1])
        assert isinstance(result, bool)

    def test_get_subset_tensor_reduces_dimensions(self):
        ds = _GridDataset(_make_raw_multiindex_df(n_cells=4, n_months=3, n_samples=10))
        subset = ds.get_subset_tensor(time_ids=[600], features=["pred_x"])
        assert subset.shape[0] == 1  # 1 time step
        assert subset.shape[3] == 1  # 1 feature

    def test_get_subset_dataframe_filters_entities(self):
        ds = _GridDataset(_make_raw_multiindex_df(n_cells=4, n_months=2))
        entity_ids = ds.dataframe.index.get_level_values(1).unique()[:2].tolist()
        subset = ds.get_subset_dataframe(entity_ids=entity_ids)
        result_entities = subset.index.get_level_values(1).unique()
        assert len(result_entities) == 2
        assert set(result_entities) == set(entity_ids)

    def test_get_subset_tensor_invalid_time_raises(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        with pytest.raises(KeyError):
            ds.get_subset_tensor(time_ids=[99999])


# ============================================================
# Group C: Properties
# ============================================================


class TestProperties:

    def test_num_entities(self):
        ds = _GridDataset(_make_raw_multiindex_df(n_cells=5, n_months=2))
        assert ds.num_entities == 5

    def test_num_time_steps(self):
        ds = _GridDataset(_make_raw_multiindex_df(n_cells=4, n_months=3))
        assert ds.num_time_steps == 3

    def test_num_features(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        assert ds.num_features == 0  # all cols are pred_*, so features = non-target = 0

    def test_pred_vars_returns_pred_columns(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        assert ds.pred_vars == ["pred_x", "pred_y"]

    def test_is_prediction_mode(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        assert ds.is_prediction is True


# ============================================================
# Group D: Deep Copy Semantics
# ============================================================


class TestDeepCopy:

    def test_deepcopy_structural_independence(self):
        """Dropping a column on the copy must not affect the original."""
        ds = _GridDataset(_make_raw_multiindex_df())
        original_cols = list(ds.dataframe.columns)
        ds_copy = copy.deepcopy(ds)

        # Mutate the copy's container (post-S4d a prediction `.dataframe` has no sample
        # columns, so add a probe column rather than drop one) — the original is unaffected.
        ds_copy.dataframe["_probe"] = 0
        assert list(ds.dataframe.columns) == original_cols

    def test_deepcopy_shallow_array_sharing(self):
        """Shallow copy shares numpy array memory — this is the documented
        ~1,700x speedup design. In-place mutation of array contents propagates."""
        ds = _GridDataset(_make_raw_multiindex_df())
        ds_copy = copy.deepcopy(ds)
        assert ds_copy.dataframe is not ds.dataframe

    def test_deepcopy_is_correct_type(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        ds_copy = copy.deepcopy(ds)
        assert isinstance(ds_copy, _GridDataset)
        assert ds_copy is not ds

    def test_deepcopy_clears_tensor_cache(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        ds.to_tensor()
        ds_copy = copy.deepcopy(ds)
        assert ds_copy._split_tensor_cache == {}


# ============================================================
# Group E: HDI/MAP Delegation
# ============================================================


class TestHdiMap:

    def test_calculate_hdi_map_returns_expected_columns(self):
        ds = _GridDataset(_make_raw_multiindex_df(n_cells=4, n_months=2, n_samples=50))
        result = ds.calculate_hdi_map(alpha=0.9)
        # ADR-025 (#222/S4): MAP + three fixed HDIs (50/90/95) + severe_scenario, var-keyed
        # (the sb/ns/os rename is a boundary step); raw min/max are dropped.
        for var in ds.pred_vars:
            assert f"{var}_map" in result.columns
            assert f"{var}_severe_scenario" in result.columns
            for pct in (50, 90, 95):
                assert f"{var}_hdi{pct}_lower" in result.columns
                assert f"{var}_hdi{pct}_upper" in result.columns
            assert f"{var}_min" not in result.columns and f"{var}_max" not in result.columns

    def test_calculate_hdi_map_invalid_alpha_raises(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        with pytest.raises(ValueError, match="between 0 and 1"):
            ds.calculate_hdi_map(alpha=1.5)

    def test_calculate_hdi_map_zero_alpha_raises(self):
        ds = _GridDataset(_make_raw_multiindex_df())
        with pytest.raises(ValueError):
            ds.calculate_hdi_map(alpha=0.0)

    def test_calculate_hdi_map_non_prediction_raises(self):
        df = _make_raw_multiindex_df()
        df.columns = ["feat_a", "feat_b"]
        ds = _GridDataset(df, targets=["feat_a"])
        with pytest.raises(ValueError, match="prediction"):
            ds.calculate_hdi_map()


# ============================================================
# Group F: FAO_PGMDataset Validation
# ============================================================


class TestFAOPGMDataset:

    def test_valid_construction(self):
        df = make_fao_df()
        ds = FAO_PGMDataset(df)
        assert ds.dataframe is not None

    def test_inherits_grid_dataset(self):
        df = make_fao_df()
        ds = FAO_PGMDataset(df)
        assert isinstance(ds, _GridDataset)  # the generic base (S8: _PGDataset retired)

    def test_missing_metadata_columns_rejected(self):
        df = make_fao_df()
        df = df.drop(columns=["pg_xcoord", "country_iso_a3"])
        with pytest.raises(ValueError, match="metadata columns"):
            FAO_PGMDataset(df)

    def test_no_pred_columns_and_no_targets_rejected(self):
        df = make_fao_df()
        df = df.drop(columns=[c for c in df.columns if c.startswith("pred_")])
        with pytest.raises(ValueError, match="pred_"):
            FAO_PGMDataset(df)

    def test_priogrid_id_index_required(self):
        rng = np.random.default_rng(42)
        idx = pd.MultiIndex.from_arrays(
            [[600, 600], [1, 2]], names=["month_id", "wrong_id"]
        )
        data = {col: [0.0, 0.0] for col in FAO_PGMDataset._METADATA_COLS}
        data["pred_x"] = [rng.random(5), rng.random(5)]
        df = pd.DataFrame(data, index=idx)
        with pytest.raises(ValueError):
            FAO_PGMDataset(df)

    def test_properties_match_input(self):
        df = make_fao_df(n_cells=4, n_months=2)
        ds = FAO_PGMDataset(df)
        assert ds.num_entities == 4
        assert ds.num_time_steps == 2
