"""Characterization tests for handler-level statistical wrappers on _GridDataset."""

import numpy as np
import pandas as pd
import pytest
from views_crafdapi.data.handlers import _GridDataset, ForecastDataset

pytestmark = pytest.mark.layer2_data


class TestElementwiseSum:

    def test_sums_arrays_elementwise(self):
        arrays = pd.Series([
            np.array([1.0, 2.0, 3.0]),
            np.array([4.0, 5.0, 6.0]),
        ])
        result = ForecastDataset._elementwise_sum(None, arrays)
        np.testing.assert_array_equal(result, np.array([5.0, 7.0, 9.0]))

    def test_single_array_returned_unchanged(self):
        arrays = pd.Series([np.array([10.0, 20.0, 30.0])])
        result = ForecastDataset._elementwise_sum(None, arrays)
        np.testing.assert_array_equal(result, np.array([10.0, 20.0, 30.0]))


class TestEndToEndGoldenValues:
    """End-to-end values through `summarize` (includes the float32 sample store), against the
    same summaries computed by hand with numpy on the stored draws."""

    def test_summarize_matches_a_hand_computation(self):
        rng = np.random.default_rng(42)
        index = pd.MultiIndex.from_tuples(
            [(500, 100001), (500, 100002)],
            names=["month_id", "priogrid_id"],
        )
        draws = [rng.poisson(3.0, size=1000).astype(float), rng.exponential(2.0, size=1000)]
        ds = _GridDataset(pd.DataFrame({"pred_fatalities": draws}, index=index))
        result = ds.summarize()
        for cell, d in zip((100001, 100002), draws):
            d = d.astype(np.float32).astype(np.float64)   # as stored
            any_ = d[d > 0]
            expected = {
                "p_gt0": (d > 0).mean(), "p_gt25": (d > 25).mean(), "mean": d.mean(),
                "mean_if_any": any_.mean(), "median_if_any": np.median(any_),
                "severe_scenario": np.sort(d)[-50:].mean(), "n_draws": 1000,
            }
            for q, value in expected.items():
                assert result.loc[(500, cell), f"pred_fatalities_{q}"] == pytest.approx(value, rel=1e-9), q


class TestFloat32PrecisionIntegration:
    """Verify statistical outputs at float32 precision (C-44).

    Production data passes through _validate_prediction_structure which converts
    all prediction arrays to float32. These tests ensure golden values remain
    valid after the precision reduction.
    """

    def _make_fao_dataset(self, rng, pred_data):
        """Build a ForecastDataset with given prediction data."""
        from tests.conftest import make_fao_df
        df = make_fao_df(n_cells=2, n_months=1, n_samples=len(pred_data[0]))
        df["pred_test"] = pred_data
        df = df.drop(columns=["pred_other"])
        return ForecastDataset(df)

    def test_float32_summaries_match_float64_within_tolerance(self):
        """Summaries from the float32 store must be within 1e-3 of float64."""
        rng = np.random.default_rng(42)
        samples_64 = [rng.normal(loc=5.0, scale=1.0, size=1000) for _ in range(2)]

        index = pd.MultiIndex.from_tuples(
            [(600, 100), (600, 101)], names=["month_id", "priogrid_id"]
        )
        ds_64 = _GridDataset(pd.DataFrame(
            {"pred_test": [s.copy() for s in samples_64]}, index=index
        ))
        result_64 = ds_64.summarize()

        ds_32 = self._make_fao_dataset(rng, [s.copy() for s in samples_64])
        assert ds_32._sample_array("pred_test").dtype == np.float32  # canonical store is float32
        result_32 = ds_32.summarize()

        for col in result_64.columns:
            for idx in result_64.index:
                v64 = result_64.loc[idx, col]
                v32 = result_32.loc[idx, col]
                assert v32 == pytest.approx(v64, abs=1e-3), (
                    f"Float32 drift too large for {col} at {idx}: "
                    f"float64={v64}, float32={v32}, diff={abs(v64-v32)}"
                )

    def test_float32_near_zero_draws_keep_their_order(self):
        """Tiny positive draws stay positive and ordered at float32: every draw counts as
        "any deaths", and the severe case sits above the mean."""
        rng = np.random.default_rng(99)
        tiny = [rng.exponential(0.001, 1000) for _ in range(2)]
        ds = self._make_fao_dataset(rng, tiny)
        result = ds.summarize()
        for idx in result.index:
            assert result.loc[idx, "pred_test_p_gt0"] == 1.0
            assert result.loc[idx, "pred_test_severe_scenario"] > result.loc[idx, "pred_test_mean"] > 0

    def test_float32_large_values_no_overflow(self):
        """Large prediction values shouldn't overflow float32 in statistics."""
        rng = np.random.default_rng(77)
        large = [rng.normal(loc=1e4, scale=1e3, size=500) for _ in range(2)]
        ds = self._make_fao_dataset(rng, large)
        result = ds.summarize()
        # `dtype != object` no longer isolates numeric columns: the geo name/ISO3 columns are
        # `category` dtype (memory fix) and would slip through, so filter on numeric dtype.
        numeric_cols = [c for c in result.columns if pd.api.types.is_numeric_dtype(result[c])]
        for col in numeric_cols:
            assert np.isfinite(result[col]).all(), f"Non-finite values in {col}"
