"""S3 (#222): the single output-schema module (ADR-025 §3/§4)."""

import numpy as np
import pytest

from views_faoapi.forecast.serialize import schema
from views_faoapi.forecast.summarize.estimator import collapse

pytestmark = pytest.mark.layer2_data


# --- masses -------------------------------------------------------------------------------
def test_masses_are_the_adr025_trio():
    assert schema.MASSES == (0.5, 0.9, 0.95)


def test_mass_pct_encodes_percent():
    assert schema.mass_pct(0.5) == 50
    assert schema.mass_pct(0.9) == 90
    assert schema.mass_pct(0.95) == 95


def test_mass_pct_unknown_mass_fails_loud():
    with pytest.raises(ValueError):
        schema.mass_pct(0.8)


def test_schema_masses_match_collapse_hdi_keys():
    """The reduction and the column names share MASSES, so hdi keys never drift from names."""
    r = collapse(np.random.default_rng(0).random((2, 128)) * 10, masses=schema.MASSES)
    assert set(r.masses) == set(schema.MASSES)
    for mass in schema.MASSES:
        # the key used by collapse is exactly the one the column name is derived from
        assert r.hdi[mass].shape == (2, 2)
        assert schema.hdi_col("sb", mass, "lower") == f"sb_hdi{schema.mass_pct(mass)}_lower"


# --- series mapping -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,expected",
    [
        ("pred_lr_ged_sb", "sb"),
        ("pred_ln_sb_best", "sb"),
        ("ged_ns_best", "ns"),
        ("pred_lr_ged_os", "os"),
        ("lr_ns_best", "ns"),
    ],
)
def test_series_of_strips_prefixes(name, expected):
    assert schema.series_of(name) == expected


def test_series_of_no_series_fails_loud():
    with pytest.raises(ValueError):
        schema.series_of("pred_lr_ged_foo")


def test_series_of_ambiguous_fails_loud():
    with pytest.raises(ValueError):
        schema.series_of("pred_sb_ns_best")  # two series tokens


# --- identity mapping is grounded in the real metadata contract ---------------------------
def test_identity_sources_are_real_metadata_columns():
    """Every identity source column must exist in the 9-column GAUL metadata contract —
    so a rename upstream can't leave the schema pointing at a phantom column."""
    from views_faoapi.data.handlers import ForecastDataset

    meta = set(ForecastDataset._METADATA_COLS)
    for consumer, source in schema.IDENTITY_SOURCE.items():
        assert source in meta, f"{consumer} maps to {source!r}, absent from _METADATA_COLS"


# --- the 33-column contract ---------------------------------------------------------------
def test_series_value_columns_are_the_ten_in_order():
    assert schema.series_value_columns("sb") == [
        "sb_map",
        "sb_hdi50_lower", "sb_hdi50_upper",
        "sb_hdi90_lower", "sb_hdi90_upper",
        "sb_hdi95_lower", "sb_hdi95_upper",
        "sb_severe_scenario",
        "sb_bimodality_flag",
        "sb_actual",
    ]


def test_bulk_layout_is_exactly_33_unique_columns():
    cols = schema.bulk_columns()
    assert len(cols) == 36
    assert len(set(cols)) == 36  # no duplicates
    assert cols[:6] == list(schema.IDENTITY_COLUMNS)
    # 9 value columns per series, in series order
    assert cols[6:16] == schema.series_value_columns("sb")
    assert cols[16:26] == schema.series_value_columns("ns")
    assert cols[26:36] == schema.series_value_columns("os")


def test_hdi_col_rejects_bad_bound():
    with pytest.raises(ValueError):
        schema.hdi_col("sb", 0.9, "middle")
