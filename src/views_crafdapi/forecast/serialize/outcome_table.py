"""The table for one outcome at one level: what a slice serves, and what a bulk file will hold.

The service specification (docs/SERVICE_SPECIFICATION.md) and the partner guide
(docs/api/README.md) define it:

- one row per place and month;
- the place's id and names first, then `month` (written `2027-01`) and VIEWS' `month_id`
  (decided 2026-11-17, #205 and #206); grid rows also carry the cell's latitude and longitude;
- then the outcome's summary columns (`schema.SUMMARY_QUANTITIES`, decided 2026-11-17, #204),
  its recorded number of deaths in `{outcome}_actual`, and `n_draws`;
- forecast months carry the summary columns; months before the forecast carry the recorded deaths.

The newest recorded month is also the first forecast month, and only a sliver of it has been
reported when the history is cut, with unreported cells stored as 0. Served, it would show 0
deaths next to the forecast. So recorded deaths are served only for months before the first
forecast month (register C-293).

Partners never get the samples. The summaries come from `ForecastDataset.summarize`; above the
grid cell the draws are summed over the cells first, then summarised. Every value column is
float64, even when empty, and the table is checked (`summary_checks`) before it is served.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.serialize.json_contract import to_consumer_columns
from views_crafdapi.forecast.serialize.summary_checks import check_summaries
from views_crafdapi.time import month_id_to_date

# The partner's level code -> the dataset's level name (`None` is the grid cell).
LEVELS: dict[str, Optional[str]] = {"grid": None, "admin1": "gaul1", "country": "country"}

# The column that identifies a place at each level, in the partner's names.
PLACE_COLUMN: dict[str, str] = {
    "grid": "priogrid_id",
    "admin1": "admin1_code",
    "country": "country_code",
}

# The place's id and names (#205, 2026-11-17), always served whatever `columns` asks for.
PLACE_COLUMNS: dict[str, list[str]] = {
    "grid": [
        "priogrid_id", "latitude", "longitude",
        "admin1_code", "admin1_name", "country_code", "country_name", "country_iso3",
    ],
    "admin1": ["admin1_code", "admin1_name", "country_code", "country_name", "country_iso3"],
    "country": ["country_code", "country_name", "country_iso3"],
}

# Every row's identity: the place, then the month written two ways (#206, 2026-11-17).
IDENTITY_COLUMNS: dict[str, list[str]] = {
    level: columns + ["month", "month_id"] for level, columns in PLACE_COLUMNS.items()
}

# The cell's centre, under the names partners see. The geography stores them as pg_ycoord/xcoord.
_COORDINATE_SOURCE = {"latitude": "pg_ycoord", "longitude": "pg_xcoord"}

_CELL = "priogrid_id"
_MONTH = "month_id"


def outcomes(forecast_ds) -> list[str]:
    """The outcome codes served (`sb`, `ns`, `os` today), one per forecast target."""
    return [schema.series_of(t) for t in forecast_ds.targets]


def _one_outcome_names(outcome: str) -> dict[str, str]:
    """`{outcome}_n_draws` is served as `n_draws`: a table holds one outcome, so the count of
    draws needs no prefix. The one place that rename is written."""
    return {schema.summary_col(outcome, "n_draws"): "n_draws"}


def value_columns(outcome: str) -> list[str]:
    """The value columns for one outcome: its summaries, its recorded deaths, then `n_draws`."""
    names = _one_outcome_names(outcome)
    return [names.get(c, c) for c in schema.series_value_columns(outcome)]


def target_for(dataset, outcome: str) -> Optional[str]:
    """The dataset target holding `outcome`, or `None` when the dataset has none.

    Two targets for one outcome (say a log and a raw series) cannot both be served under one
    name, and picking one would be a guess, so that is refused.
    """
    found = [t for t in dataset.targets if schema.series_of(t) == outcome]
    if len(found) > 1:
        raise ValueError(f"several targets hold outcome {outcome!r}: {found}")
    return found[0] if found else None


def forecast_months(forecast_ds) -> list[int]:
    return sorted(int(m) for m in forecast_ds._time_values)


def recorded_months(forecast_ds, historical_ds) -> list[int]:
    """The months whose recorded deaths are served: the history's, before the first forecast
    month (see the module docstring)."""
    if historical_ds is None:
        return []
    first = forecast_months(forecast_ds)[0]
    return sorted(int(m) for m in historical_ds._time_values if m < first)


def needs_history(forecast_ds, months: Optional[list[int]]) -> bool:
    """Whether a request reaches months before the forecast, and so needs the history.

    A request for forecast months only is answered from the forecast alone, so a missing
    history does not take it away.
    """
    return months is None or min(months) < forecast_months(forecast_ds)[0]


def _one_month_of_geography(forecast_ds) -> pd.DataFrame:
    """The forecast's geography for one month, when every month has the same cells (they do:
    one row per cell per month). Building the place table from one month instead of all of them
    takes a few hundredths of a second instead of one to two seconds. If the months do not have
    the same number of rows, all of them are used."""
    geo = forecast_ds.geo_metadata
    months = forecast_months(forecast_ds)
    start, stop = geo.index.slice_locs((months[0],), (months[0],))
    if (stop - start) * len(months) != len(geo):
        return geo
    return geo.iloc[start:stop]


def place_identity(forecast_ds, level: str) -> pd.DataFrame:
    """One row per place at `level`, indexed by the place id, carrying the place's names
    (and, for a grid cell, its latitude and longitude).

    Built from the forecast's per-cell geography. Fails loudly if one place id maps to two
    different names: that is a broken geography table, not something to guess around.
    """
    geo = _one_month_of_geography(forecast_ds)
    ident = pd.DataFrame({_CELL: geo.index.get_level_values(_CELL)})
    for consumer, source in {**schema.IDENTITY_SOURCE, **_COORDINATE_SOURCE}.items():
        ident[consumer] = geo[source].to_numpy()
    # The geography stores the GAUL codes as floats; partners write them as whole numbers.
    for code in ("admin1_code", "country_code"):
        ident[code] = ident[code].astype("Int64")
    columns = PLACE_COLUMNS[level]
    place = PLACE_COLUMN[level]
    # A place whose cells give its ISO3 on some rows and nothing on others has one ISO3, not two
    # names: fill the gaps from the place's own value (#225).
    ident["country_iso3"] = ident.groupby(place)["country_iso3"].transform("first")
    ident = ident[columns].drop_duplicates()
    if ident[place].duplicated().any():
        bad = sorted(ident.loc[ident[place].duplicated(keep=False), place].unique().tolist())
        raise ValueError(f"place ids at level {level!r} map to more than one name: {bad[:10]}")
    return ident.set_index(place)


def cells_read(forecast_ds, level: str, places: Optional[list[int]]) -> int:
    """How many grid cells a request reads: the places' own cells, not the rows it answers with.
    A country is one row in the answer and hundreds of cells in the work."""
    geo = _one_month_of_geography(forecast_ds)
    if places is None:
        return len(geo)
    if level == "grid":
        return len(places)
    source = schema.IDENTITY_SOURCE[PLACE_COLUMN[level]]
    return int(np.isin(geo[source].to_numpy(), places).sum())


def _forecast_rows(forecast_ds, level, outcome, places, months, identity) -> Optional[pd.DataFrame]:
    """The summary columns for the forecast months asked for, indexed (month_id, place)."""
    target = target_for(forecast_ds, outcome)
    if target is None:
        return None
    asked = forecast_months(forecast_ds)
    if months is not None:
        asked = [m for m in months if m in set(asked)]
        if not asked:
            return None
    dataset_level = LEVELS[level]
    table = forecast_ds.summarize(
        features=[target],
        time_ids=asked,
        entity_ids=places,
        level=dataset_level,
        aggregate=dataset_level is not None,
        with_metadata=False,
    )
    table = to_consumer_columns(table).rename(columns=_one_outcome_names(outcome))
    table.index = table.index.set_names([_MONTH, PLACE_COLUMN[level]])
    return table


def _recorded_values(historical_ds, target) -> np.ndarray:
    """One recorded value per row, as stored. The history keeps them as float64 in its feature
    store; `_sample_array` would hand back a float32 copy of the whole column. A dataset whose
    target is a sample column (the tests use one dataset as forecast and history) has no
    feature store entry, so its first draw is read instead."""
    stored = getattr(historical_ds, "_feature_store", {}).get(target)
    if stored is not None:
        return stored[:, 0]
    return historical_ds._sample_array(target)[:, 0]


def _recorded_rows(historical_ds, level, outcome, places, months, identity) -> Optional[pd.Series]:
    """The recorded deaths for `months`, summed to `level`, indexed (month_id, place).

    `months` is the list of recorded months to read (never empty). A place whose cells are all
    missing stays missing (`min_count=1`); it does not become 0. Places the forecast's geography
    does not know are left out, so a place is either served with its names or refused, never
    served nameless.

    The history is sorted by month, so the months are read as contiguous blocks instead of
    scanning every row of a decades-long table.
    """
    target = target_for(historical_ds, outcome)
    if target is None:
        raise ValueError(f"the history holds no recorded deaths for outcome {outcome!r}")
    geo = historical_ds.geo_metadata
    observed = _recorded_values(historical_ds, target)
    if len(observed) != len(geo):
        raise ValueError(
            f"recorded values ({len(observed)}) and their geography ({len(geo)}) differ in length"
        )
    index = geo.index
    # Read by position below (`levels[0]`, `codes[1]`), so the order is checked by name here.
    if list(index.names) != [_MONTH, _CELL]:
        raise ValueError(f"the history is indexed {list(index.names)}, not [{_MONTH!r}, {_CELL!r}]")
    if not index.is_monotonic_increasing:
        raise ValueError("the history's rows are not sorted by month; its months cannot be read as blocks")
    # Consecutive months are one block: decades of history read as one slice, not 400.
    runs, months = [], sorted(months)
    for m in months:
        if runs and m == runs[-1][1] + 1:
            runs[-1][1] = m
        else:
            runs.append([m, m])
    bounds = [index.slice_locs((first,), (last,)) for first, last in runs]
    if len(bounds) == 1:
        rows = slice(*bounds[0])   # read in place, no copy of the row positions
    else:
        rows = np.concatenate([np.arange(start, stop) for start, stop in bounds])
    month_ids = index.levels[0].to_numpy()[index.codes[0][rows]]
    known = identity.index.to_numpy(dtype=float)
    if level == "grid":
        # Decide per distinct cell (tens of thousands), then look each row's cell up: far cheaper
        # than testing every one of the history's rows against the list.
        cells = index.levels[1].to_numpy()
        allowed = np.isin(cells, known)
        if places is not None:
            allowed &= np.isin(cells, places)
        cell_codes = index.codes[1][rows]
        keep = allowed[cell_codes]
        place_ids = cells[cell_codes]
    else:
        source = schema.IDENTITY_SOURCE[PLACE_COLUMN[level]]
        place_ids = geo[source].to_numpy()[rows]
        keep = np.isin(place_ids, places) if places is not None else np.ones(len(place_ids), dtype=bool)
    observed = observed[rows]
    column = schema.actual_col(outcome)
    frame = pd.DataFrame(
        {_MONTH: month_ids[keep], PLACE_COLUMN[level]: place_ids[keep], column: observed[keep]}
    )
    summed = frame.groupby([_MONTH, PLACE_COLUMN[level]])[column].sum(min_count=1)
    if level != "grid":
        # After summing there is one row per region and month, so this check is cheap here.
        summed = summed[summed.index.get_level_values(1).isin(known)]
    return summed


def build_outcome_table(
    forecast_ds,
    historical_ds,
    level: str,
    outcome: str,
    places: Optional[list[int]] = None,
    months: Optional[list[int]] = None,
    identity: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """The table for one outcome at one level, for the places and months asked for.

    `places` and `months` of `None` mean all. Both are assumed known: the caller checks them
    against `place_identity` and the served months first, so a typo is refused, not answered
    with an empty table. A request reaching months before the forecast needs `historical_ds`
    holding the outcome; the caller checks that too (`needs_history`, `target_for`).
    Columns: `IDENTITY_COLUMNS[level]`, then `value_columns(outcome)`.
    `identity` is `place_identity(forecast_ds, level)`, passed when the caller already has it.
    """
    if identity is None:
        identity = place_identity(forecast_ds, level)
    forecast = _forecast_rows(forecast_ds, level, outcome, places, months, identity)

    recorded = None
    past = recorded_months(forecast_ds, historical_ds)
    if months is not None:
        past = [m for m in past if m in set(months)]
    if past:
        recorded = _recorded_rows(historical_ds, level, outcome, places, past, identity)

    parts = [p for p in (forecast, recorded) if p is not None]
    if parts:
        table = pd.concat(parts, axis=1, join="outer").sort_index()
    else:
        table = pd.DataFrame(index=pd.MultiIndex.from_arrays([[], []], names=[_MONTH, PLACE_COLUMN[level]]))

    table = table.reset_index()
    if level != "grid":
        # Both sources key admin-1 and country rows by the geography's float codes; partners
        # write and read them as whole numbers.
        table[PLACE_COLUMN[level]] = table[PLACE_COLUMN[level]].astype("Int64")
    table = table.join(identity, on=PLACE_COLUMN[level])
    table["month"] = [month_id_to_date(int(m)) for m in table[_MONTH]]
    for column in value_columns(outcome):
        table[column] = table[column].astype("float64") if column in table.columns else np.nan
    check_summaries(table, outcome)
    return table[IDENTITY_COLUMNS[level] + value_columns(outcome)]
