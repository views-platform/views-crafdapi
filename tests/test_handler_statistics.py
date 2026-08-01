"""Characterization tests for handler-level statistical wrappers on _GridDataset."""

import numpy as np
import pandas as pd
import pytest
from views_crafdapi.data.handlers import _GridDataset, ForecastDataset

pytestmark = pytest.mark.layer2_data


@pytest.fixture
def simple_dataset():
    """Minimal _GridDataset with pred_* columns for testing statistical methods."""
    rng = np.random.default_rng(42)
    n_samples = 100
    index = pd.MultiIndex.from_tuples(
        [(500, 100001), (500, 100002), (501, 100001), (501, 100002)],
        names=["month_id", "priogrid_id"],
    )
    data = {
        "pred_fatalities": [
            rng.exponential(2, n_samples) for _ in range(4)
        ]
    }
    return _GridDataset(pd.DataFrame(data, index=index))


class TestAnalyzeSamples:

    def test_returns_three_element_tuple(self, simple_dataset):
        rng = np.random.default_rng(42)
        samples = rng.exponential(2, 1000)
        result = simple_dataset._analyze_samples(samples, alpha=0.9, enforce_non_negative=False)
        assert len(result) == 3
        hdi_lo, hdi_hi, map_val = result
        assert hdi_lo < hdi_hi
        assert np.isfinite(map_val)

    def test_all_nan_returns_nan_tuple(self, simple_dataset):
        result = simple_dataset._analyze_samples(
            np.array([np.nan, np.nan]), alpha=0.9, enforce_non_negative=False
        )
        assert all(np.isnan(v) for v in result)

    def test_golden_values(self, simple_dataset):
        rng = np.random.default_rng(42)
        samples = rng.normal(loc=5.0, scale=1.0, size=1000)
        lo, hi, map_val = simple_dataset._analyze_samples(
            samples, alpha=0.9, enforce_non_negative=False
        )
        # Re-baselined to the views-frames tower estimator (tower_point/hdi_tower); register C-81.
        assert lo == pytest.approx(3.3801279067993164, abs=1e-4)
        assert hi == pytest.approx(6.6678876876831055, abs=1e-4)
        assert map_val == pytest.approx(5.1310577392578125, abs=1e-4)

    def test_calculate_single_hdi_golden_value(self, simple_dataset):
        rng = np.random.default_rng(42)
        samples = rng.normal(loc=5.0, scale=1.0, size=1000)
        lo, hi = simple_dataset._calculate_single_hdi(samples, alpha=0.9)
        assert lo == pytest.approx(3.3801279067993164, abs=1e-4)
        assert hi == pytest.approx(6.6678876876831055, abs=1e-4)


class TestSimonComputeSingleMap:

    def test_enforce_non_negative_clips_to_zero(self, simple_dataset):
        rng = np.random.default_rng(42)
        samples = rng.normal(loc=-2, scale=0.5, size=1000)
        result = simple_dataset._compute_single_map(
            samples, enforce_non_negative=True
        )
        assert result >= 0.0
        assert isinstance(result, float)

    def test_golden_value(self, simple_dataset):
        rng = np.random.default_rng(42)
        samples = rng.normal(loc=5.0, scale=1.0, size=1000)
        result = simple_dataset._compute_single_map(
            samples, enforce_non_negative=False, alpha=0.9
        )
        assert result == pytest.approx(5.1310577392578125, abs=1e-4)


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
    """End-to-end golden values through calculate_hdi_map (includes float32 conversion)."""

    def test_calculate_hdi_map_golden_values(self):
        rng = np.random.default_rng(42)
        index = pd.MultiIndex.from_tuples(
            [(500, 100001), (500, 100002)],
            names=["month_id", "priogrid_id"],
        )
        data = {
            "pred_fatalities": [
                rng.normal(loc=5.0, scale=1.0, size=1000),
                rng.exponential(2.0, size=1000),
            ]
        }
        ds = _GridDataset(pd.DataFrame(data, index=index))
        result = ds.calculate_hdi_map(alpha=0.9, enforce_non_negative=False)

        # Re-baselined to the views-frames tower estimator; register C-81.
        assert result.loc[(500, 100001), "pred_fatalities_map"] == pytest.approx(
            5.1310577392578125, abs=1e-4
        )
        assert result.loc[(500, 100001), "pred_fatalities_hdi90_lower"] == pytest.approx(
            3.3801279067993164, abs=1e-4
        )
        assert result.loc[(500, 100001), "pred_fatalities_hdi90_upper"] == pytest.approx(
            6.6678876876831055, abs=1e-4
        )
        assert result.loc[(500, 100002), "pred_fatalities_map"] == pytest.approx(
            0.6897088289260864, abs=1e-4
        )

    def test_calculate_hdi_map_enforce_non_negative(self):
        rng = np.random.default_rng(42)
        index = pd.MultiIndex.from_tuples(
            [(500, 100001)],
            names=["month_id", "priogrid_id"],
        )
        data = {
            "pred_fatalities": [
                rng.normal(loc=-2.0, scale=0.5, size=1000),
            ]
        }
        ds = _GridDataset(pd.DataFrame(data, index=index))
        result = ds.calculate_hdi_map(alpha=0.9, enforce_non_negative=True)
        assert result.loc[(500, 100001), "pred_fatalities_map"] >= 0.0
        lo = result.loc[(500, 100001), "pred_fatalities_hdi90_lower"]
        hi = result.loc[(500, 100001), "pred_fatalities_hdi90_upper"]
        assert lo < hi


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

    def test_float32_hdi_map_matches_float64_within_tolerance(self):
        """HDI-MAP results from float32 pipeline must be within 1e-3 of float64."""
        rng = np.random.default_rng(42)
        samples_64 = [rng.normal(loc=5.0, scale=1.0, size=1000) for _ in range(2)]

        index = pd.MultiIndex.from_tuples(
            [(600, 100), (600, 101)], names=["month_id", "priogrid_id"]
        )
        ds_64 = _GridDataset(pd.DataFrame(
            {"pred_test": [s.copy() for s in samples_64]}, index=index
        ))
        result_64 = ds_64.calculate_hdi_map(alpha=0.9, enforce_non_negative=False)

        ds_32 = self._make_fao_dataset(rng, [s.copy() for s in samples_64])
        assert ds_32._sample_array("pred_test").dtype == np.float32  # canonical store is float32
        result_32 = ds_32.calculate_hdi_map(alpha=0.9, enforce_non_negative=False)

        for col in result_64.columns:
            for idx in result_64.index:
                v64 = result_64.loc[idx, col]
                v32 = result_32.loc[idx, col]
                assert v32 == pytest.approx(v64, abs=1e-3), (
                    f"Float32 drift too large for {col} at {idx}: "
                    f"float64={v64}, float32={v32}, diff={abs(v64-v32)}"
                )

    def test_float32_near_zero_hdi_bounds(self):
        """Near-zero distributions shouldn't produce inverted HDI bounds at float32."""
        rng = np.random.default_rng(99)
        tiny = [rng.exponential(0.001, 1000) for _ in range(2)]
        ds = self._make_fao_dataset(rng, tiny)
        result = ds.calculate_hdi_map(alpha=0.9, enforce_non_negative=True)
        for idx in result.index:
            lo = result.loc[idx, "pred_test_hdi90_lower"]
            hi = result.loc[idx, "pred_test_hdi90_upper"]
            mp = result.loc[idx, "pred_test_map"]
            assert lo <= hi, f"Inverted HDI bounds at {idx}: lo={lo}, hi={hi}"
            assert mp >= 0.0, f"Negative MAP at {idx}: {mp}"

    def test_float32_large_values_no_overflow(self):
        """Large prediction values shouldn't overflow float32 in statistics."""
        rng = np.random.default_rng(77)
        large = [rng.normal(loc=1e4, scale=1e3, size=500) for _ in range(2)]
        ds = self._make_fao_dataset(rng, large)
        result = ds.calculate_hdi_map(alpha=0.9, enforce_non_negative=False)
        # `dtype != object` no longer isolates numeric columns: the geo name/ISO3 columns are
        # `category` dtype (memory fix) and would slip through, so filter on numeric dtype.
        numeric_cols = [c for c in result.columns if pd.api.types.is_numeric_dtype(result[c])]
        for col in numeric_cols:
            assert np.isfinite(result[col]).all(), f"Non-finite values in {col}"
