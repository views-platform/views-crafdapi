"""The table for one outcome at one level (#204, #206): the rules the route tests cannot reach."""

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.serialize import outcome_table

_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


def _forecast_from(first_month, n_months=2, seed=1):
    """A forecast whose months start at `first_month`. Recorded deaths are served only for
    months before the forecast, so a test reading a history month needs a later forecast."""
    df = make_fao_df(n_months=n_months, seed=seed, targets=_TARGETS)
    return ForecastDataset(_relabel_months(df, list(range(first_month, first_month + n_months))))


def test_a_region_whose_cells_are_all_missing_stays_missing():
    """Recorded deaths are summed over a region's cells. Missing everywhere is missing, not 0:
    a 0 would read as a peaceful month. One missing cell among recorded ones is skipped."""
    df = make_fao_df(targets=_TARGETS)
    # Region 1 is cells 100 and 101; region 2 is cells 102 and 103. Month 600 only.
    for cell in (100, 101):
        df.at[(600, cell), "pred_lr_ged_sb"] = np.full(10, np.nan)
    df.at[(600, 102), "pred_lr_ged_sb"] = np.full(10, np.nan)
    history = ForecastDataset(df)
    table = outcome_table.build_outcome_table(_forecast_from(601), history, "admin1", "sb", months=[600])
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
    forecast = ForecastDataset(_relabel_months(df, [600, 601]))
    history = ForecastDataset(_relabel_months(df.copy(), [598, 599]))
    table = outcome_table.build_outcome_table(forecast, history, level, "sb")
    assert str(table[place].dtype) == "Int64" and table[place].notna().all()
    assert table["sb_mean"].notna().sum() == table["sb_actual"].notna().sum() == len(table) // 2


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
    assert outcome_table.forecast_months(forecast) == [600, 601]
    assert outcome_table.recorded_months(forecast, history) == [599]   # 600 is the frontier

    table = outcome_table.build_outcome_table(forecast, history, "admin1", "sb").set_index(["month", "admin1_code"])
    assert len(table) == 6   # two regions x three months
    past, overlap, ahead = table.loc["2029-11"], table.loc["2029-12"], table.loc["2030-01"]
    assert past["sb_actual"].notna().all() and past["sb_mean"].isna().all()
    # The frontier month is recorded in the history but served as forecast only (C-293).
    assert overlap["sb_actual"].isna().all() and overlap["sb_mean"].notna().all()
    assert ahead["sb_actual"].isna().all() and ahead["sb_mean"].notna().all()


def _history_with_values(values_by_cell, month=600):
    """A real historical dataset (one recorded value per cell, float64 feature store) for one
    month, cells 100.. in order, with the fixture's geography."""
    base = make_fao_df(n_cells=len(values_by_cell), n_months=1, n_samples=1)
    feat = base.drop(columns=[c for c in base.columns if c.startswith("pred_")])
    feat = _relabel_months(feat, [month])
    feat["lr_ged_sb"] = np.asarray(values_by_cell, dtype=np.float64)
    return ForecastDataset(feat, targets=["lr_ged_sb"])


def test_recorded_deaths_are_read_at_full_precision():
    """The history is stored as float64. 16,777,217 is the first whole number float32 cannot
    hold; read through a float32 copy it would be served as 16,777,216."""
    history = _history_with_values([16_777_217.0, 1.0, 2.0, 3.0])
    forecast = _forecast_from(601)
    table = outcome_table.build_outcome_table(forecast, history, "grid", "sb", places=[100], months=[600])
    assert table["sb_actual"].tolist() == [16_777_217.0]


def test_a_place_only_the_history_knows_is_left_out_not_served_nameless():
    """The forecast's geography decides which places exist. A cell only the history has would
    otherwise come back with no names, while asking for it by id is refused."""
    # The forecast starts after the history, so the history's months are read at all.
    forecast = ForecastDataset(_relabel_months(make_fao_df(n_cells=2, targets=_TARGETS), [602, 603]))   # cells 100, 101
    history = ForecastDataset(make_fao_df(n_cells=4, targets=_TARGETS))      # cells 100-103, months 600-601
    table = outcome_table.build_outcome_table(forecast, history, "grid", "sb")
    assert sorted(table["priogrid_id"].unique()) == [100, 101]
    assert table["admin1_name"].notna().all()
    assert table["sb_actual"].notna().sum() == 4   # the history was read: 2 cells x 2 months
    # At admin 1 the same holds: region 2 (cells 102, 103) exists only in the history.
    table = outcome_table.build_outcome_table(forecast, history, "admin1", "sb")
    assert sorted(table["admin1_code"].unique()) == [1]
    assert table["admin1_name"].notna().all()


def test_a_country_code_without_an_iso3_is_named_in_the_refusal():
    df = make_fao_df(targets=_TARGETS)
    df.loc[df["admin1_gaul0_code"] == 20, "country_iso_a3"] = None
    with pytest.raises(ValueError, match=r"without an ISO3 code.*\[20\]"):
        outcome_table.build_outcome_table(ForecastDataset(df), None, "country", "sb")


def test_only_the_months_asked_for_are_read_from_the_history():
    """Months are read as blocks of the month-sorted history: a run of consecutive months as one
    block, months with gaps between them as several. The months around them stay out."""
    df = _relabel_months(make_fao_df(n_months=4, targets=_TARGETS), [595, 596, 597, 598])
    history = ForecastDataset(df)
    forecast = _forecast_from(600)
    for months, expected in (
        ([597, 596], ["2029-08", "2029-09"]),            # one run
        ([598, 595], ["2029-07", "2029-10"]),            # a gap
        ([595, 596, 598], ["2029-07", "2029-08", "2029-10"]),
    ):
        table = outcome_table.build_outcome_table(forecast, history, "admin1", "sb", months=months)
        assert sorted(table["month"].unique()) == expected, months
        assert table["sb_actual"].notna().all()


def test_a_history_indexed_cell_first_is_refused_not_misread():
    """The months are read by position in the index. A history indexed (cell, month) would have
    its cells read as months; it is refused instead."""
    history = ForecastDataset(_relabel_months(make_fao_df(targets=_TARGETS), [598, 599]))
    history.geo_metadata = history.geo_metadata.swaplevel().sort_index()
    with pytest.raises(ValueError, match="indexed"):
        outcome_table.build_outcome_table(_forecast_from(600), history, "grid", "sb")


def test_two_targets_for_one_outcome_are_refused_not_guessed():
    """A log series and a raw series both map to `sb`; serving the first under `sb_*` would be
    a guess with a factor-of-e error. The bulk table refuses the same input."""
    df = make_fao_df(targets=("pred_ln_ged_sb_prob", "pred_lr_ged_sb"))
    with pytest.raises(ValueError, match="several targets hold outcome 'sb'"):
        outcome_table.build_outcome_table(ForecastDataset(df), None, "grid", "sb")


def test_grid_rows_carry_the_cell_centre():
    forecast = ForecastDataset(make_fao_df(targets=_TARGETS))
    table = outcome_table.build_outcome_table(forecast, None, "grid", "sb").set_index("priogrid_id")
    assert table.loc[101, ["latitude", "longitude"]].iloc[0].tolist() == [21.0, 11.0]   # pg_ycoord, pg_xcoord



def test_an_unsorted_history_is_refused_not_misread():
    """The months are read as blocks of rows. A history not sorted by month would give blocks
    holding other months' rows."""
    history = ForecastDataset(_relabel_months(make_fao_df(targets=_TARGETS), [598, 599]))
    history.geo_metadata = history.geo_metadata.iloc[::-1]
    with pytest.raises(ValueError, match="not sorted"):
        outcome_table.build_outcome_table(_forecast_from(600), history, "grid", "sb")


def _float32_summaries(forecast, monkeypatch):
    """Make the forecast's summaries arrive as float32, as a narrower reduction would."""
    real = forecast.summarize
    monkeypatch.setattr(forecast, "summarize", lambda **kw: _numbers_as_float32(real(**kw)))


def test_every_value_column_is_float64_whatever_the_summaries_arrive_as(monkeypatch):
    """faoapi #487: served numeric columns are float64, even empty ones."""
    forecast = _forecast_from(600)
    _float32_summaries(forecast, monkeypatch)
    table = outcome_table.build_outcome_table(forecast, None, "admin1", "sb")
    for column in outcome_table.value_columns("sb"):
        assert table[column].dtype == np.float64, column


def test_a_table_breaking_a_summary_relation_is_refused(monkeypatch):
    """The check runs on every served table: a chance rising with the threshold stops it."""
    forecast = _forecast_from(600)
    real = forecast.summarize

    def broken(**kw):
        out = real(**kw)
        out["pred_lr_ged_sb_p_gt100"] = 1.0
        out["pred_lr_ged_sb_p_gt25"] = 0.0
        return out
    monkeypatch.setattr(forecast, "summarize", broken)
    with pytest.raises(ValueError, match="sb_p_gt100 is above sb_p_gt25"):
        outcome_table.build_outcome_table(forecast, None, "grid", "sb")


def _numbers_as_float32(frame):
    numbers = frame.select_dtypes("number").columns
    return frame.astype({c: "float32" for c in numbers})
