"""S2 (#222): the `expected_shortfall` reducer (ADR-025 `severe_scenario`)."""

import numpy as np
import pytest

from views_crafdapi.forecast.summarize.severe import expected_shortfall

pytestmark = pytest.mark.layer2_data


def test_worst_five_percent_mean_is_exact_on_a_known_row():
    # 0..99; worst 5% = the 5 largest {95,96,97,98,99}; mean = 97.
    row = np.arange(100.0)
    assert expected_shortfall(row, tail=0.05)[0] == pytest.approx(97.0)


def test_1d_input_is_a_single_row():
    out = expected_shortfall(np.arange(50.0), tail=0.1)
    assert out.shape == (1,)


def test_all_nan_row_is_nan():
    assert np.isnan(expected_shortfall(np.full((1, 16), np.nan)))[0]


def test_bounded_between_mean_and_max():
    rng = np.random.default_rng(3)
    mat = rng.random((6, 200)) * 100
    es = expected_shortfall(mat, tail=0.05)
    assert np.all(es >= mat.mean(axis=1) - 1e-9)  # tail mean >= overall mean
    assert np.all(es <= mat.max(axis=1) + 1e-9)  # ... and <= the max


def test_tail_one_is_the_full_mean():
    row = np.arange(10.0)
    assert expected_shortfall(row, tail=1.0)[0] == pytest.approx(row.mean())


def test_invalid_tail_raises():
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            expected_shortfall(np.arange(10.0), tail=bad)


def test_nonfinite_in_valid_row_is_zeroed_not_propagated():
    row = np.array([[np.nan, np.inf, 1.0, 2.0, 3.0, 4.0]])
    out = expected_shortfall(row, tail=0.5)
    assert np.isfinite(out).all()
