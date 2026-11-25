"""Checks that the data the API serves is right, run by the smoke test (and by the notebooks, #264).

Each check takes tables as a partner downloads them (bulk files or slices) and raises
`DataCheckError` with a plain sentence when the data is wrong. Each returns a one-line summary
when it passes, for the notebook or the smoke test to print.

What "right" means here is what can be checked from the served data alone:
- months: the forecast months have no gaps, and recorded months end the month before the
  forecast starts;
- summaries: in forecast months, every row with usable draws has all its summaries (a row with
  no usable draws is empty, with `n_draws` 0, as the data dictionary says), chances lie between 0
  and 1 and fall as the threshold rises, expected deaths are not negative, and every row with
  draws comes from the same number of draws;
- levels: expected deaths and recorded deaths add up, month by month: the grid cells inside an
  admin-1 region add up to the admin-1 regions, and the grid cells inside a country add up to the
  countries. The API adds the draws up before summarising them, and the mean of a sum is the sum
  of the means, so the totals must agree to rounding. (A cell can belong to a country but no
  admin-1 region, so admin-1 regions are not compared with countries directly.)

Whether the numbers are plausible (a map that looks like the conflicts we know) is for a person
to judge; the notebooks draw the maps for that.

Whether the data is current for today is checked from `/health` and the months (notebook 07):
- the newest delivery is the one served: a forecast is loaded, no newer delivery failed to load,
  and no fallback is served;
- the forecast's age is within the service's own limit;
- the forecast's first month is the month of its date, or the month before;
- the number of forecast months, if one is expected.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd

from views_crafdapi.forecast.serialize.schema import EXCEEDANCE_THRESHOLDS

# Admin-1 and country draws are summed in float32 before summarising, then summed again here over
# up to ~65,000 cells. Relative 1e-5 is well above that rounding (a real run agreed within 1e-6)
# and far below any real disagreement (a misplaced cell moves a total by percents).
_RELATIVE_TOLERANCE = 1e-5


class DataCheckError(AssertionError):
    """The served data failed a check. The message says which, and what was found."""


def _month_after(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year + mon // 12}-{mon % 12 + 1:02d}"


def check_months(table: pd.DataFrame, made_in: str) -> str:
    """Forecast months (from `made_in`, ``2026-07``) have no gaps; recorded months, if any, end
    the month before. (That the forecast starts at `made_in` is not checked: the API derives the
    `X-Forecast-Made-In` header from the forecast's own first month, so it could not fail.) A
    history that stops two or more months before the forecast fails on purpose: partners would
    see a gap of months with neither recorded nor forecast deaths."""
    months = sorted(table["month"].astype(str).unique())
    forecast = [m for m in months if m >= made_in]
    recorded = [m for m in months if m < made_in]
    if not forecast:
        raise DataCheckError(f"no forecast months at or after {made_in}")
    for earlier, later in zip(forecast, forecast[1:]):
        if later != _month_after(earlier):
            raise DataCheckError(f"the forecast months have a gap between {earlier} and {later}")
    if recorded and _month_after(recorded[-1]) != made_in:
        raise DataCheckError(f"the recorded months end in {recorded[-1]}, not the month before {made_in}")
    summary = f"forecast months {forecast[0]} to {forecast[-1]} ({len(forecast)}), no gaps"
    if recorded:
        summary += f"; recorded months {recorded[0]} to {recorded[-1]}"
    return summary


def check_forecast_summaries(table: pd.DataFrame, outcome: str, made_in: str,
                             expected_draws: Optional[int] = None) -> str:
    """In forecast months, for rows with usable draws: no summary missing; chances in [0, 1] and
    falling with the threshold; expected deaths not negative; one number of draws everywhere
    (`expected_draws`, if given). Rows with no usable draws (`n_draws` 0) are empty by design and
    are only counted."""
    rows = table[table["month"].astype(str) >= made_in]
    if rows.empty:
        raise DataCheckError(f"no {outcome} rows in forecast months")
    if "n_draws" not in rows.columns:
        raise DataCheckError("columns missing from the table: n_draws")
    if rows["n_draws"].isna().any():   # a row with no usable draws says 0, never empty
        raise DataCheckError(f"empty values in forecast months (n_draws: {int(rows['n_draws'].isna().sum()):,})")
    no_draws = int((rows["n_draws"] == 0).sum())
    rows = rows[rows["n_draws"] > 0]
    if rows.empty:
        raise DataCheckError(f"no {outcome} row in forecast months has any usable draws")
    chances = [f"{outcome}_p_gt{c}" for c in EXCEEDANCE_THRESHOLDS]
    mean = f"{outcome}_mean"
    present = [c for c in chances + [mean, "n_draws"] if c in rows.columns]
    missing_columns = sorted(set(chances + [mean, "n_draws"]) - set(present))
    if missing_columns:
        raise DataCheckError(f"columns missing from the table: {', '.join(missing_columns)}")
    empty = rows[present].isna().sum()
    if empty.any():
        detail = ", ".join(f"{c}: {n:,}" for c, n in empty.items() if n)
        raise DataCheckError(f"empty values in forecast months ({detail})")
    values = rows[chances].to_numpy(dtype=float)
    if (values < 0).any() or (values > 1).any():
        raise DataCheckError(f"a chance outside 0 to 1 in {outcome}_p_gt*")
    if (np.diff(values, axis=1) > 1e-12).any():
        raise DataCheckError(f"a chance rises with the threshold in {outcome}_p_gt* (it must fall)")
    if (rows[mean] < 0).any():
        raise DataCheckError(f"negative expected deaths in {mean}")
    draws = rows["n_draws"].unique()
    if len(draws) != 1:
        raise DataCheckError(f"rows come from different numbers of draws: {sorted(draws)[:5]}")
    if expected_draws is not None and int(draws[0]) != expected_draws:
        raise DataCheckError(f"{int(draws[0])} draws per row, expected {expected_draws}")
    summary = f"{len(rows):,} forecast rows: no empty summaries, chances in order, {int(draws[0])} draws each"
    if no_draws:
        summary += f"; {no_draws:,} rows with no usable draws (empty by design)"
    return summary


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _RELATIVE_TOLERANCE * max(abs(a), abs(b), 1.0)


def check_levels_add_up(by_level: Mapping[str, pd.DataFrame], column: str) -> str:
    """`column` summed per month: grid cells inside an admin-1 region = admin-1 regions, and grid
    cells inside a country = countries. `by_level` maps "grid", "admin1" and "country" to tables
    of the same months."""
    grid = by_level["grid"]
    for level, table in by_level.items():
        filled = table.groupby("month")[column].count()
        empty_months = sorted(m for m, n in filled.items() if n == 0)
        if filled.empty or empty_months:
            where = ", ".join(map(str, empty_months)) or "every month"
            raise DataCheckError(f"{column} has no values at {level} level in {where}")
    totals = {
        "grid in admin-1": grid[grid["admin1_code"].notna()].groupby("month")[column].sum(),
        "admin-1": by_level["admin1"].groupby("month")[column].sum(),
        "grid in country": grid[grid["country_code"].notna()].groupby("month")[column].sum(),
        "country": by_level["country"].groupby("month")[column].sum(),
    }
    level_months = {level: set(by_level[level]["month"].astype(str)) for level in ("grid", "admin1", "country")}
    months = sorted(set.union(*level_months.values()))
    if not months:
        raise DataCheckError(f"no month present at any level for {column}")
    for level, present in level_months.items():
        absent = [m for m in months if m not in present]
        if absent:
            raise DataCheckError(f"{column}: {level} level has no rows for {', '.join(absent)}")
    for month in months:
        for cells, level in (("grid in admin-1", "admin-1"), ("grid in country", "country")):
            g, t = totals[cells].get(month, np.nan), totals[level].get(month, np.nan)
            if not _close(g, t):
                raise DataCheckError(
                    f"{column} does not add up in {month}: {cells} {g:,.3f}, {level} {t:,.3f}")
    return f"{column} adds up from grid cells to admin-1 regions and to countries in all {len(months)} months"


def check_newest_delivery_served(health: Mapping[str, Any]) -> str:
    """From `/health`: a forecast is loaded, no newer delivery failed to load, and the service
    does not serve an older forecast in place of the newest. Any of these means partners get an
    older forecast than the newest delivered, with answers that look normal.

    One serving state is not a failure, as on `/health` itself: `cached_unverified` (the last
    forecast was served while the store could not be asked) once the store answers again. While
    the store still cannot be asked, whether the forecast is the newest cannot be confirmed."""
    if not health.get("forecast_loaded"):
        raise DataCheckError("no forecast is loaded: every data request answers 503")
    if health.get("forecast_loading"):
        raise DataCheckError(f"a newer delivery is not served: {health['forecast_loading']}")
    state = health.get("forecast_serving_state") or {}
    run = health.get("served_run") or "not named"
    if health.get("appwrite_connected") is False:
        raise DataCheckError("whether the newest delivery is served cannot be confirmed: "
                             "the service cannot reach its store")
    if state.get("serving") == "cached_unverified":
        if health.get("appwrite_connected"):
            return (f"run {run} is served from the service's own copy; the store answers again, "
                    "and the service checks it for a newer delivery within minutes")
        raise DataCheckError("whether the newest delivery is served cannot be confirmed: "
                             "the service cannot reach its store")
    if state.get("degraded"):
        raise DataCheckError(f"an older forecast is served in place of the newest: {state.get('reason') or 'no reason given'}")
    return f"the newest delivery is served (run {run})"


def check_forecast_age(health: Mapping[str, Any]) -> str:
    """From `/health`'s `forecast_freshness`: the forecast's age is known and within the service's
    own limit (`sla_days`). An unknown age fails: nothing then says the forecast is current."""
    fr = health.get("forecast_freshness") or {}
    age, limit = fr.get("age_days"), fr.get("sla_days")
    if age is None or limit is None:
        raise DataCheckError("the forecast's age is unknown: /health does not report it")
    if fr.get("is_stale") or age > limit:   # is_stale is computed before /health rounds the age
        raise DataCheckError(f"the forecast is {age:.1f} days old, more than the {limit:g}-day limit")
    return f"the forecast is {age:.0f} days old, within the {limit:g}-day limit"


def check_forecast_month_fits_its_date(made_in: str, dated: Optional[str]) -> str:
    """The forecast's first month (`made_in`, ``2026-11``) is the month of the forecast's date, or
    the month before. The date is `/health`'s `forecast_freshness.created_at` (ISO-8601): the
    run's own date when the producer stamps one, otherwise when the delivery was stored. A forecast
    is made once the previous month's recorded deaths arrive, and delivered then or in the
    following month, so its first month is never more than a month before that date. A run made on
    old months fails here; an old run delivered again keeps its old date and fails the age check.

    With the age check, this bounds how old the months can be. It does not compare them with
    today: between deliveries a current forecast's first month is legitimately one or two months
    behind today's."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(made_in)):
        raise DataCheckError(f"the forecast's first month cannot be read: {made_in!r}")
    if not dated:
        raise DataCheckError("the forecast's date is unknown: /health does not report it")
    try:
        stamp = datetime.fromisoformat(str(dated).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(timezone.utc)   # the month in UTC, as /health counts the age
    except ValueError:
        raise DataCheckError(f"the forecast's date cannot be read: {dated!r}") from None
    month = f"{stamp.year}-{stamp.month:02d}"
    lag = (stamp.year * 12 + stamp.month) - (int(made_in[:4]) * 12 + int(made_in[5:7]))
    if lag < 0:
        raise DataCheckError(f"the forecast starts in {made_in}, after its date ({month})")
    if lag > 1:
        raise DataCheckError(f"the forecast starts in {made_in}, but it is dated {month}, {lag} months later")
    return f"the forecast starts in {made_in} and is dated {month}"


def check_horizon(table: pd.DataFrame, made_in: str, expected_months: Optional[int] = None) -> str:
    """The number of forecast months (from `made_in` on), and, if given, that it is
    `expected_months`. A delivery cut short shows up here as a short horizon."""
    forecast = sorted(m for m in table["month"].astype(str).unique() if m >= made_in)
    if not forecast:
        raise DataCheckError(f"no forecast months at or after {made_in}")
    if expected_months is not None and len(forecast) != expected_months:
        raise DataCheckError(f"{len(forecast)} forecast months ({forecast[0]} to {forecast[-1]}), "
                             f"expected {expected_months}")
    return f"{len(forecast)} forecast months, {forecast[0]} to {forecast[-1]}"
