"""The checks the notebooks and the smoke test run on served data (views_crafdapi.data_checks)."""
import numpy as np
import pandas as pd
import pytest

from views_crafdapi.data_checks import (
    DataCheckError, check_forecast_summaries, check_levels_add_up, check_months,
)

MADE_IN = "2026-07"


def _forecast_rows(months=("2026-07", "2026-08"), draws=1024, **override):
    rows = []
    for m in months:
        row = {"month": m, "sb_p_gt0": 0.5, "sb_p_gt25": 0.2, "sb_p_gt100": 0.1, "sb_p_gt500": 0.01,
               "sb_p_gt1000": 0.0, "sb_mean": 3.0, "n_draws": draws}
        row.update(override)
        rows.append(row)
    return pd.DataFrame(rows)


class TestMonths:
    def test_contiguous_forecast_after_recorded_months_passes(self):
        table = pd.DataFrame({"month": ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]})
        assert "2026-07 to 2026-09" in check_months(table, MADE_IN)

    def test_across_a_year_end(self):
        table = pd.DataFrame({"month": ["2026-11", "2026-12", "2027-01"]})
        check_months(table, "2026-11")

    def test_a_january_forecast_after_a_december_history(self):
        check_months(pd.DataFrame({"month": ["2026-11", "2026-12", "2027-01", "2027-02"]}), "2027-01")

    def test_a_gap_fails(self):
        with pytest.raises(DataCheckError, match="gap between 2026-07 and 2026-09"):
            check_months(pd.DataFrame({"month": ["2026-07", "2026-09"]}), MADE_IN)

    def test_recorded_months_ending_early_fail(self):
        with pytest.raises(DataCheckError, match="recorded months end in 2026-05"):
            check_months(pd.DataFrame({"month": ["2026-05", "2026-07"]}), MADE_IN)


class TestForecastSummaries:
    def test_good_rows_pass(self):
        assert "1024 draws each" in check_forecast_summaries(_forecast_rows(), "sb", MADE_IN, 1024)

    def test_recorded_months_are_not_checked(self):
        table = pd.concat([_forecast_rows(), pd.DataFrame([{"month": "2026-06", "sb_mean": np.nan}])])
        check_forecast_summaries(table, "sb", MADE_IN)

    def test_a_row_with_no_usable_draws_may_be_empty(self):
        """The data dictionary: no usable draws means empty summaries and n_draws 0."""
        empty = _forecast_rows(months=("2026-08",), draws=0, sb_mean=np.nan, sb_p_gt0=np.nan)
        table = pd.concat([_forecast_rows(months=("2026-07",)), empty])
        assert "1 rows with no usable draws" in check_forecast_summaries(table, "sb", MADE_IN, 1024)

    def test_only_rows_without_draws_fails(self):
        with pytest.raises(DataCheckError, match="no sb row in forecast months has any usable draws"):
            check_forecast_summaries(_forecast_rows(draws=0, sb_mean=np.nan), "sb", MADE_IN)

    def test_an_empty_summary_fails(self):
        with pytest.raises(DataCheckError, match="empty values"):
            check_forecast_summaries(_forecast_rows(sb_mean=np.nan), "sb", MADE_IN)

    def test_a_chance_rising_at_the_first_threshold_fails(self):
        with pytest.raises(DataCheckError, match="rises with the threshold"):
            check_forecast_summaries(_forecast_rows(sb_p_gt0=0.1, sb_p_gt25=0.2), "sb", MADE_IN)

    def test_an_empty_number_of_draws_fails(self):
        table = _forecast_rows()
        table.loc[0, "n_draws"] = np.nan
        with pytest.raises(DataCheckError, match="n_draws"):
            check_forecast_summaries(table, "sb", MADE_IN)

    def test_a_negative_chance_fails(self):
        with pytest.raises(DataCheckError, match="outside 0 to 1"):
            check_forecast_summaries(_forecast_rows(sb_p_gt1000=-0.1), "sb", MADE_IN)

    def test_a_chance_above_one_fails(self):
        with pytest.raises(DataCheckError, match="outside 0 to 1"):
            check_forecast_summaries(_forecast_rows(sb_p_gt0=1.2), "sb", MADE_IN)

    def test_a_chance_rising_with_the_threshold_fails(self):
        with pytest.raises(DataCheckError, match="rises with the threshold"):
            check_forecast_summaries(_forecast_rows(sb_p_gt100=0.3), "sb", MADE_IN)

    def test_negative_expected_deaths_fail(self):
        with pytest.raises(DataCheckError, match="negative"):
            check_forecast_summaries(_forecast_rows(sb_mean=-1.0), "sb", MADE_IN)

    def test_the_wrong_number_of_draws_fails(self):
        with pytest.raises(DataCheckError, match="128 draws per row, expected 1024"):
            check_forecast_summaries(_forecast_rows(draws=128), "sb", MADE_IN, 1024)

    def test_more_draws_than_expected_fails(self):
        with pytest.raises(DataCheckError, match="2048 draws per row, expected 1024"):
            check_forecast_summaries(_forecast_rows(draws=2048), "sb", MADE_IN, 1024)

    def test_mixed_numbers_of_draws_fail(self):
        table = pd.concat([_forecast_rows(months=("2026-07",)), _forecast_rows(months=("2026-08",), draws=512)])
        with pytest.raises(DataCheckError, match="different numbers of draws"):
            check_forecast_summaries(table, "sb", MADE_IN)

    def test_a_missing_column_fails(self):
        with pytest.raises(DataCheckError, match="columns missing"):
            check_forecast_summaries(_forecast_rows().drop(columns=["n_draws"]), "sb", MADE_IN)


def _levels(grid_values=(1.0, 2.0, 4.0), admin1_values=(3.0, 4.0), country_value=8.0):
    """Three cells in two regions, plus one cell in the country but in no region (1.0), plus one
    cell in no country (100.0): admin-1 = 7.0, country = 8.0."""
    grid = pd.DataFrame({"month": MADE_IN, "admin1_code": [1, 1, 2], "country_code": [9, 9, 9],
                         "sb_mean": list(grid_values)})
    no_region = pd.DataFrame({"month": [MADE_IN], "admin1_code": [pd.NA], "country_code": [9], "sb_mean": [1.0]})
    outside = pd.DataFrame({"month": [MADE_IN], "admin1_code": [pd.NA], "country_code": [pd.NA], "sb_mean": [100.0]})
    admin1 = pd.DataFrame({"month": MADE_IN, "admin1_code": [1, 2], "sb_mean": list(admin1_values)})
    country = pd.DataFrame({"month": [MADE_IN], "sb_mean": [country_value]})
    return {"grid": pd.concat([grid, no_region, outside]), "admin1": admin1, "country": country}


class TestLevelsAddUp:
    def test_matching_totals_pass_and_a_cell_in_a_country_but_no_region_counts_for_the_country(self):
        assert "adds up" in check_levels_add_up(_levels(), "sb_mean")

    def test_rounding_differences_pass(self):
        check_levels_add_up(_levels(country_value=8.0 * (1 + 2e-6)), "sb_mean")

    def test_a_small_real_difference_fails(self):
        """0.01% apart is far above rounding: one misplaced cell moves a total by more."""
        with pytest.raises(DataCheckError, match="does not add up"):
            check_levels_add_up(_levels(country_value=8.0 * (1 + 1e-4)), "sb_mean")

    def test_grid_and_admin1_disagreeing_fails(self):
        with pytest.raises(DataCheckError, match="grid in admin-1 8.000, admin-1 7.000"):
            check_levels_add_up(_levels(grid_values=(1.0, 2.0, 5.0)), "sb_mean")

    def test_grid_and_country_disagreeing_fails(self):
        with pytest.raises(DataCheckError, match="grid in country 8.000, country 9.000"):
            check_levels_add_up(_levels(country_value=9.0), "sb_mean")

    def test_an_entirely_empty_column_fails_rather_than_adding_up_to_zero(self):
        levels = {k: v.assign(sb_mean=np.nan) for k, v in _levels().items()}
        with pytest.raises(DataCheckError, match="no values at grid level"):
            check_levels_add_up(levels, "sb_mean")

    def test_a_month_missing_at_one_level_fails(self):
        levels = _levels()
        levels["country"] = pd.concat([levels["country"], pd.DataFrame({"month": ["2026-08"], "sb_mean": [1.0]})])
        with pytest.raises(DataCheckError, match="grid level has no rows for 2026-08"):
            check_levels_add_up(levels, "sb_mean")

    def test_one_level_empty_fails(self):
        levels = _levels()
        levels["country"] = levels["country"].assign(sb_mean=np.nan)
        with pytest.raises(DataCheckError, match="no values at country level in 2026-07"):
            check_levels_add_up(levels, "sb_mean")
