"""The served summaries of a forecast's draws (#204, data dictionary).

For each row (one place and month), from its draws:

- `n_draws`: how many draws are usable (finite numbers). Every other summary uses these only.
  A draw that is not a finite number is left out, never read as 0. Ingest refuses such draws
  (`ingestion/plausibility.py`, C-72), so this is a second line, not the first.
- `p_gt{c}`: the share of usable draws above `c`, for each threshold in
  `schema.EXCEEDANCE_THRESHOLDS`.
- `mean`: the average of the usable draws.
- `mean_if_any`, `median_if_any`: the average and the middle value of the usable draws above 0;
  empty when no draw is above 0.
- `severe_scenario`: the average of the largest `ceil(SEVERE_TAIL x n_draws)` usable draws;
  empty below `SEVERE_MIN_DRAWS` usable draws, where it would be a single draw.

A row with no usable draws is empty in every summary and 0 in `n_draws`. All arithmetic is
float64. Numpy only: no pandas, no geography.
"""

from __future__ import annotations

import numpy as np

from views_crafdapi.forecast.serialize import schema


def summarize_draws(values) -> dict[str, np.ndarray]:
    """``values``: ``(N, S)`` draws, or ``(S,)`` for one row. Returns ``{quantity: (N,)}`` for
    every quantity in `schema.SUMMARY_QUANTITIES`, float64.

    Memory: up to three ``(N, S)`` float64 arrays live at once (the input as float64, the sorted
    draws, their running sums); a month of the whole grid at 128 draws is about 66 MB each.
    """
    x = np.asarray(values, dtype=np.float64)
    if x.ndim == 1:
        x = x.reshape(1, -1)
    n_rows, width = x.shape
    rows = np.arange(n_rows)
    # Usable draws ascending, the rest (NaN) after them: row r's usable draws are at [0, n[r]).
    ordered = np.where(np.isfinite(x), x, np.nan)
    ordered.sort(axis=1)
    n = width - np.isnan(ordered).sum(axis=1)

    out: dict[str, np.ndarray] = {}
    for c in schema.EXCEEDANCE_THRESHOLDS:
        out[f"p_gt{c}"] = (ordered > c).sum(axis=1)          # NaN > c is False
    n_any = out["p_gt0"].copy()                              # draws above 0: the last n_any usable

    # `running[r, i]` is the sum of row r's i + 1 smallest usable draws; `below(i)` the sum of
    # the i smallest (0 for i = 0).
    running = np.nan_to_num(ordered)
    np.cumsum(running, axis=1, out=running)

    def below(i):
        return np.where(i > 0, running[rows, np.clip(i - 1, 0, max(width - 1, 0))], 0.0) if width else np.zeros(n_rows)

    with np.errstate(invalid="ignore", divide="ignore"):
        for c in schema.EXCEEDANCE_THRESHOLDS:
            out[f"p_gt{c}"] = np.where(n > 0, out[f"p_gt{c}"] / np.maximum(n, 1), np.nan)
        total = below(n)
        out["mean"] = np.where(n > 0, total / np.maximum(n, 1), np.nan)
        first_any = n - n_any
        out["mean_if_any"] = np.where(n_any > 0, (total - below(first_any)) / np.maximum(n_any, 1), np.nan)
        if width:
            lower_middle = np.clip(first_any + (n_any - 1) // 2, 0, width - 1)
            upper_middle = np.clip(first_any + n_any // 2, 0, width - 1)
            middle = (ordered[rows, lower_middle] + ordered[rows, upper_middle]) / 2
        else:
            middle = np.full(n_rows, np.nan)
        out["median_if_any"] = np.where(n_any > 0, middle, np.nan)
        worst = np.ceil(schema.SEVERE_TAIL * n).astype(np.int64)
        severe = (total - below(n - worst)) / np.maximum(worst, 1)
        out["severe_scenario"] = np.where(n >= schema.SEVERE_MIN_DRAWS, severe, np.nan)

    out["n_draws"] = n.astype(np.float64)
    return {q: np.asarray(out[q], dtype=np.float64) for q in schema.SUMMARY_QUANTITIES}
