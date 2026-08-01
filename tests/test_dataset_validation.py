"""Tests for FAO_PGMDataset pre-construction schema validation (C-13)."""

import copy

import numpy as np
import pandas as pd
import pytest

from views_faoapi.data.handlers import FAO_PGMDataset

pytestmark = pytest.mark.layer2_data

_METADATA_COLS = FAO_PGMDataset._METADATA_COLS


def _make_df(
    n_rows=4,
    pred_cols=("pred_test",),
    index_names=("month_id", "priogrid_id"),
    include_metadata=True,
    use_multiindex=True,
):
    data = {}
    if include_metadata:
        for col in _METADATA_COLS:
            data[col] = ["val"] * n_rows
    for col in pred_cols:
        data[col] = [np.array([0.1, 0.2]) for _ in range(n_rows)]
    df = pd.DataFrame(data)
    if use_multiindex:
        df.index = pd.MultiIndex.from_arrays(
            [[600, 600, 601, 601][:n_rows], [100, 101, 100, 101][:n_rows]],
            names=list(index_names),
        )
    return df


class TestFAOPGMDatasetValidation:

    def test_missing_pred_columns_raises(self):
        df = _make_df(pred_cols=())
        with pytest.raises(ValueError, match="No prediction columns"):
            FAO_PGMDataset(df)

    def test_wrong_index_names_raises(self):
        df = _make_df(index_names=("time_id", "grid_id"))
        with pytest.raises(ValueError, match="MultiIndex must be"):
            FAO_PGMDataset(df)

    def test_non_multiindex_raises(self):
        df = _make_df(use_multiindex=False)
        with pytest.raises(ValueError, match="2-level MultiIndex"):
            FAO_PGMDataset(df)

    def test_valid_dataframe_passes(self):
        df = _make_df()
        dataset = FAO_PGMDataset(df)
        assert hasattr(dataset, "dataframe")
        assert len(dataset.targets) > 0

    def test_priogrid_gid_accepted_and_renamed(self):
        df = _make_df(index_names=("month_id", "priogrid_gid"))
        dataset = FAO_PGMDataset(df)
        assert dataset.dataframe.index.names[1] == "priogrid_id"

    def test_fill_value_default_is_zero(self):
        df = _make_df(n_rows=2)
        dataset = FAO_PGMDataset(df)
        assert dataset._fill_value == 0

    def test_fill_value_nan_produces_nan_in_filled_cells(self):
        from views_faoapi.data.handlers import _GridDataset

        index = pd.MultiIndex.from_tuples(
            [(600, 100), (601, 100), (601, 101)],
            names=["month_id", "priogrid_id"],
        )
        df = pd.DataFrame({"pred_test": [1.0, 2.0, 3.0]}, index=index)
        dataset = _GridDataset(df, fill_value=float("nan"))
        filled_rows = dataset.dataframe.index.difference(df.index)
        assert len(filled_rows) > 0, "Test requires filled rows to be non-vacuous"
        for col in dataset.dataframe.columns:
            val = dataset.dataframe.loc[filled_rows[0], col]
            assert np.all(np.isnan(val))


class TestDeepCopyOptimization:

    def test_deepcopy_preserves_dataframe(self):
        df = _make_df()
        original = FAO_PGMDataset(df)
        cloned = copy.deepcopy(original)
        assert cloned.dataframe.shape == original.dataframe.shape
        assert list(cloned.dataframe.columns) == list(original.dataframe.columns)

    def test_deepcopy_preserves_levels(self):
        df = _make_df()
        original = FAO_PGMDataset(df)
        cloned = copy.deepcopy(original)
        assert cloned.levels == original.levels
        assert cloned.levels is not original.levels

    def test_deepcopy_preserves_geo_metadata(self):
        df = _make_df()
        original = FAO_PGMDataset(df)
        cloned = copy.deepcopy(original)
        assert cloned.geo_metadata.shape == original.geo_metadata.shape

    def test_deepcopy_isolates_dataframe_mutation(self):
        df = _make_df()
        original = FAO_PGMDataset(df)
        orig_shape = original.dataframe.shape
        cloned = copy.deepcopy(original)
        # Mutate the clone's container (post-S4d a prediction `.dataframe` has no sample
        # columns, so add a probe column rather than drop one) — the original is unaffected.
        cloned.dataframe["_probe"] = 0
        assert original.dataframe.shape == orig_shape
        assert "_probe" not in original.dataframe.columns

    def test_deepcopy_isolates_geo_metadata_mutation(self):
        df = _make_df()
        original = FAO_PGMDataset(df)
        orig_cols = list(original.geo_metadata.columns)
        cloned = copy.deepcopy(original)
        cloned.geo_metadata.drop(cloned.geo_metadata.columns[0], axis=1, inplace=True)
        assert list(original.geo_metadata.columns) == orig_cols
