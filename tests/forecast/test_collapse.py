"""S2 (#222): `estimator.collapse` → CollapseResult (MAP + nested HDIs + severe)."""

import numpy as np
import pytest

from views_faoapi.forecast.summarize.estimator import collapse, tower_collapse
from views_faoapi.forecast.summarize.result import CollapseResult
from views_faoapi.forecast.summarize.severe import expected_shortfall

pytestmark = pytest.mark.layer2_data


def _samples(seed=0, n=5, s=256):
    return np.random.default_rng(seed).random((n, s)).astype(np.float32) * 50


def test_shapes_and_type():
    r = collapse(_samples(), masses=(0.5, 0.9, 0.95))
    assert isinstance(r, CollapseResult)
    assert r.map.shape == r.severe.shape == (5,)
    assert set(r.masses) == {0.5, 0.9, 0.95}
    for m in (0.5, 0.9, 0.95):
        assert r.hdi[m].shape == (5, 2)


def test_hdis_are_nested_50_within_90_within_99():
    r = collapse(_samples(seed=1), masses=(0.5, 0.9, 0.95))
    assert np.all(r.lower(0.5) >= r.lower(0.9) - 1e-9)
    assert np.all(r.lower(0.9) >= r.lower(0.95) - 1e-9)
    assert np.all(r.upper(0.5) <= r.upper(0.9) + 1e-9)
    assert np.all(r.upper(0.9) <= r.upper(0.95) + 1e-9)
    # ordering: lower <= map <= upper at every mass
    for m in (0.5, 0.9, 0.95):
        assert np.all(r.lower(m) <= r.map + 1e-6)
        assert np.all(r.map <= r.upper(m) + 1e-6)


def test_map_matches_the_single_mass_tower_collapse():
    """collapse() and tower_collapse() share the tower MAP — same input, identical MAP."""
    vals = _samples(seed=2)
    _, _, mp_tuple = tower_collapse(vals, mass=0.9)
    r = collapse(vals, masses=(0.5, 0.9, 0.95))
    assert np.allclose(r.map, mp_tuple, equal_nan=True)


def test_hdi90_matches_the_single_mass_tower_collapse():
    vals = _samples(seed=2)
    lo, up, _ = tower_collapse(vals, mass=0.9)
    r = collapse(vals, masses=(0.5, 0.9, 0.95))
    assert np.allclose(r.lower(0.9), lo, equal_nan=True)
    assert np.allclose(r.upper(0.9), up, equal_nan=True)


def test_severe_matches_expected_shortfall():
    vals = _samples(seed=4)
    r = collapse(vals, tail=0.05)
    assert np.allclose(r.severe, expected_shortfall(vals, tail=0.05), equal_nan=True)


def test_all_nan_row_is_nan_everywhere():
    r = collapse(np.full((1, 32), np.nan), masses=(0.5, 0.9, 0.95))
    assert np.isnan(r.map).all() and np.isnan(r.severe).all()
    for m in (0.5, 0.9, 0.95):
        assert np.isnan(r.hdi[m]).all()


def test_zero_dominated_cell_has_zero_map_but_nonzero_severe():
    """The dominant real case: mostly-zero draws → MAP 0, yet the severe scenario captures
    the tail (a nonzero worst-case) — exactly why severe_scenario is served alongside MAP."""
    rng = np.random.default_rng(9)
    vals = np.zeros((1, 1000), dtype=np.float32)
    idx = rng.choice(1000, size=40, replace=False)
    vals[0, idx] = rng.integers(5, 30, size=40).astype(np.float32)
    r = collapse(vals, masses=(0.5, 0.9, 0.95), tail=0.05)
    assert r.map[0] == 0.0
    assert r.severe[0] > 0.0  # the worst 5% (~50 draws) includes the 40 active ones
    assert r.severe[0] >= r.map[0]


def test_enforce_non_negative_clips_map_only():
    r = collapse(np.full((1, 64), -5.0), masses=(0.9,), enforce_non_negative=True)
    assert r.map[0] >= 0.0


def test_1d_input_is_single_row():
    r = collapse(np.arange(80.0), masses=(0.9,))
    assert r.map.shape == (1,) and r.hdi[0.9].shape == (1, 2)


def test_empty_masses_fails_loud():
    with pytest.raises(ValueError):
        collapse(_samples(), masses=())


def test_bimodality_flag_zero_on_unimodal_one_on_bimodal():
    """The 0/1 flag (ADR-025 A.3) fires on a clearly separated, materially populated second
    mode and stays 0 on a unimodal distribution."""
    rng = np.random.default_rng(11)
    unimodal = rng.normal(5.0, 1.0, 400).astype(np.float32)
    bimodal = np.concatenate(
        [rng.normal(0.0, 0.3, 200), rng.normal(20.0, 0.5, 200)]
    ).astype(np.float32)
    r = collapse(np.stack([unimodal, bimodal]))
    assert r.bimodality[0] == 0.0
    assert r.bimodality[1] == 1.0
    # all-NaN row → NaN flag (matches the other stats)
    assert np.isnan(collapse(np.full((1, 32), np.nan)).bimodality[0])
