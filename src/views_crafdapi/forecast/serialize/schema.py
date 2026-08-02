"""The single source of truth for the ADR-025 FAO output schema.

One module owns everything about *what the served columns are called*: the credible masses,
the internal-target → consumer-series (``sb``/``ns``/``os``) map, the identity-column mapping
(GAUL admin-1/admin-0 + ISO3), and the per-series + full 33-column name contract. Both the
JSON serializer (epic #222 / S4) and the admin-1 bulk parquet writer (#222 / S6) build their
columns from here, so a column name lives in exactly one place (Common Closure) — the old hazard
of the name contract being spelled in both ``json_contract.py`` and an inline handler dict is
retired.

Pure names + mappings: no pandas, no numpy — safe to import from anywhere.
Consumer-facing names carry no ``ged_``/``lr_``/``ln_``/``pred_`` prefix (ADR-025 §3); all
values are raw counts (ADR-024).
"""

from __future__ import annotations

# --- Credible masses (ADR-025 §4): three nested HDIs; mass encoded as a percent in the name. ---
# This is THE definition the reduction (`estimator.collapse`) and the column names share, so the
# CollapseResult hdi keys and the `s_hdi{pct}` column names can never drift apart.
MASSES: tuple[float, ...] = (0.50, 0.90, 0.95)

_PCT_BY_MASS: dict[float, int] = {m: round(m * 100) for m in MASSES}


def mass_pct(mass: float) -> int:
    """The integer percent used in column names for a served credible mass (``0.90`` → ``90``)."""
    try:
        return _PCT_BY_MASS[mass]
    except KeyError:
        raise ValueError(f"unknown credible mass {mass!r}; served masses are {MASSES}") from None


# --- Exceedance thresholds (ADR-034): CRAF'd's new summary statistic. -------------------------
# ``P(Y > c)`` = the posterior probability that a series exceeds ``c`` fatalities, one per
# threshold. Like ``MASSES``, this is THE single definition the reduction (`estimator.collapse`)
# and the ``{series}_p_gt{c}`` column names share, so the CollapseResult exceedance keys and the
# served column names can never drift. A DECLARED constant, not a runtime override: a change is
# one reviewed, git-historied line that auto-propagates to every column name + golden (easy) and
# is a deliberate served-contract amendment (safe) — a served set that varied with the
# environment is the silent-contract-drift hazard ADR-025 exists to prevent. Placeholders per
# ADR-034 §3 (operator, 2026-08-02) pending CRAF'd's real numbers.
EXCEEDANCE_THRESHOLDS: tuple[int, ...] = (25, 100, 1000)


# --- Consumer series stems (ADR-025 §3): the bare sb/ns/os, no internal prefix. ---
SERIES: tuple[str, ...] = ("sb", "ns", "os")


def series_of(var_name: str) -> str:
    """Map an internal target / ``pred_*`` variable name to its consumer series stem.

    Finds the single ``sb``/``ns``/``os`` token, ignoring any ``ged_``/``lr_``/``ln_``/``pred_``/
    ``best`` decoration (``pred_lr_ged_sb`` → ``sb``, ``pred_ln_ns_best`` → ``ns``). Fail-loud
    (ADR-003/008) when a name carries no series or an ambiguous one — a new upstream naming is a
    contract event to surface, never a silent guess.
    """
    tokens = set(var_name.lower().split("_"))
    found = [s for s in SERIES if s in tokens]
    if len(found) != 1:
        raise ValueError(
            f"cannot map {var_name!r} to a unique series stem {SERIES}: matched {found}"
        )
    return found[0]


# --- Identity columns (ADR-025 §4): consumer name ← internal GAUL metadata column. ---
# `month_id` passes through unchanged (the time id). Country is GAUL admin-0, NOT UN M49.
IDENTITY_SOURCE: dict[str, str] = {
    "admin1_code": "admin1_gaul1_code",
    "admin1_name": "admin1_gaul1_name",
    "country_code": "admin1_gaul0_code",
    "country_name": "admin1_gaul0_name",
    "country_iso3": "country_iso_a3",
}

# The full identity block, in ADR-025 §4 order (6 columns).
IDENTITY_COLUMNS: tuple[str, ...] = (
    "month_id",
    "admin1_code",
    "admin1_name",
    "country_code",
    "country_name",
    "country_iso3",
)


# --- Per-series value columns (ADR-025 §4): 9 per series. ---
def map_col(series: str) -> str:
    return f"{series}_map"


def hdi_col(series: str, mass: float, bound: str) -> str:
    """``{series}_hdi{pct}_{lower|upper}`` (e.g. ``sb_hdi90_lower``)."""
    if bound not in ("lower", "upper"):
        raise ValueError(f"bound must be 'lower' or 'upper', got {bound!r}")
    return f"{series}_hdi{mass_pct(mass)}_{bound}"


def exceed_col(series: str, threshold: int) -> str:
    """``{series}_p_gt{c}`` — the served ``P(series > c)`` exceedance column (ADR-034 §3).

    ``c`` must be one of :data:`EXCEEDANCE_THRESHOLDS`; a stray threshold is a contract event
    (fail-loud, ADR-003/008), never a silently-named column.
    """
    if threshold not in EXCEEDANCE_THRESHOLDS:
        raise ValueError(
            f"unknown exceedance threshold {threshold!r}; served thresholds are "
            f"{EXCEEDANCE_THRESHOLDS}"
        )
    return f"{series}_p_gt{threshold}"


def severe_col(series: str) -> str:
    return f"{series}_severe_scenario"


def bimodality_col(series: str) -> str:
    """``{series}_bimodality_flag`` — the 0/1 secondary-mode flag (ADR-025 §A.3)."""
    return f"{series}_bimodality_flag"


def actual_col(series: str) -> str:
    return f"{series}_actual"


def series_value_columns(series: str) -> list[str]:
    """The 10 ADR-025 value columns for one series, in canonical order:
    ``map · hdi50_lower · hdi50_upper · hdi90_lower · hdi90_upper · hdi95_lower · hdi95_upper
    · severe_scenario · bimodality_flag · actual``.
    """
    cols = [map_col(series)]
    for mass in MASSES:
        cols += [hdi_col(series, mass, "lower"), hdi_col(series, mass, "upper")]
    cols += [severe_col(series), bimodality_col(series), actual_col(series)]
    return cols


def bulk_columns() -> list[str]:
    """The full ADR-025 admin-1 bulk layout: 6 identity + 3 series × 10 = **36 columns**
    (the 33-column base + the per-series `bimodality_flag`, ADR-025 §A.3 / C.1)."""
    cols = list(IDENTITY_COLUMNS)
    for series in SERIES:
        cols += series_value_columns(series)
    return cols


# --- consumer rename (epic #222 / S4, "disciplined B") ---------------------------------------
# The forecast reduction emits quantity columns keyed by the internal var (`{var}_map`,
# `{var}_hdi90_lower`, …). The consumer-facing `sb/ns/os` rename is a *boundary* transform:
# structured (rebuild `{series}_{quantity}` from the pair, never a substring swap) and **scoped
# to quantity columns** — identity, geo, and the historical `actual` carry no forecast-quantity
# suffix, so they pass through and the fail-loud `series_of` gate never trips on them.


def _served_quantities() -> list[str]:
    """The forecast-quantity suffixes a value column can carry (no `actual` — not a reduction
    output). Ordered longest-first so suffix matching is unambiguous."""
    q = ["severe_scenario", "bimodality_flag"]
    for mass in MASSES:
        q += [f"hdi{mass_pct(mass)}_lower", f"hdi{mass_pct(mass)}_upper"]
    q.append("map")
    return q


def consumer_column(column: str) -> str:
    """Rename one var-keyed forecast-quantity column `{var}_{quantity}` → `{series}_{quantity}`.

    Returns ``column`` unchanged when it carries no known forecast-quantity suffix (identity,
    geo, and `{series}_actual` all pass through). Structured — rebuilds from the `(var, quantity)`
    pair. Fail-loud (via `series_of`) only on a genuine quantity column whose var has no series.
    """
    for quantity in _served_quantities():
        suffix = "_" + quantity
        if column.endswith(suffix):
            var = column[: -len(suffix)]
            return f"{series_of(var)}_{quantity}"
    return column
