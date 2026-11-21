"""The bulk file for one outcome at one level: the whole table a slice draws from, as Parquet.

The service specification (docs/SERVICE_SPECIFICATION.md) makes the bulk download the main way
in: one ready-made file per outcome and level (#203). The file holds exactly what a slice with no
options returns (`outcome_table.build_outcome_table`), so the two cannot disagree:

- every place at the level, every month: the recorded months, then the forecast months;
- a recorded month's row has the recorded deaths (`{outcome}_actual`) and empty summary columns;
  a forecast month's row has the summary columns and an empty `{outcome}_actual`.

The table is built and written a few months at a time. At grid level it has one row per cell
and month, decades of history included: tens of millions of rows, which do not fit in memory
as one pandas table (measured 2026-11-21: past 11 GB before it was stopped).
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import pyarrow as pa
import pyarrow.parquet as pq

from views_crafdapi.forecast.serialize import outcome_table

# Months built and written at a time. At grid level a month is one row per cell (about 65,000),
# so a year is under a million rows.
MONTHS_PER_PART = 12


def bulk_months(forecast_ds, historical_ds) -> list[int]:
    """Every month in the file: the recorded months before the forecast, then the forecast's."""
    return sorted(
        set(outcome_table.forecast_months(forecast_ds))
        | set(outcome_table.recorded_months(forecast_ds, historical_ds))
    )


def write_bulk_file(
    path: Union[str, Path], forecast_ds, historical_ds, level: str, outcome: str
) -> Path:
    """Write the table for `outcome` at `level` to `path` as Parquet. Returns the path.

    The caller checks first that `historical_ds` holds the outcome: a file whose recorded deaths
    are empty would read as "nothing recorded" (the slice refuses in the same case).
    """
    path = Path(path)
    identity = outcome_table.place_identity(forecast_ds, level)
    months = bulk_months(forecast_ds, historical_ds)
    if not months:
        raise ValueError("no months to write: the forecast holds none")
    writer = None
    try:
        for start in range(0, len(months), MONTHS_PER_PART):
            part = outcome_table.build_outcome_table(
                forecast_ds, historical_ds, level, outcome,
                months=months[start:start + MONTHS_PER_PART], identity=identity,
            )
            # The first part fixes the column types; every later part is written with them, so
            # a part where a column happens to be all empty cannot change its type.
            table = pa.Table.from_pandas(
                part, preserve_index=False, schema=writer.schema if writer else None
            )
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema)
            writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()
    return path
