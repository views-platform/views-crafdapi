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


# Rows checked at a time. The disk-cache read runs this over a memmapped `(N, S)` float32
# block of ~1.2 GB per target (C-277), so it must never materialise the whole block, let alone
# a float64 copy of it. 65,536 rows x 128 samples is ~32 MB of float32 per chunk.
_CHUNK_ROWS = 65_536


def assert_prediction_samples_plausible(var_name: str, samples, chunk_rows: int = _CHUNK_ROWS) -> None:
    """Every posterior sample of one prediction variable must be finite and non-negative.

    ``samples``: an ``(N, S)``, ``(S,)`` or scalar array, possibly a memmap; empty passes. Checked ``chunk_rows`` rows
    at a time, in its own float dtype (a non-float input is cast per chunk). Raises
    ``ValueError`` on violation.

    Measured on the delivered run (3 targets x 2,330,712 x 128 float32, 3.5 GB, #168): 2.3 s from
    a cold page cache, 0.8 s warm, no anonymous memory beyond one chunk.
    """
    arr = np.atleast_1d(np.asarray(samples))
    n_nonfinite, minimum = 0, np.inf
    for start in range(0, len(arr), chunk_rows):
        block = arr[start:start + chunk_rows]
        if block.dtype.kind != "f":
            block = block.astype(np.float64)
        finite = np.isfinite(block)
        if not finite.all():
            n_nonfinite += int((~finite).sum())
        elif n_nonfinite == 0 and block.size:
            minimum = min(minimum, float(block.min()))
    if n_nonfinite:
        raise ValueError(
            f"{var_name}: {n_nonfinite} non-finite prediction sample(s) (NaN/Inf) — implausible (C-72)"
        )
    if minimum < 0:
        raise ValueError(
            f"{var_name}: negative prediction sample(s) (min {minimum:.4g}) — "
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

    iso_col = gm["country_iso_a3"]
    if isinstance(iso_col.dtype, pd.CategoricalDtype):
        # C-263: the check is over the SET of distinct codes, so run it on the categories —
        # a few hundred labels — instead of expanding the categorical back to one Python string
        # per row. `.astype(str)` on 28.4M rows materialised the object block this column exists
        # to avoid. The reported count is still the row count, so the error message is unchanged
        # for any input that fails; only the work done to reach it differs.
        cats = pd.Series(iso_col.cat.categories.astype(str))
        bad_cats = cats[(cats != _NO_COUNTRY_SENTINEL) & ~cats.str.fullmatch(r"[A-Za-z]{3}")]
        if len(bad_cats) > 0:
            n_bad = int(iso_col.isin(bad_cats).sum())
            raise ValueError(
                f"country_iso_a3: {n_bad} value(s) not a 3-letter code "
                f"(e.g. {bad_cats.tolist()[:5]}) — implausible (C-72)"
            )
        return

    iso = iso_col.dropna().astype(str)
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
