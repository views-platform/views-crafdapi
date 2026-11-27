"""The checks the notebooks and the smoke test run on served data (views_crafdapi.data_checks)."""
import numpy as np
import pandas as pd
import pytest

from views_crafdapi.data_checks import (
    DataCheckError, check_forecast_age, check_forecast_month_fits_its_date, check_forecast_summaries,
    check_bulk_file_complete, check_files_agree, check_horizon, check_levels_add_up, check_months,
    check_newest_delivery_served,
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


def _health(**override):
    health = {"status": "healthy", "forecast_loaded": True, "served_run": "run_a",
              "forecast_freshness": {"sla_days": 45.0, "age_days": 10.0, "is_stale": False}}
    health.update(override)
    return health


class TestNewestDeliveryServed:
    def test_a_loaded_forecast_with_nothing_pending_passes(self):
        assert "run_a" in check_newest_delivery_served(_health())

    def test_nothing_loaded_fails(self):
        with pytest.raises(DataCheckError, match="no forecast is loaded"):
            check_newest_delivery_served(_health(forecast_loaded=False))

    def test_a_newer_delivery_that_failed_to_load_fails(self):
        with pytest.raises(DataCheckError, match="a newer delivery is not served"):
            check_newest_delivery_served(_health(forecast_loading="the newest delivery could not be loaded"))

    def test_a_fallback_served_fails(self):
        with pytest.raises(DataCheckError, match="an older forecast is served"):
            check_newest_delivery_served(_health(forecast_serving_state={"degraded": True, "reason": "refused"}))

    def test_a_serving_state_that_is_not_degraded_passes(self):
        check_newest_delivery_served(_health(forecast_serving_state={"degraded": False}))

    def test_an_unverified_copy_passes_once_the_store_answers_again(self):
        """As on /health: a past failure to ask the store is shown, not alarmed on."""
        state = {"degraded": True, "reason": "manifest_lookup_error", "serving": "cached_unverified"}
        assert "store answers again" in check_newest_delivery_served(
            _health(forecast_serving_state=state, appwrite_connected=True))

    def test_a_normal_state_fails_while_the_store_cannot_be_reached(self):
        """As on /health, which is degraded when its store probe fails."""
        with pytest.raises(DataCheckError, match="cannot reach its store"):
            check_newest_delivery_served(_health(appwrite_connected=False))

    def test_an_unverified_copy_fails_while_the_store_cannot_be_reached(self):
        state = {"degraded": True, "reason": "manifest_lookup_error", "serving": "cached_unverified"}
        with pytest.raises(DataCheckError, match="cannot be confirmed"):
            check_newest_delivery_served(_health(forecast_serving_state=state, appwrite_connected=False))

    def test_a_last_good_fallback_fails_even_with_the_store_reachable(self):
        state = {"degraded": True, "reason": "newest run refused", "serving": "last_good_manifested"}
        with pytest.raises(DataCheckError, match="an older forecast is served"):
            check_newest_delivery_served(_health(forecast_serving_state=state, appwrite_connected=True))


class TestForecastAge:
    def test_within_the_limit_passes(self):
        assert "10 days old" in check_forecast_age(_health())

    def test_older_than_the_limit_fails(self):
        with pytest.raises(DataCheckError, match="55.0 days old, more than the 45-day limit"):
            check_forecast_age(_health(forecast_freshness={"sla_days": 45.0, "age_days": 55.0, "is_stale": True}))

    def test_exactly_at_the_limit_passes(self):
        check_forecast_age(_health(forecast_freshness={"sla_days": 45.0, "age_days": 45.0, "is_stale": False}))

    def test_just_over_the_limit_fails_even_when_rounding_shows_the_limit(self):
        """/health rounds the age to two decimals; its is_stale is computed before that."""
        with pytest.raises(DataCheckError, match="more than the 45-day limit"):
            check_forecast_age(_health(forecast_freshness={"sla_days": 45.0, "age_days": 45.0, "is_stale": True}))

    def test_over_the_limit_fails_without_the_stale_flag(self):
        with pytest.raises(DataCheckError, match="more than the 45-day limit"):
            check_forecast_age(_health(forecast_freshness={"sla_days": 45.0, "age_days": 46.0}))

    def test_a_fractional_limit_is_shown_as_it_is(self):
        with pytest.raises(DataCheckError, match="more than the 30.5-day limit"):
            check_forecast_age(_health(forecast_freshness={"sla_days": 30.5, "age_days": 31.0, "is_stale": True}))

    def test_the_limit_comes_from_health_not_from_a_constant(self):
        check_forecast_age(_health(forecast_freshness={"sla_days": 60.0, "age_days": 55.0, "is_stale": False}))

    def test_an_unknown_age_fails(self):
        with pytest.raises(DataCheckError, match="age is unknown"):
            check_forecast_age(_health(forecast_freshness={"error": "freshness check failed"}))

    def test_no_freshness_at_all_fails(self):
        health = _health()
        del health["forecast_freshness"]
        with pytest.raises(DataCheckError, match="age is unknown"):
            check_forecast_age(health)


class TestForecastMonthFitsItsDate:
    def test_delivered_in_its_first_month_passes(self):
        check_forecast_month_fits_its_date("2026-11", "2026-11-25T12:05:00+00:00")

    def test_delivered_the_month_after_passes(self):
        """FAO's July run was made on 2026-07-27 and stored on 2026-08-14."""
        check_forecast_month_fits_its_date("2026-07", "2026-08-14T18:35:54.962+00:00")

    def test_delivered_two_months_later_fails(self):
        with pytest.raises(DataCheckError, match="starts in 2026-09, but it is dated 2026-11, 2 months later"):
            check_forecast_month_fits_its_date("2026-09", "2026-11-14T08:00:00+00:00")

    def test_starting_after_its_delivery_fails(self):
        with pytest.raises(DataCheckError, match="starts in 2026-12, after its date \\(2026-11\\)"):
            check_forecast_month_fits_its_date("2026-12", "2026-11-30T12:00:00+00:00")

    def test_across_a_year_end(self):
        check_forecast_month_fits_its_date("2026-12", "2027-01-03T00:00:00Z")
        with pytest.raises(DataCheckError, match="2 months later"):
            check_forecast_month_fits_its_date("2026-11", "2027-01-03T00:00:00Z")

    def test_the_month_is_counted_in_utc(self):
        """23:30 on 31 October in UTC-2 is 1 November in UTC."""
        check_forecast_month_fits_its_date("2026-11", "2026-10-31T23:30:00-02:00")
        with pytest.raises(DataCheckError, match="after its date"):
            check_forecast_month_fits_its_date("2026-12", "2026-10-31T23:30:00-02:00")

    def test_an_unreadable_first_month_fails(self):
        with pytest.raises(DataCheckError, match="first month cannot be read"):
            check_forecast_month_fits_its_date("Nov 2026", "2026-11-25T12:05:00Z")

    def test_an_unknown_delivery_date_fails(self):
        with pytest.raises(DataCheckError, match="the forecast's date is unknown"):
            check_forecast_month_fits_its_date("2026-11", None)

    def test_an_unreadable_delivery_date_fails(self):
        with pytest.raises(DataCheckError, match="cannot be read"):
            check_forecast_month_fits_its_date("2026-11", "last tuesday")


class TestHorizon:
    def test_counts_the_forecast_months(self):
        table = pd.DataFrame({"month": ["2026-06", "2026-07", "2026-08", "2026-09"]})
        assert "3 forecast months, 2026-07 to 2026-09" in check_horizon(table, MADE_IN)

    def test_the_expected_number_passes(self):
        check_horizon(pd.DataFrame({"month": ["2026-07", "2026-08"]}), MADE_IN, 2)

    def test_a_short_horizon_fails(self):
        with pytest.raises(DataCheckError, match="2 forecast months .* expected 36"):
            check_horizon(pd.DataFrame({"month": ["2026-07", "2026-08"]}), MADE_IN, 36)

    def test_a_horizon_longer_than_expected_fails(self):
        with pytest.raises(DataCheckError, match="3 forecast months .* expected 2"):
            check_horizon(pd.DataFrame({"month": ["2026-07", "2026-08", "2026-09"]}), MADE_IN, 2)

    def test_no_forecast_months_fails(self):
        with pytest.raises(DataCheckError, match="no forecast months"):
            check_horizon(pd.DataFrame({"month": ["2026-05"]}), MADE_IN)


def _bulk(places=(1, 2, 3), recorded=("2026-05", "2026-06"), forecast=("2026-07", "2026-08")):
    rows = [{"month": m, "admin1_code": p, "sb_actual": 1.0, "n_draws": np.nan} for m in recorded for p in places]
    rows += [{"month": m, "admin1_code": p, "sb_actual": np.nan, "n_draws": 1024} for m in forecast for p in places]
    return pd.DataFrame(rows)


class TestBulkFileComplete:
    def test_a_complete_file_passes(self):
        assert "3 places in each of 4 months" in check_bulk_file_complete(_bulk(), "admin1", "sb", MADE_IN)

    def test_a_row_without_its_place_fails(self):
        table = _bulk()
        table.loc[0, "admin1_code"] = np.nan
        with pytest.raises(DataCheckError, match="1 rows do not name their place"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_a_month_with_places_missing_fails(self):
        table = _bulk()
        table = table[~((table["month"] == "2026-08") & (table["admin1_code"] == 3))]
        with pytest.raises(DataCheckError, match="different numbers of places: 2 in 2026-08"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_a_place_repeated_in_a_month_fails(self):
        """A month with one place twice and another missing has the right number of rows."""
        table = _bulk()
        table.loc[(table["month"] == "2026-08") & (table["admin1_code"] == 3), "admin1_code"] = 2
        with pytest.raises(DataCheckError, match="1 rows repeat a place in a month"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_a_place_swapped_for_another_in_one_month_fails(self):
        table = _bulk()
        table.loc[(table["month"] == "2026-08") & (table["admin1_code"] == 3), "admin1_code"] = 4
        with pytest.raises(DataCheckError, match="the months hold different places \\(4 in all, 3 in each month\\)"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_a_month_missing_fails(self):
        table = _bulk(forecast=("2026-07", "2026-09"))
        with pytest.raises(DataCheckError, match="admin1/sb: the forecast months have a gap"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_a_gap_in_the_recorded_months_fails(self):
        with pytest.raises(DataCheckError, match="admin1/sb: months missing between 2025-01 and 2026-06"):
            check_bulk_file_complete(_bulk(recorded=("2025-01", "2026-06")), "admin1", "sb", MADE_IN)

    def test_the_newest_recorded_month_without_deaths_fails(self):
        table = _bulk()
        table.loc[table["month"] == "2026-06", "sb_actual"] = np.nan
        with pytest.raises(DataCheckError, match="recorded months with no recorded deaths: 2026-06"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_a_row_without_a_month_fails(self):
        table = _bulk()
        table.loc[0, "month"] = None
        with pytest.raises(DataCheckError, match="1 rows have no month"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_no_recorded_months_fails(self):
        with pytest.raises(DataCheckError, match="no recorded months before 2026-07"):
            check_bulk_file_complete(_bulk(recorded=()), "admin1", "sb", MADE_IN)

    def test_no_forecast_months_fails(self):
        with pytest.raises(DataCheckError, match="no forecast months"):
            check_bulk_file_complete(_bulk(forecast=()), "admin1", "sb", MADE_IN)

    def test_a_forecast_month_without_draws_fails(self):
        """The failure this check exists for: the last month downloads, but carries nothing."""
        table = _bulk()
        table.loc[table["month"] == "2026-08", "n_draws"] = 0
        with pytest.raises(DataCheckError, match="forecast months with no usable draws in any row: 2026-08"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_some_rows_without_draws_pass(self):
        """A row with no usable draws is allowed (the data dictionary); a whole month is not."""
        table = _bulk()
        table.loc[(table["month"] == "2026-08") & (table["admin1_code"] == 1), "n_draws"] = 0
        check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_a_recorded_month_without_deaths_fails(self):
        table = _bulk()
        table.loc[table["month"] == "2026-05", "sb_actual"] = np.nan
        with pytest.raises(DataCheckError, match="recorded months with no recorded deaths: 2026-05"):
            check_bulk_file_complete(table, "admin1", "sb", MADE_IN)

    def test_a_missing_column_fails(self):
        with pytest.raises(DataCheckError, match="columns missing from the file: n_draws"):
            check_bulk_file_complete(_bulk().drop(columns=["n_draws"]), "admin1", "sb", MADE_IN)

    def test_an_empty_file_fails(self):
        with pytest.raises(DataCheckError, match="no rows"):
            check_bulk_file_complete(_bulk().iloc[0:0], "admin1", "sb", MADE_IN)


def _summary(made_in="2026-07", months=("2026-06", "2026-07"), places=3):
    return {"made_in": made_in, "months": list(months), "places": places}


class TestFilesAgree:
    def test_agreeing_files_pass(self):
        files = {(lv, o): _summary(places={"grid": 9, "admin1": 3}[lv]) for lv in ("grid", "admin1") for o in ("sb", "ns")}
        assert "all 4 files" in check_files_agree(files)

    def test_files_from_different_forecasts_fail(self):
        files = {("admin1", "sb"): _summary(), ("admin1", "ns"): _summary(made_in="2026-08", months=("2026-07", "2026-08"))}
        with pytest.raises(DataCheckError, match="different forecasts"):
            check_files_agree(files)

    def test_files_covering_different_months_fail(self):
        files = {("admin1", "sb"): _summary(), ("admin1", "ns"): _summary(months=("2026-05", "2026-06", "2026-07"))}
        with pytest.raises(DataCheckError, match="different months"):
            check_files_agree(files)

    def test_same_span_but_different_months_fail(self):
        files = {("admin1", "sb"): _summary(months=("2026-05", "2026-06", "2026-07")),
                 ("admin1", "ns"): _summary(months=("2026-05", "2026-05", "2026-07"))}
        with pytest.raises(DataCheckError, match="different months"):
            check_files_agree(files)

    def test_one_level_with_different_place_counts_fails(self):
        files = {("admin1", "sb"): _summary(), ("admin1", "ns"): _summary(places=2), ("grid", "sb"): _summary(places=9)}
        with pytest.raises(DataCheckError, match="admin1 files have different numbers of places"):
            check_files_agree(files)

    def test_files_with_no_months_fail(self):
        files = {("admin1", "sb"): _summary(months=()), ("admin1", "ns"): _summary(months=())}
        with pytest.raises(DataCheckError, match="files with no months"):
            check_files_agree(files)

    def test_no_files_fails(self):
        with pytest.raises(DataCheckError, match="no files"):
            check_files_agree({})
