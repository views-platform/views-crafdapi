"""Exceedance probability P(Y > c) in the served collapse (ADR-034 §3, 1a-i).

The reduction gains a new statistic without touching the served columns yet (that is 1a-ii):
`collapse(..., thresholds=...)` fills `CollapseResult.exceedance = {c: (N,)}` via views-frames'
`exceedance` summarizer. These tests pin the arithmetic (strict `>`, empirical survival
fraction), the NaN discipline (all-NaN rows stay NaN, not a spurious 0), the shape, and the
zero-cost default (no thresholds → unchanged behaviour). The name contract is checked too.
"""

import numpy as np
import pytest

from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.summarize.estimator import collapse


class TestExceedanceArithmetic:
    def test_strict_greater_than_survival_fraction(self):
        # draws {10,20,30,40}: P(Y>25) = |{30,40}|/4 = 0.5; P(Y>5)=1.0; P(Y>40)=0.0 (strict).
        cr = collapse([[10.0, 20.0, 30.0, 40.0]], thresholds=(5, 25, 40))
        assert cr.exceedance[5][0] == pytest.approx(1.0)
        assert cr.exceedance[25][0] == pytest.approx(0.5)
        assert cr.exceedance[40][0] == pytest.approx(0.0)  # strict > : 40 is NOT > 40

    def test_onset_probability_p_gt_zero(self):
        # the views-frames flagship: P(Y>0) = fraction of non-zero draws.
        cr = collapse([[0.0, 0.0, 0.0, 7.0]], thresholds=(0,))
        assert cr.exceedance[0][0] == pytest.approx(0.25)

    def test_multi_row_multi_threshold_shape_and_alignment(self):
        rows = [[10.0, 20.0, 30.0, 40.0], [0.0, 0.0, 0.0, 0.0]]
        cr = collapse(rows, thresholds=(25, 100))
        for c in (25, 100):
            assert cr.exceedance[c].shape == (2,)
        assert cr.exceedance[25][0] == pytest.approx(0.5)
        assert cr.exceedance[25][1] == pytest.approx(0.0)  # all-zero row never exceeds 25
        assert cr.exceedance[100][0] == pytest.approx(0.0)


class TestExceedanceNaNDiscipline:
    def test_all_nan_row_is_nan_not_zero(self):
        # an all-NaN row must yield NaN P (not a spurious 0 that reads as "certainly below c").
        rows = [[np.nan, np.nan, np.nan], [1.0, 50.0, 200.0]]
        cr = collapse(rows, thresholds=(25,))
        assert np.isnan(cr.exceedance[25][0])
        assert cr.exceedance[25][1] == pytest.approx(2 / 3)  # {50,200} > 25


class TestExceedanceDefaultIsZeroCost:
    def test_no_thresholds_leaves_exceedance_empty(self):
        cr = collapse([[1.0, 2.0, 3.0]])
        assert cr.exceedance == {}

    def test_hdi_map_unchanged_when_thresholds_added(self):
        rows = [[1.0, 5.0, 9.0, 40.0]]
        base = collapse(rows, masses=schema.MASSES)
        withx = collapse(rows, masses=schema.MASSES, thresholds=schema.EXCEEDANCE_THRESHOLDS)
        assert withx.map[0] == pytest.approx(base.map[0], nan_ok=True)
        for m in schema.MASSES:
            np.testing.assert_allclose(withx.hdi[m], base.hdi[m], equal_nan=True)


class TestExceedanceReachesServedColumns:
    """End-to-end (1a-ii): the served grid path emits `{var}_p_gt{c}` with correct values."""

    def test_served_pg_frame_carries_correct_exceedance(self):
        import pandas as pd
        from views_crafdapi.data.handlers import _GridDataset

        # cell A draws {10,20,30,40}: P(>25)=.5, P(>100)=0; cell B all 200: P(>25)=P(>100)=1, P(>1000)=0.
        index = pd.MultiIndex.from_tuples([(500, 1), (500, 2)], names=["month_id", "priogrid_id"])
        data = {"pred_fatalities": [
            np.array([10.0, 20.0, 30.0, 40.0]),
            np.array([200.0, 200.0, 200.0, 200.0]),
        ]}
        served = _GridDataset(pd.DataFrame(data, index=index)).calculate_hdi_map(alpha=0.9)
        for c in schema.EXCEEDANCE_THRESHOLDS:
            assert schema.exceed_col("pred_fatalities", c) in served.columns
        col = lambda c: f"pred_fatalities_p_gt{c}"  # noqa: E731
        assert served.loc[(500, 1), col(25)] == pytest.approx(0.5)
        assert served.loc[(500, 1), col(100)] == pytest.approx(0.0)
        assert served.loc[(500, 2), col(25)] == pytest.approx(1.0)
        assert served.loc[(500, 2), col(100)] == pytest.approx(1.0)
        assert served.loc[(500, 2), col(1000)] == pytest.approx(0.0)


class TestExceedColNameContract:
    def test_name_scheme(self):
        assert schema.exceed_col("sb", 25) == "sb_p_gt25"
        assert schema.exceed_col("os", 1000) == "os_p_gt1000"

    def test_unknown_threshold_fails_loud(self):
        with pytest.raises(ValueError, match="unknown exceedance threshold"):
            schema.exceed_col("sb", 42)
