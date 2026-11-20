"""The table for one outcome at one level (#204, #206): the two rules the route tests cannot reach."""

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.serialize import outcome_table

_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


def test_a_region_whose_cells_are_all_missing_stays_missing():
    """Recorded deaths are summed over a region's cells. Missing everywhere is missing, not 0:
    a 0 would read as a peaceful month. One missing cell among recorded ones is skipped."""
    df = make_fao_df(targets=_TARGETS)
    # Region 1 is cells 100 and 101; region 2 is cells 102 and 103. Month 600 only.
    for cell in (100, 101):
        df.at[(600, cell), "pred_lr_ged_sb"] = np.full(10, np.nan)
    df.at[(600, 102), "pred_lr_ged_sb"] = np.full(10, np.nan)
    history = ForecastDataset(df)
    table = outcome_table.build_outcome_table(
        ForecastDataset(make_fao_df(targets=_TARGETS)), history, "admin1", "sb", months=[600]
    )
    actual = table.set_index("admin1_code")["sb_actual"]
    assert np.isnan(actual[1])
    assert actual[2] == pytest.approx(history._sample_array("pred_lr_ged_sb")[3, 0])


def test_two_country_codes_sharing_one_iso3_are_refused():
    """The dataset sums countries by ISO3 and partners name them by GAUL code. If two GAUL
    codes shared one ISO3, their rows would merge silently, so the table refuses instead."""
    df = make_fao_df(targets=_TARGETS)
    df["country_iso_a3"] = "AAA"   # GAUL countries 10 and 20 now share one ISO3
    forecast = ForecastDataset(df)
    with pytest.raises(ValueError, match="share one ISO3"):
        outcome_table.build_outcome_table(forecast, None, "country", "sb")


@pytest.mark.parametrize("level,place", [("admin1", "admin1_code"), ("country", "country_code")])
def test_place_ids_are_whole_numbers_when_the_geography_stores_floats(level, place):
    """The delivered geography stores GAUL codes as floats (1301.0). Partners must get 1301, on
    forecast and recorded rows alike."""
    df = make_fao_df(targets=_TARGETS)
    for column in ("admin1_gaul1_code", "admin1_gaul0_code"):
        df[column] = df[column].astype(float)
    forecast, history = ForecastDataset(df), ForecastDataset(df.copy())
    table = outcome_table.build_outcome_table(forecast, history, level, "sb")
    assert str(table[place].dtype) == "Int64"
    assert table["sb_map"].notna().all() and table["sb_actual"].notna().all()


@pytest.mark.parametrize("level", ["grid", "admin1"])
def test_every_code_column_is_a_whole_number_not_only_the_place_id(level):
    """At grid and admin-1 the row also names its admin-1 and country codes; those must be whole
    numbers too, not only the column that identifies the place."""
    df = make_fao_df(targets=_TARGETS)
    for column in ("admin1_gaul1_code", "admin1_gaul0_code"):
        df[column] = df[column].astype(float)
    table = outcome_table.build_outcome_table(ForecastDataset(df), None, level, "sb")
    for column in ("admin1_code", "country_code"):
        assert str(table[column].dtype) == "Int64", column


def test_one_place_with_two_names_is_refused():
    """A geography that gives one admin-1 code two names is broken; the table refuses rather
    than pick one."""
    df = make_fao_df(targets=_TARGETS)
    df.loc[(600, 101), "admin1_gaul1_name"] = "Region1-bis"   # cell 101 is also region 1
    with pytest.raises(ValueError, match="more than one name"):
        outcome_table.build_outcome_table(ForecastDataset(df), None, "admin1", "sb")


def _relabel_months(df, months):
    """The same rows, with the month ids replaced in order (one month per block of cells)."""
    cells = df.index.get_level_values("priogrid_id")
    old = df.index.get_level_values("month_id")
    mapping = dict(zip(sorted(set(old)), months))
    df = df.copy()
    df.index = pd.MultiIndex.from_arrays([[mapping[m] for m in old], cells], names=["month_id", "priogrid_id"])
    return df


def test_past_months_without_a_forecast_are_served_with_their_recorded_deaths():
    """In the real service the history reaches back decades and the forecast covers only the
    months ahead, overlapping in one month. A past month has a row with the recorded deaths
    and empty summaries; a forecast-only month has summaries and no recorded deaths."""
    history = ForecastDataset(_relabel_months(make_fao_df(n_months=2, targets=_TARGETS), [599, 600]))
    forecast = ForecastDataset(_relabel_months(make_fao_df(n_months=2, targets=_TARGETS, seed=1), [600, 601]))
    assert outcome_table.served_months(forecast, history) == [599, 600, 601]

    table = outcome_table.build_outcome_table(forecast, history, "admin1", "sb").set_index(["month", "admin1_code"])
    assert len(table) == 6   # two regions x three months
    past, overlap, ahead = table.loc["2029-11"], table.loc["2029-12"], table.loc["2030-01"]
    assert past["sb_actual"].notna().all() and past["sb_map"].isna().all()
    assert overlap["sb_actual"].notna().all() and overlap["sb_map"].notna().all()
    assert ahead["sb_actual"].isna().all() and ahead["sb_map"].notna().all()
