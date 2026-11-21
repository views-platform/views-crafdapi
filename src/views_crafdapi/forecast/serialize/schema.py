"""The served column names: the one place they are spelled.

The summary columns are the set decided on 2026-11-17 (#204, data dictionary
`docs/api/data_dictionary.md`). They replace the earlier set (the most likely value, three
credible intervals and a two-peak flag), which measured at 0 or empty in almost every row of a
real forecast.

Per outcome `s` (`sb`, `ns`, `os`), computed from the draws of one place and month:

- `s_p_gt0`, `s_p_gt25`, `s_p_gt100`, `s_p_gt500`, `s_p_gt1000`: the chance of more than that
  many deaths;
- `s_mean`: the average over all draws;
- `s_mean_if_any`, `s_median_if_any`: the average and the middle value over the draws with at
  least one death;
- `s_severe_scenario`: the average of the worst 5% of draws, empty below 20 usable draws;
- `s_n_draws`: how many usable draws the row's summaries come from (served as `n_draws` in a
  one-outcome table);

and `s_actual`, the recorded deaths, which is not computed from draws.

Pure names: no pandas, no numpy, safe to import from anywhere. Served names carry no
`ged_`/`lr_`/`ln_`/`pred_` prefix; every number of deaths is a count (ADR-024).
"""

from __future__ import annotations

# The exceedance thresholds: the edges of the bins VIEWS scores monthly forecasts with in its
# Prediction Challenge (Hegre, Vesco, Colaresi et al. 2025, JPR). 0 gives the chance of any deaths.
EXCEEDANCE_THRESHOLDS: tuple[int, ...] = (0, 25, 100, 500, 1000)

# The severe case is the mean of the worst `SEVERE_TAIL` of the draws. With fewer than
# `SEVERE_MIN_DRAWS` usable draws the worst 5% would be a single draw (the maximum), so the
# column is empty instead. hydranet has reported runs with 16 draws.
SEVERE_TAIL = 0.05
SEVERE_MIN_DRAWS = 20

# The quantities computed from the draws, in served order. `n_draws` is last.
SUMMARY_QUANTITIES: tuple[str, ...] = (
    *(f"p_gt{c}" for c in EXCEEDANCE_THRESHOLDS),
    "mean",
    "mean_if_any",
    "median_if_any",
    "severe_scenario",
    "n_draws",
)

# Outcome codes: the bare sb/ns/os.
SERIES: tuple[str, ...] = ("sb", "ns", "os")


def series_of(var_name: str) -> str:
    """Map an internal target name to its outcome code.

    Finds the single ``sb``/``ns``/``os`` token, ignoring ``ged_``/``lr_``/``ln_``/``pred_``/
    ``best`` decoration (``pred_lr_ged_sb`` → ``sb``). Fails loudly when a name carries no
    outcome or two: a new upstream naming is something to look at, never a silent guess.
    """
    tokens = set(var_name.lower().split("_"))
    found = [s for s in SERIES if s in tokens]
    if len(found) != 1:
        raise ValueError(
            f"cannot map {var_name!r} to a unique series stem {SERIES}: matched {found}"
        )
    return found[0]


# The place's id and names: served name <- the geography's column (#205). Country is GAUL admin 0,
# not UN M49. Which of them a level serves is in `outcome_table.PLACE_COLUMNS`.
IDENTITY_SOURCE: dict[str, str] = {
    "admin1_code": "admin1_gaul1_code",
    "admin1_name": "admin1_gaul1_name",
    "country_code": "admin1_gaul0_code",
    "country_name": "admin1_gaul0_name",
    "country_iso3": "country_iso_a3",
}


def summary_col(series: str, quantity: str) -> str:
    """``{series}_{quantity}``, such as ``sb_p_gt25``. ``quantity`` must be a served one."""
    if quantity not in SUMMARY_QUANTITIES:
        raise ValueError(f"unknown summary {quantity!r}; served: {SUMMARY_QUANTITIES}")
    return f"{series}_{quantity}"


def exceed_col(series: str, threshold: int) -> str:
    """``{series}_p_gt{c}``: the chance of more than ``c`` deaths."""
    return summary_col(series, f"p_gt{threshold}")


def actual_col(series: str) -> str:
    return f"{series}_actual"


def series_value_columns(series: str) -> list[str]:
    """The value columns for one outcome: the summaries, the recorded deaths, then
    ``{series}_n_draws`` (served as ``n_draws``, `outcome_table.value_columns`)."""
    summaries = [summary_col(series, q) for q in SUMMARY_QUANTITIES if q != "n_draws"]
    return summaries + [actual_col(series), summary_col(series, "n_draws")]


# --- The served rename -----------------------------------------------------------------------
# The reduction names its columns by the internal target (`{var}_mean`, …). The rename to the
# outcome code is applied where the answer is served, rebuilt from the (var, quantity) pair,
# never a substring swap, and only to summary columns: identity, geography and `actual` carry
# no summary suffix and pass through.


def _summary_suffixes() -> list[str]:
    """The summary suffixes, longest first, so `p_gt1000` is tried before `p_gt100` and
    `mean_if_any` before `mean`."""
    return sorted(SUMMARY_QUANTITIES, key=len, reverse=True)


def consumer_column(column: str) -> str:
    """Rename one summary column `{var}_{quantity}` → `{series}_{quantity}`.

    Returns ``column`` unchanged when it carries no summary suffix. Fails loudly (via
    `series_of`) only on a summary column whose target has no outcome code.
    """
    for quantity in _summary_suffixes():
        suffix = "_" + quantity
        if column.endswith(suffix):
            var = column[: -len(suffix)]
            return f"{series_of(var)}_{quantity}"
    return column
