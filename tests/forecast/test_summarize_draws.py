"""The served summaries of a row's draws (#204, data dictionary), one rule per test."""

import numpy as np
import pytest

from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.summarize.draws import summarize_draws

pytestmark = pytest.mark.layer2_data


def _one(draws):
    """The summaries of one row, as plain floats."""
    return {q: float(v[0]) for q, v in summarize_draws(np.asarray(draws, dtype=float)).items()}


def test_every_served_quantity_comes_back_in_order():
    out = summarize_draws(np.zeros((3, 40)))
    assert list(out) == list(schema.SUMMARY_QUANTITIES)
    assert all(v.shape == (3,) and v.dtype == np.float64 for v in out.values())


def test_the_chances_count_draws_strictly_above_the_threshold():
    """`p_gt25` is the share of draws above 25: a draw of exactly 25 does not count."""
    r = _one([0, 0, 25, 26, 100, 101, 500, 501, 1000, 1001])
    assert [r[f"p_gt{c}"] for c in schema.EXCEEDANCE_THRESHOLDS] == [0.8, 0.7, 0.5, 0.3, 0.1]


def test_mean_is_over_all_draws_and_the_if_any_summaries_over_draws_above_zero():
    r = _one([0, 0, 0, 0, 0, 0, 1, 3, 10, 100])
    assert r["mean"] == pytest.approx(11.4)
    assert r["mean_if_any"] == pytest.approx(28.5)
    assert r["median_if_any"] == pytest.approx(6.5)   # even count: the two middle values, 3 and 10
    assert _one([0, 2, 5, 9])["median_if_any"] == 5   # odd count: the middle value


def test_with_no_draw_above_zero_the_if_any_summaries_are_empty_not_zero():
    r = _one(np.zeros(50))
    assert r["p_gt0"] == 0 and r["mean"] == 0
    assert np.isnan(r["mean_if_any"]) and np.isnan(r["median_if_any"])


def test_severe_is_the_mean_of_the_worst_five_percent():
    draws = np.arange(40, dtype=float)                 # worst 5% of 40 = 2 draws: 38, 39
    assert _one(draws)["severe_scenario"] == pytest.approx(38.5)
    draws = np.arange(41, dtype=float)                 # ceil(0.05 x 41) = 3 draws: 38, 39, 40
    assert _one(draws)["severe_scenario"] == pytest.approx(39.0)


def test_severe_is_empty_below_twenty_draws_where_it_would_be_the_maximum():
    assert np.isnan(_one(np.arange(19.0))["severe_scenario"])
    assert _one(np.arange(20.0))["severe_scenario"] == 19.0


def test_only_finite_draws_are_used_and_counted():
    """A draw that is not a finite number is left out, never read as 0: an infinite draw
    would otherwise make every chance 1 or the mean infinite."""
    finite = [0, 0, 4, 8] + [1] * 16
    r = _one(finite + [np.nan, np.inf, -np.inf])
    expected = _one(finite)
    assert r == pytest.approx(expected, nan_ok=True)
    assert r["n_draws"] == 20


def test_a_row_without_usable_draws_is_empty_and_counts_zero_draws():
    r = _one([np.nan] * 30)
    assert r["n_draws"] == 0
    assert all(np.isnan(r[q]) for q in schema.SUMMARY_QUANTITIES if q != "n_draws")


def test_rows_are_independent():
    """Each row is summarised on its own draws, whatever the rows around it hold."""
    rows = np.array([[0.0] * 20, [5.0] * 20, [np.nan] * 20])
    out = summarize_draws(rows)
    alone = [summarize_draws(row) for row in rows]
    for i in range(3):
        for q in schema.SUMMARY_QUANTITIES:
            np.testing.assert_equal(out[q][i], alone[i][q][0])


def test_the_mean_is_the_chance_of_any_times_the_mean_if_any():
    """The relation the served tables are checked against (`summary_checks`)."""
    rng = np.random.default_rng(0)
    draws = rng.poisson(0.4, size=(200, 64)) * rng.integers(1, 500, size=(200, 64))
    out = summarize_draws(draws)
    some = out["p_gt0"] > 0
    np.testing.assert_allclose(out["mean"][some], (out["p_gt0"] * out["mean_if_any"])[some], rtol=1e-12)


def test_rows_with_no_draws_at_all_are_empty_not_an_error():
    """A request for zero draws (an empty draw axis) is a row with no usable draws."""
    out = summarize_draws(np.empty((3, 0)))
    assert out["n_draws"].tolist() == [0.0, 0.0, 0.0]
    assert all(np.isnan(out[q]).all() for q in schema.SUMMARY_QUANTITIES if q != "n_draws")


def test_the_input_is_not_changed():
    draws = np.array([[3.0, np.inf, 1.0, np.nan, 2.0]])
    before = draws.copy()
    summarize_draws(draws)
    np.testing.assert_array_equal(draws, before)


def test_the_if_any_summaries_leave_out_draws_at_or_below_zero():
    """Ingest refuses negative draws, but the definition is "draws above 0", not "all draws"."""
    r = _one([-5.0, 0.0, 10.0, 20.0])
    assert r["mean_if_any"] == 15.0 and r["median_if_any"] == 15.0
