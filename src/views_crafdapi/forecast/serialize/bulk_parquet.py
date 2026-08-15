"""The ADR-025 admin-1 bulk artifact: one wide parquet, everything in one file.

FAO loads this in one step instead of paging the JSON API. It is the **same** served numbers
as the API, packaged wide at admin-1 (GAUL level-1) grain: one row per `(month_id, admin1_code)`,
6 identity columns + per series `s ∈ {sb, ns, os}` the 13 value columns — the ADR-025 base
+ per-series `bimodality_flag` + the three ADR-034 `p_gt{c}` exceedance columns = **45 total**.

- Forecast columns (`s_map`, `s_hdi{50,90,95}_{lower,upper}`, `s_severe_scenario`,
  `s_p_gt{25,100,1000}`) come from the
  forecast dataset's admin-1 aggregate reduction (the same `calculate_hdi_map` the API uses).
- `s_actual` is the **historical observed** count, summed to admin-1 and joined on
  `(month_id, admin1_code)`; `NaN` where there is no observed value (the forecast horizon).
- Identity columns are the consumer-facing GAUL names (admin-0 country, not M49), from the
  9-column metadata via `schema.IDENTITY_SOURCE`.

Most values are raw fatality counts (ADR-024), but **not all**: the three ADR-034 §3 exceedance columns per series (`s_p_gt25/100/1000`) are probabilities in [0, 1], and `s_bimodality_flag` is a 0/1 flag. Written via pyarrow (pandas not required by the
engine, ADR-030 spirit — a thin DataFrame→parquet is acceptable here in `serialize/`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import pandas as pd

from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.serialize.json_contract import to_consumer_columns

# The admin-1 identity source columns in the 9-column GAUL metadata contract.
_GAUL1_CODE = "admin1_gaul1_code"
_META_FOR_IDENTITY = (
    "admin1_gaul1_code",
    "admin1_gaul1_name",
    "admin1_gaul0_code",
    "admin1_gaul0_name",
    "country_iso_a3",
)


def _identity_table(geo_metadata: pd.DataFrame) -> pd.DataFrame:
    """One row per admin-1 unit with the 5 consumer identity columns (+ the join key).

    Derived from the per-cell geo-metadata: admin-1 is the unit, admin-0 is its country.
    Fail-loud if an admin-1 code maps to more than one country/name (a broken hierarchy).
    """
    cols = [c for c in _META_FOR_IDENTITY if c in geo_metadata.columns]
    ident = geo_metadata[cols].drop_duplicates()
    dupes = ident[_GAUL1_CODE].duplicated(keep=False)
    if dupes.any():
        bad = sorted(ident.loc[dupes, _GAUL1_CODE].unique())
        raise ValueError(f"admin-1 codes map to multiple identities (broken hierarchy): {bad}")
    return ident.rename(
        columns={src: consumer for consumer, src in schema.IDENTITY_SOURCE.items()}
    ).set_index("admin1_code")


def _actual_by_admin1(historical_ds) -> pd.DataFrame:
    """Historical observed counts summed to `(month_id, admin1_code)`, one `s_actual` column
    per series. Observed data is one value per cell (`S=1`); fatality counts are additive, so
    the admin-1 value is the sum over its cells."""
    geo = historical_ds.geo_metadata
    months = geo.index.get_level_values("month_id").to_numpy()
    admin1 = geo[_GAUL1_CODE].to_numpy()
    frames = []
    for target in historical_ds.targets:
        series = schema.series_of(target)
        observed = historical_ds._sample_array(target)[:, 0]  # (N,) single observed value/cell
        col = schema.actual_col(series)
        s = pd.DataFrame({"month_id": months, "admin1_code": admin1, col: observed})
        frames.append(s.groupby(["month_id", "admin1_code"])[col].sum())
    return pd.concat(frames, axis=1)  # indexed by (month_id, admin1_code)


def build_bulk_table(forecast_ds, historical_ds=None) -> pd.DataFrame:
    """Assemble the 45-column admin-1 bulk table (ADR-025 base + per-series bimodality_flag
    + the three ADR-034 `p_gt{c}` exceedance columns per series).

    ``forecast_ds`` / ``historical_ds`` are `ForecastDataset`s (forecast samples / observed
    counts). Returns a DataFrame with `schema.bulk_columns()` in order; `s_actual` is `NaN`
    when ``historical_ds`` is absent or has no observed value for that unit-month.
    """
    # Forecast value columns at admin-1 (gaul1), consumer-renamed (sb/ns/os).
    fc = forecast_ds.calculate_hdi_map(aggregate=True, level="gaul1", with_metadata=False)
    fc = to_consumer_columns(fc)  # {var}_* → {series}_* (fail-loud on non-conforming targets)
    # Fail loud if two forecast targets map to the same series (e.g. a `_best` + `_prob` pair):
    # a silent column collision would mislabel one series' values (wire run-0 has clean targets).
    if fc.columns.duplicated().any():
        dup = sorted(fc.columns[fc.columns.duplicated()].unique())
        raise ValueError(f"multiple forecast targets map to the same series: {dup}")
    fc.index = fc.index.set_names(["month_id", "admin1_code"])

    # Identity block (6 consumer columns), joined on admin1_code.
    ident = _identity_table(forecast_ds.geo_metadata)
    table = fc.join(ident, on="admin1_code")

    # Historical actuals, joined on the (month_id, admin1_code) index; NaN elsewhere.
    if historical_ds is not None:
        actuals = _actual_by_admin1(historical_ds)
        table = table.join(actuals)

    table = table.reset_index()
    # Any schema column not produced (e.g. s_actual with no history) is NaN.
    for col in schema.bulk_columns():
        if col not in table.columns:
            table[col] = pd.NA
    return table[schema.bulk_columns()]


def write_bulk_parquet(
    path: Union[str, Path], forecast_ds, historical_ds=None
) -> Path:
    """Write the ADR-025 bulk table to ``path`` as parquet. Returns the path."""
    path = Path(path)
    build_bulk_table(forecast_ds, historical_ds).to_parquet(path, index=False)
    return path
