"""Tests for geographic aggregation pipeline in ForecastDataset (C-06 partial, C-27 partial)."""

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset

pytestmark = pytest.mark.layer2_data


# ============================================================
# _get_pg_cells
# ============================================================


class TestGetPgCells:

    def test_country_level(self, fao_dataset):
        cells = fao_dataset._get_pg_cells("country", 10)
        assert sorted(cells) == [100, 101]

    def test_country_level_other(self, fao_dataset):
        cells = fao_dataset._get_pg_cells("country", 20)
        assert sorted(cells) == [102, 103]

    def test_gaul1_level(self, fao_dataset):
        cells = fao_dataset._get_pg_cells("gaul1", 2)
        assert sorted(cells) == [102, 103]

    @pytest.mark.parametrize("level", ["gaul0", "gaul2"])
    def test_levels_outside_the_specification_are_refused(self, fao_dataset, level):
        """Every public way in names the allowed levels, never a bare KeyError."""
        with pytest.raises(ValueError, match="Level must be one of"):
            fao_dataset._get_pg_cells(level, 10)
        with pytest.raises(ValueError, match="Level must be one of"):
            fao_dataset.summarize(level=level, aggregate=True)
        with pytest.raises(ValueError, match="Level must be one of"):
            fao_dataset.summarize(level=level)
        with pytest.raises(ValueError, match="Level must be one of"):
            fao_dataset.get_subset_dataframe(level=level, aggregate=True)

    def test_invalid_level_raises(self, fao_dataset):
        with pytest.raises(ValueError, match="Level must be one of"):
            fao_dataset._get_pg_cells("invalid_level", "AAA")

    def test_unknown_code_returns_empty(self, fao_dataset):
        cells = fao_dataset._get_pg_cells("country", 99)
        assert cells == []


# ============================================================
# _elementwise_sum
# ============================================================


class TestElementwiseSum:

    def test_two_arrays(self, fao_dataset):
        a1 = np.array([1.0, 2.0, 3.0])
        a2 = np.array([4.0, 5.0, 6.0])
        series = pd.Series([a1, a2])
        result = fao_dataset._elementwise_sum(series)
        np.testing.assert_array_equal(result, np.array([5.0, 7.0, 9.0]))

    def test_single_array(self, fao_dataset):
        a1 = np.array([10.0, 20.0, 30.0])
        series = pd.Series([a1])
        result = fao_dataset._elementwise_sum(series)
        np.testing.assert_array_equal(result, a1)

    def test_three_arrays(self, fao_dataset):
        arrays = [np.array([1.0, 1.0]), np.array([2.0, 2.0]), np.array([3.0, 3.0])]
        series = pd.Series(arrays)
        result = fao_dataset._elementwise_sum(series)
        np.testing.assert_array_equal(result, np.array([6.0, 6.0]))

    def test_zero_arrays(self, fao_dataset):
        arrays = [np.zeros(5), np.zeros(5)]
        series = pd.Series(arrays)
        result = fao_dataset._elementwise_sum(series)
        np.testing.assert_array_equal(result, np.zeros(5))


# ============================================================
# _aggregate_distributions
# ============================================================


class TestAggregateDistributions:

    def test_country_aggregation_produces_correct_shape(self, fao_dataset):
        df = fao_dataset.get_subset_dataframe(with_metadata=False).join(fao_dataset.geo_metadata, how="left")
        result = fao_dataset._aggregate_distributions(df, "country")
        # 2 countries × 2 months = 4 rows
        assert result.shape[0] == 4

    def test_preserves_time_dimension(self, fao_dataset):
        df = fao_dataset.get_subset_dataframe(with_metadata=False).join(fao_dataset.geo_metadata, how="left")
        result = fao_dataset._aggregate_distributions(df, "country")
        time_ids = result.index.get_level_values("month_id").unique()
        assert sorted(time_ids) == [600, 601]

    def test_sums_correctly(self, fao_dataset):
        df = fao_dataset.get_subset_dataframe(with_metadata=False).join(fao_dataset.geo_metadata, how="left")
        result = fao_dataset._aggregate_distributions(df, "country")

        # Independent oracle: source the constituent cells from the canonical frame
        # (get_subset_dataframe, frame-sourced) and sum them by hand.
        cells = fao_dataset.get_subset_dataframe(with_metadata=False)
        cell_100_m600 = cells.loc[(600, 100), "pred_test"]
        cell_101_m600 = cells.loc[(600, 101), "pred_test"]
        expected_sum = cell_100_m600 + cell_101_m600

        agg_value = result.loc[(600, 10), "pred_test"]
        np.testing.assert_array_almost_equal(agg_value, expected_sum)

    def test_empty_dataframe(self, fao_dataset):
        empty_df = fao_dataset.dataframe.iloc[:0].join(fao_dataset.geo_metadata.iloc[:0], how="left")
        result = fao_dataset._aggregate_distributions(empty_df, "country")
        assert result.empty

    def test_gaul1_aggregation_shape(self, fao_dataset):
        df = fao_dataset.get_subset_dataframe(with_metadata=False).join(fao_dataset.geo_metadata, how="left")
        result = fao_dataset._aggregate_distributions(df, "gaul1")
        assert result.shape[0] == 4  # 2 gaul1 regions (1, 2) × 2 months

    def test_gaul1_preserves_metadata_hierarchy(self, fao_dataset):
        df = fao_dataset.get_subset_dataframe(with_metadata=False).join(fao_dataset.geo_metadata, how="left")
        result = fao_dataset._aggregate_distributions(df, "gaul1")
        for col in ["country_iso_a3", "admin1_gaul1_name", "admin1_gaul0_name"]:
            assert col in result.columns, f"gaul1 aggregation should preserve {col}"

    def test_aggregated_distribution_preserves_sample_count(self, fao_dataset):
        df = fao_dataset.get_subset_dataframe(with_metadata=False).join(fao_dataset.geo_metadata, how="left")
        result = fao_dataset._aggregate_distributions(df, "country")
        original_n_samples = fao_dataset.sample_size
        agg_samples = result.iloc[0]["pred_test"]
        assert len(agg_samples) == original_n_samples


# ============================================================
# get_subset_dataframe
# ============================================================


class TestGetSubsetDataframe:

    def test_filters_time(self, fao_dataset):
        result = fao_dataset.get_subset_dataframe(time_ids=[600])
        assert all(idx[0] == 600 for idx in result.index)

    def test_with_metadata(self, fao_dataset):
        result = fao_dataset.get_subset_dataframe(with_metadata=True)
        assert "country_iso_a3" in result.columns

    def test_without_metadata(self, fao_dataset):
        result = fao_dataset.get_subset_dataframe(with_metadata=False)
        assert "country_iso_a3" not in result.columns

    def test_aggregate_country(self, fao_dataset):
        result = fao_dataset.get_subset_dataframe(
            level="country", aggregate=True
        )
        # 2 countries × 2 months = 4 rows
        assert result.shape[0] == 4

    def test_entity_ids_country(self, fao_dataset):
        result = fao_dataset.get_subset_dataframe(
            level="country", entity_ids=[10], with_metadata=False
        )
        # Only cells in country 10 (GAUL; cells 100, 101) × 2 months = 4 rows
        assert result.shape[0] == 4
        cell_ids = result.index.get_level_values("priogrid_id").unique()
        assert sorted(cell_ids) == [100, 101]

    def test_aggregate_without_level_raises(self, fao_dataset):
        with pytest.raises(ValueError, match="Must specify 'level'"):
            fao_dataset.get_subset_dataframe(aggregate=True)

    def test_features_filter(self, fao_dataset):
        result = fao_dataset.get_subset_dataframe(features=["pred_test"], with_metadata=False)
        assert "pred_test" in result.columns
        assert "pred_other" not in result.columns

    def test_entity_ids_gaul1(self, fao_dataset):
        result = fao_dataset.get_subset_dataframe(
            level="gaul1", entity_ids=[1], with_metadata=False
        )
        cell_ids = result.index.get_level_values("priogrid_id").unique()
        assert sorted(cell_ids) == [100, 101]

    def test_aggregate_gaul1(self, fao_dataset):
        result = fao_dataset.get_subset_dataframe(
            level="gaul1", aggregate=True
        )
        assert result.shape[0] == 4  # 2 gaul1 regions × 2 months


# ============================================================
# calculate_hdi_map
# ============================================================


class TestSummarize:

    def test_cell_summaries(self, fao_dataset):
        result = fao_dataset.summarize(with_metadata=False)
        assert result.shape[0] == 8  # 4 cells × 2 months
        for var in fao_dataset.targets:
            for q in ("p_gt0", "mean", "median_if_any", "n_draws"):
                assert f"{var}_{q}" in result.columns

    def test_aggregated_summaries(self, fao_dataset):
        result = fao_dataset.summarize(level="country", aggregate=True, with_metadata=False)
        assert result.shape[0] == 4  # 2 countries × 2 months
        assert any("_severe_scenario" in col for col in result.columns)
        assert not any(c.endswith(("_map", "_min", "_max", "_lower", "_upper")) for c in result.columns)

    def test_aggregated_summaries_gaul1(self, fao_dataset):
        result = fao_dataset.summarize(level="gaul1", aggregate=True, with_metadata=False)
        assert result.shape[0] == 4  # 2 gaul1 regions × 2 months
        assert any("_mean_if_any" in col for col in result.columns)

    def test_the_summary_of_the_sum_is_not_the_sum_of_the_summaries(self, fao_dataset):
        """CIC Section 10: the draws are summed first, then summarised. The mean is the one
        summary that adds up (the data dictionary says partners may add it); the median does
        not."""
        cells = fao_dataset.summarize(with_metadata=False).join(
            fao_dataset.geo_metadata[["admin1_gaul0_code"]], how="left"
        )
        naive = cells.groupby(
            [cells.index.get_level_values("month_id"), "admin1_gaul0_code"]
        ).sum(numeric_only=True)
        summed = fao_dataset.summarize(level="country", aggregate=True, with_metadata=False)
        assert len(summed) == len(naive)
        var = fao_dataset.targets[0]
        np.testing.assert_allclose(summed[f"{var}_mean"].values, naive[f"{var}_mean"].values)
        assert not np.allclose(summed[f"{var}_median_if_any"].values, naive[f"{var}_median_if_any"].values)

    def test_aggregated_chances_fall_as_the_threshold_rises(self, fao_dataset):
        result = fao_dataset.summarize(level="country", aggregate=True, with_metadata=False)
        for var in fao_dataset.targets:
            chances = [result[f"{var}_p_gt{c}"] for c in (0, 25, 100, 500, 1000)]
            for higher, lower in zip(chances, chances[1:]):
                assert (lower <= higher).all()

    def test_aggregate_without_level_raises(self, fao_dataset):
        with pytest.raises(ValueError, match="Must specify 'level'"):
            fao_dataset.summarize(aggregate=True)


# ============================================================
# Historical/feature path stays float64 (out of the frame migration's scope)
# ============================================================


def _feature_df(n_cells=4, n_months=2, seed=7):
    """A historical/scalar source: the 9 geo columns + a float64 scalar target, no pred_*."""
    base = make_fao_df(n_cells=n_cells, n_months=n_months, n_samples=1, seed=seed)
    feat = base.drop(columns=[c for c in base.columns if c.startswith("pred_")])
    rng = np.random.default_rng(seed)
    feat["lr_ged_sb"] = rng.gamma(2.0, 30000.0, size=len(feat)).astype(np.float64)
    return feat


class TestFeatureAggregationStaysFloat64:
    """Regression guard (S4b scope creep, caught by geo_meta.ipynb): the historical/scalar
    aggregation must stay **float64** and byte-identical to the legacy `elementwise_sum` —
    the frame-native float32 joint-sum is for PREDICTION samples only (ADR-030 §1). A
    float32 stack here would silently re-baseline historical aggregates."""

    def test_feature_aggregate_is_float64_byte_identical(self):
        ds = ForecastDataset(_feature_df(), targets=["lr_ged_sb"])
        assert not ds.is_prediction
        agg = ds.get_subset_dataframe(aggregate=True, level="country")

        raw = ds.get_subset_dataframe(with_metadata=False).join(ds.geo_metadata, how="left")
        for (t, unit), row in agg.iterrows():
            cell = np.asarray(row["lr_ged_sb"])
            assert cell.dtype == np.float64, "historical aggregate must stay float64"
            members = raw[(raw.index.get_level_values(ds._time_id) == t)
                          & (raw["admin1_gaul0_code"] == unit)]["lr_ged_sb"]
            expected = np.stack([np.asarray(v, np.float64) for v in members]).sum(axis=0)
            assert np.array_equal(cell, expected), f"{unit}@{t}: feature sum re-baselined"


# ============================================================
# C-70 — frame-native joint-sum draw alignment (is_prediction serving path)
# ============================================================


def _known_prediction_df():
    """A 2-cell, 1-month prediction frame with KNOWN per-draw sample values.

    Both priogrid cells (100, 101) map to GAUL country 10 in ``make_fao_df``, so a
    country aggregate must equal cell-100 + cell-101 **draw by draw**. ``pred_test``
    uses an ascending signature per cell so a draw-misalignment (a reversed/rolled
    draw axis on one cell) cannot coincidentally produce the aligned sum.
    """
    df = make_fao_df(n_cells=2, n_months=1, n_samples=5)
    # Rows are ordered (600, 100), (600, 101).
    df["pred_test"] = [
        np.array([10.0, 11.0, 12.0, 13.0, 14.0]),
        np.array([20.0, 21.0, 22.0, 23.0, 24.0]),
    ]
    df["pred_other"] = [
        np.array([1.0, 2.0, 3.0, 4.0, 5.0]),
        np.array([0.0, 1.0, 2.0, 3.0, 4.0]),
    ]
    return df


class TestC70FrameNativeAlignment:
    """C-70 on the **live serving path**. ``test_falsify_88_path.py`` pins the legacy
    ``_elementwise_sum`` primitive; this pins the ``is_prediction`` frame-native path
    end-to-end through ``_aggregate_distributions`` -> ``_frame_native_joint_sum`` ->
    ``aggregate_via_leaf`` — the path that actually serves FAO. A draw-misalignment here
    would be silent, signal-free forecast corruption (right magnitudes, wrong draw pairing)
    that the synthetic golden cannot catch, since it reads through the same store."""

    def test_country_aggregate_sums_aligned_draws(self):
        ds = ForecastDataset(_known_prediction_df())
        assert ds.is_prediction
        sub = ds.get_subset_dataframe(with_metadata=False).join(ds.geo_metadata, how="left")
        agg = ds._aggregate_distributions(sub, "country")

        # draw k of country 10 == draw k of cell-100 + draw k of cell-101, for every k
        np.testing.assert_array_equal(
            np.asarray(agg.loc[(600, 10), "pred_test"]),
            np.array([30.0, 32.0, 34.0, 36.0, 38.0]),
        )
        np.testing.assert_array_equal(
            np.asarray(agg.loc[(600, 10), "pred_other"]),
            np.array([1.0, 3.0, 5.0, 7.0, 9.0]),
        )

    def test_aggregate_is_independent_of_input_row_order(self):
        """Reordering the input rows (values travelling with them) must not change the
        aggregate — the ``sort_index`` re-binding (the C-155 invariant) makes draw pairing
        positional-but-stable. A bug that stacked samples in a different order than the
        index would diverge here."""
        def country_aaa(df):
            ds = ForecastDataset(df)
            sub = ds.get_subset_dataframe(with_metadata=False).join(ds.geo_metadata, how="left")
            agg = ds._aggregate_distributions(sub, "country")
            return np.asarray(agg.loc[(600, 10), "pred_test"])

        base = _known_prediction_df()
        reversed_rows = base.iloc[::-1]
        np.testing.assert_array_equal(country_aaa(base), country_aaa(reversed_rows))
        np.testing.assert_array_equal(country_aaa(base), np.array([30.0, 32.0, 34.0, 36.0, 38.0]))


class TestC146MissingGeographyCode:
    """C-146 corner found via the real-artifact golden (2026-06-28): ~1.1% of real cells
    carry a **missing** GAUL code (NaN). The frame-native joint-sum factorized NaN -> -1
    and raised ``KeyError: -1`` on the live serving path — a crash the synthetic golden
    never exercised. Legacy pandas ``groupby`` silently drops NaN group keys; the
    frame-native path must match (drop the cell), not crash."""

    def _df_with_one_null_gaul1(self):
        df = make_fao_df(n_cells=4, n_months=1, n_samples=6)
        # one admin-1 unit per cell (100,101,102,103 -> 1,2,3,4); null cell 101's code.
        df["admin1_gaul1_code"] = [1.0, np.nan, 3.0, 4.0]
        df["admin1_gaul1_name"] = ["Region1", None, "Region3", "Region4"]
        # give known per-draw values so the surviving aggregates are checkable
        df["pred_test"] = [np.full(6, 1.0), np.full(6, 9.0), np.full(6, 2.0), np.full(6, 3.0)]
        return df

    def test_missing_level_code_does_not_crash_and_drops_the_cell(self):
        ds = ForecastDataset(self._df_with_one_null_gaul1())
        sub = ds.get_subset_dataframe(with_metadata=False).join(ds.geo_metadata, how="left")
        agg = ds._aggregate_distributions(sub, "gaul1")  # must not raise KeyError: -1

        units = {int(u) for u in agg.index.get_level_values("admin1_gaul1_code")}
        assert units == {1, 3, 4}, "the NaN-coded cell must be dropped (legacy parity)"
        # surviving single-cell units carry their cell's samples unchanged
        np.testing.assert_array_equal(np.asarray(agg.loc[(600, 1), "pred_test"]), np.full(6, 1.0))
        np.testing.assert_array_equal(np.asarray(agg.loc[(600, 3), "pred_test"]), np.full(6, 2.0))

    def test_frame_native_matches_legacy_groupby_under_missing_codes(self):
        """The frame-native (is_prediction) drop must be byte-identical to the legacy
        float64 groupby(dropna) on the same NaN-bearing geography."""
        df = self._df_with_one_null_gaul1()
        pred = ForecastDataset(df)
        sub = pred.get_subset_dataframe(with_metadata=False).join(pred.geo_metadata, how="left")
        frame_native = pred._aggregate_distributions(sub, "gaul1")

        # legacy reference: the same cells/codes through the float64 elementwise_sum groupby
        legacy = (
            sub.reset_index()
            .dropna(subset=["admin1_gaul1_code"])
            .groupby(["month_id", "admin1_gaul1_code"])["pred_test"]
            .apply(lambda s: np.stack(list(s)).sum(axis=0))
        )
        for (t, code), expected in legacy.items():
            got = np.asarray(frame_native.loc[(t, int(code)), "pred_test"])
            np.testing.assert_array_equal(got, np.asarray(expected, np.float32))


class TestSummariesNeedDraws:
    """Summaries are computed from draws. The history holds one recorded value per cell, so
    asking it for summaries is refused rather than answered with summaries of nothing."""

    def test_no_draws_means_zero_draws_and_empty_summaries_at_every_level(self, fao_dataset):
        """Asking for no draws at all gives rows that count 0 draws and summarise nothing, the
        same at cell and region level."""
        for kwargs in ({}, {"level": "gaul1", "aggregate": True}):
            out = fao_dataset.summarize(sample_idx=[], with_metadata=False, **kwargs)
            var = fao_dataset.targets[0]
            assert (out[f"{var}_n_draws"] == 0).all(), kwargs
            assert out[f"{var}_mean"].isna().all(), kwargs

    def test_a_history_is_refused(self):
        base = make_fao_df(n_cells=4, n_months=1, n_samples=1)
        feat = base.drop(columns=[c for c in base.columns if c.startswith("pred_")])
        feat["lr_ged_sb"] = 1.0
        history = ForecastDataset(feat, targets=["lr_ged_sb"])
        for kwargs in ({}, {"level": "gaul1", "aggregate": True}):
            with pytest.raises(ValueError, match="computed from draws"):
                history.summarize(**kwargs)


class TestAggregatePathValidatesItsInputs:
    """Register C-259 — `get_subset_dataframe` validated as a side effect of building columns.

    The S7 aggregate path reads `_sample_array` directly and inherits none of that, so each
    check below is re-asserted on the path that no longer goes through it. The messages are
    required to match the cell path's: one endpoint answering two different ways for the same
    bad input is the failure mode these pin.
    """

    @staticmethod
    def _ds():
        return ForecastDataset(
            make_fao_df(n_cells=4, n_months=2, n_samples=16, seed=3,
                        targets=("pred_lr_ged_sb",))
        )

    def test_negative_sample_index_raises_instead_of_wrapping(self):
        """Wrapping served draw S-1 alone as a full posterior — a zero-width interval."""
        with pytest.raises(ValueError, match="Sample indices must be between 0 and 15"):
            self._ds().summarize(aggregate=True, level="gaul1", sample_idx=-1)

    def test_out_of_range_sample_index_raises_the_same_error_as_the_cell_path(self):
        with pytest.raises(ValueError, match="Sample indices must be between 0 and 15"):
            self._ds().summarize(aggregate=True, level="gaul1", sample_idx=999)

    def test_unknown_feature_raises_value_error_not_a_bare_key_error(self):
        """A bare KeyError reaches the client as HTTP 500 with the body `"'typo'"`."""
        with pytest.raises(ValueError, match="Invalid features specified"):
            self._ds().summarize(aggregate=True, level="gaul1", features=["pred_nope"])

    def test_empty_feature_list_selects_nothing_rather_than_everything(self):
        """`?features=` parses to `[]`; a falsy-empty check turned that into every target."""
        out = self._ds().summarize(aggregate=True, level="gaul1", features=[])
        assert out.shape == (0, 0), f"features=[] aggregated {out.shape[1]} columns"

    def test_unknown_time_id_raises_like_the_cell_path(self):
        with pytest.raises(KeyError, match="Invalid time IDs"):
            self._ds().summarize(aggregate=True, level="gaul1", time_ids=[9999])

    def test_country_level_index_is_not_a_categorical_carrying_every_category(self):
        """A CategoricalIndex re-exports the fan-out `observed=True` exists to prevent."""
        out = self._ds().summarize(aggregate=True, level="country", with_metadata=False)
        level_values = out.index.get_level_values("admin1_gaul0_code")
        assert not isinstance(level_values.dtype, pd.CategoricalDtype), (
            "the country index level is categorical: a downstream groupby on it emits a row "
            "for every unobserved category, and `.loc` on an absent label raises rather than "
            "selecting empty."
        )
