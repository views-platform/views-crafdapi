"""C-72: value- and metadata-plausibility checks for ingested forecast data.

Extracted from the dataset god-class (Phase 1 of #87) so the trust-boundary validation is
one small, independently testable module with a single reason to change. Schema-valid data
can still carry implausible values (NaN/Inf or negative fatalities; out-of-range
coordinates; malformed geo codes) — these fail loud before anything is cached or served.
"""

import numpy as np
import pandas as pd

# Geographic-metadata plausibility bounds.
_COORD_BOUNDS = (("pg_xcoord", -180.0, 180.0), ("pg_ycoord", -90.0, 90.0))

# The VIEWS sentinel for "no country" — ocean / ungoverned PRIO-GRID cells. It is a
# missing-metadata marker, NOT a malformed code: real historical data carries ~27k such
# cells (country_iso_a3 == "-99"), and they are dropped downstream at aggregation (C-146).
# Plausibility must treat it like null (the docstring's "missing metadata is a separate
# concern"), else it rejects the entire real dataset over legitimately-uncoded cells.
_NO_COUNTRY_SENTINEL = "-99"


def assert_prediction_samples_plausible(var_name: str, samples) -> None:
    """Every posterior sample of one prediction variable must be finite and non-negative.

    ``samples``: an ``(N, S)`` (or ``(S,)``) float array. Raises ``ValueError`` on violation.
    """
    arr = np.asarray(samples, dtype=np.float64)
    if not np.isfinite(arr).all():
        n_bad = int((~np.isfinite(arr)).sum())
        raise ValueError(
            f"{var_name}: {n_bad} non-finite prediction sample(s) (NaN/Inf) — implausible (C-72)"
        )
    if (arr < 0).any():
        raise ValueError(
            f"{var_name}: negative prediction sample(s) (min {float(arr.min()):.4g}) — "
            f"fatality predictions cannot be negative (C-72)"
        )


def assert_geo_metadata_plausible(geo_metadata: pd.DataFrame) -> None:
    """Geographic metadata plausibility: PRIO-GRID coordinate ranges and 3-letter ISO3 shape.
    Operates on non-null values only (missing metadata is a separate concern). Raises
    ``ValueError`` on violation.

    GAUL admin codes are deliberately NOT range-checked: GAUL assigns **negative** unit codes
    to disputed/special territories (e.g. codes -3727…-3730 in the global ``land_gaul`` grid),
    so negativity is legitimate, not corruption (#287 follow-up — the Africa-only run never
    included these cells; the global run does). There is no meaningful upper bound to assert,
    and non-numeric codes coerce to null (handled as missing, per above).
    """
    gm = geo_metadata

    for col, lo, hi in _COORD_BOUNDS:
        vals = pd.to_numeric(gm[col], errors="coerce")
        bad = vals.notna() & ((vals < lo) | (vals > hi))
        if bad.any():
            raise ValueError(
                f"{col}: {int(bad.sum())} value(s) outside [{lo}, {hi}] — "
                f"implausible coordinate (C-72)"
            )

    iso = gm["country_iso_a3"].dropna().astype(str)
    iso = iso[iso != _NO_COUNTRY_SENTINEL]  # the "no country" sentinel is missing, not malformed
    bad_iso = iso[~iso.str.fullmatch(r"[A-Za-z]{3}")]
    if len(bad_iso) > 0:
        sample = bad_iso.unique()[:5].tolist()
        raise ValueError(
            f"country_iso_a3: {len(bad_iso)} value(s) not a 3-letter code "
            f"(e.g. {sample}) — implausible (C-72)"
        )

    # GAUL admin codes intentionally not range-checked — negative codes are valid disputed-area
    # units (see docstring). #287 follow-up: a non-negative assert here refused the whole global
    # run over 6 legitimately-negative cells.
