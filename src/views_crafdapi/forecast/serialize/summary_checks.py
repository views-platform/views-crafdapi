"""Checks a served table's summary columns before it leaves the service (#204).

Two relations hold for every row by construction, whatever the forecast says. A row breaking
one means the reduction or the table assembly is wrong, so the table is refused rather than
served:

- the chances fall as the threshold rises: `p_gt0 >= p_gt25 >= p_gt100 >= p_gt500 >= p_gt1000`;
- the average is the chance of any deaths times the average if any:
  `mean = p_gt0 x mean_if_any`, wherever both are defined (draws are never negative; ingest
  refuses negative draws, C-72).

Empty values are skipped, never read as 0. `n_draws`, a count, takes part in neither check: a
count says nothing about whether a month has data.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from views_crafdapi.forecast.serialize import schema


def check_summaries(table: pd.DataFrame, series: str) -> None:
    """Raise ValueError naming the first broken relation for outcome `series`. Columns the
    table does not carry are skipped, so a table cut to some columns is checked on those."""
    chances = [schema.exceed_col(series, c) for c in schema.EXCEEDANCE_THRESHOLDS]
    present = [c for c in chances if c in table.columns]
    for higher, lower in zip(present, present[1:]):
        a, b = table[higher].to_numpy(dtype=float), table[lower].to_numpy(dtype=float)
        broken = ~np.isnan(a) & ~np.isnan(b) & (b > a + 1e-12)
        if broken.any():
            raise ValueError(f"{lower} is above {higher} in {int(broken.sum())} rows")

    needed = [schema.summary_col(series, q) for q in ("mean", "p_gt0", "mean_if_any")]
    if all(c in table.columns for c in needed):
        mean, p_any, if_any = (table[c].to_numpy(dtype=float) for c in needed)
        both = ~np.isnan(mean) & ~np.isnan(p_any) & ~np.isnan(if_any)
        broken = both & ~np.isclose(mean, p_any * if_any, rtol=1e-9, atol=1e-9)
        if broken.any():
            raise ValueError(f"{needed[0]} is not {needed[1]} x {needed[2]} in {int(broken.sum())} rows")
