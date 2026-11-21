"""The served column names (`schema`), as decided for #204 on 2026-11-17."""

import numpy as np
import pytest

from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.summarize.draws import summarize_draws

pytestmark = pytest.mark.layer2_data


# --- the summaries -----------------------------------------------------------------------
def test_the_summaries_are_the_decided_set_in_order():
    assert schema.SUMMARY_QUANTITIES == (
        "p_gt0", "p_gt25", "p_gt100", "p_gt500", "p_gt1000",
        "mean", "mean_if_any", "median_if_any", "severe_scenario", "n_draws",
    )


def test_the_reduction_returns_exactly_the_named_summaries():
    """The reduction and the column names share one list, so they cannot drift apart."""
    out = summarize_draws(np.random.default_rng(0).random((2, 128)) * 10)
    assert tuple(out) == schema.SUMMARY_QUANTITIES


def test_an_unknown_summary_has_no_column_name():
    with pytest.raises(ValueError):
        schema.summary_col("sb", "map")


@pytest.mark.parametrize("column,expected", [
    ("pred_lr_ged_sb_p_gt1000", "sb_p_gt1000"),
    ("pred_lr_ged_sb_p_gt100", "sb_p_gt100"),
    ("pred_lr_ged_ns_mean_if_any", "ns_mean_if_any"),
    ("pred_lr_ged_ns_mean", "ns_mean"),
    ("pred_lr_ged_os_n_draws", "os_n_draws"),
    ("admin1_gaul1_name", "admin1_gaul1_name"),
    ("sb_actual", "sb_actual"),
])
def test_the_served_rename_keeps_the_whole_summary_name(column, expected):
    assert schema.consumer_column(column) == expected



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
    from views_crafdapi.data.handlers import ForecastDataset

    meta = set(ForecastDataset._METADATA_COLS)
    for consumer, source in schema.IDENTITY_SOURCE.items():
        assert source in meta, f"{consumer} maps to {source!r}, absent from _METADATA_COLS"


# --- one outcome's value columns -------------------------------------------------------
def test_series_value_columns_are_the_eleven_in_order():
    assert schema.series_value_columns("sb") == [
        "sb_p_gt0", "sb_p_gt25", "sb_p_gt100", "sb_p_gt500", "sb_p_gt1000",
        "sb_mean", "sb_mean_if_any", "sb_median_if_any", "sb_severe_scenario",
        "sb_actual", "sb_n_draws",
    ]
