"""Phase 2 (#89): the extracted tower estimator module + serving-path drop of the legacy analyzer."""

import numpy as np
import pytest

from views_crafdapi.forecast.summarize.estimator import tower_collapse

pytestmark = pytest.mark.layer2_data


def test_tower_collapse_shapes_and_ordering():
    rng = np.random.default_rng(0)
    values = rng.random((5, 64))
    lower, upper, mp = tower_collapse(values, mass=0.9)
    assert lower.shape == upper.shape == mp.shape == (5,)
    # HDI ordering contract: lower <= map <= upper.
    assert np.all(lower <= mp + 1e-6)
    assert np.all(mp <= upper + 1e-6)


def test_tower_collapse_all_nan_row_is_nan():
    values = np.full((1, 16), np.nan)
    lower, upper, mp = tower_collapse(values, mass=0.9)
    assert np.isnan(lower).all() and np.isnan(upper).all() and np.isnan(mp).all()


def test_tower_collapse_enforce_non_negative_clips_map_only():
    values = np.full((1, 32), -5.0)  # finite, negative
    lower, upper, mp = tower_collapse(values, mass=0.9, enforce_non_negative=True)
    assert mp[0] >= 0.0  # MAP clipped


def test_tower_collapse_1d_input_is_single_row():
    lower, upper, mp = tower_collapse(np.arange(50.0), mass=0.9)
    assert lower.shape == (1,)


def test_tower_collapse_preserves_exact_zero_on_zero_dominated_cells():
    """A cell whose draws are overwhelmingly zero must collapse to an exact-zero MAP and
    an HDI pinned at the zero floor — the tower must never invent nonzero fatalities where
    the posterior mode is zero (the dominant case in real conflict forecasts).

    Ported (PDA-free, synthetic, CI-running) from the retired real-data parity oracle
    `test_views_frames_parity.py::test_tower_point_matches_faoapi_on_zero_dominated_bulk`;
    the real-data + toward-zero-collapse half of that oracle is tracked as register C-168
    (local-only `_real` golden + ADR-019 version pin).
    """
    rng = np.random.default_rng(7)
    values = np.zeros((3, 1000), dtype=np.float64)  # ~95% exact zeros per row
    for i in range(3):
        idx = rng.choice(1000, size=int(rng.integers(30, 60)), replace=False)
        values[i, idx] = rng.integers(1, 20, size=idx.size).astype(float)  # a few active draws
    lower, upper, mp = tower_collapse(values, mass=0.9)
    assert np.all(mp == 0.0), f"tower MAP must be exactly 0 on zero-dominated cells, got {mp}"
    assert np.all(lower == 0.0), f"HDI lower must be pinned at the zero floor, got {lower}"
    assert np.all(mp <= upper + 1e-6)  # ordering still holds


def test_serving_path_does_not_import_legacy_analyzer():
    """#89: PosteriorDistributionAnalyzer is dropped from the serving module (handlers)."""
    import views_crafdapi.data.handlers as h

    assert not hasattr(h, "PosteriorDistributionAnalyzer")
