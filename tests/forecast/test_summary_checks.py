"""The check every served table passes before it leaves (#204, faoapi's review 2026-11-17)."""

import numpy as np
import pandas as pd
import pytest

from views_crafdapi.forecast.serialize.summary_checks import check_summaries

pytestmark = pytest.mark.layer2_data


def _table(**overrides):
    """Two consistent rows; `overrides` replaces whole columns."""
    columns = {
        "sb_p_gt0": [0.5, 0.0], "sb_p_gt25": [0.25, 0.0], "sb_p_gt100": [0.1, 0.0],
        "sb_p_gt500": [0.0, 0.0], "sb_p_gt1000": [0.0, 0.0],
        "sb_mean": [20.0, 0.0], "sb_mean_if_any": [40.0, np.nan], "n_draws": [100.0, 100.0],
    }
    columns.update(overrides)
    return pd.DataFrame(columns)


def test_a_consistent_table_passes():
    check_summaries(_table(), "sb")


def test_a_chance_rising_with_the_threshold_is_refused():
    with pytest.raises(ValueError, match="sb_p_gt100 is above sb_p_gt25"):
        check_summaries(_table(sb_p_gt100=[0.3, 0.0]), "sb")


def test_a_mean_that_is_not_chance_times_mean_if_any_is_refused():
    with pytest.raises(ValueError, match="sb_mean is not sb_p_gt0 x sb_mean_if_any"):
        check_summaries(_table(sb_mean=[21.0, 0.0]), "sb")


def test_empty_values_are_skipped_not_read_as_zero():
    """A past month has no summaries at all; an empty chance must not count as 0 < 0.25."""
    check_summaries(_table(sb_p_gt0=[np.nan, 0.0], sb_mean=[np.nan, 0.0]), "sb")


def test_n_draws_takes_no_part():
    """A count of draws says nothing about the summaries; any value passes."""
    check_summaries(_table(n_draws=[0.0, np.nan]), "sb")


def test_a_table_cut_to_some_columns_is_checked_on_those():
    table = _table()[["sb_p_gt0", "sb_p_gt100"]].assign(sb_p_gt100=[0.9, 0.0])
    with pytest.raises(ValueError, match="sb_p_gt100 is above sb_p_gt0"):
        check_summaries(table, "sb")


def test_a_chance_rising_by_a_millionth_is_refused():
    """The tolerance is for float rounding, not for small rises."""
    with pytest.raises(ValueError, match="sb_p_gt25 is above sb_p_gt0"):
        check_summaries(_table(sb_p_gt25=[0.500001, 0.0]), "sb")


def test_a_missing_chance_does_not_break_the_mean_relation():
    """mean 20 with mean if any 40 and an empty chance of any: nothing to check, not 20 != 0."""
    check_summaries(_table(sb_p_gt0=[np.nan, 0.0]), "sb")


def test_without_all_three_columns_the_mean_relation_is_not_checked():
    """A table cut to `sb_mean` and `sb_p_gt0` cannot be checked for the relation, and is
    not refused for it."""
    check_summaries(_table(sb_mean=[99.0, 0.0])[["sb_p_gt0", "sb_mean"]], "sb")
