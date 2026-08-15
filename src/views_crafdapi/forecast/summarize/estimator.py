"""Point + interval estimation over posterior samples, via the views-frames tower estimator.

Extracted from `_ViewsDataset._tower_collapse` (Phase 2 of #87 / #89). MAP comes from
`views_frames_summarize.tower_point` (median of the narrowest canonical HDI floor — robust to
right-skew and minority duplicate draws) and the interval from `hdi_tower` (HDI at the
requested mass, pinned to the nearest canonical floor). This replaced the prior hand-rolled
histogram-mode MAP + shortest-interval HDI estimator, removed in the ADR-025 output-schema
epic (#222 / S1) — the tower is now the sole estimator. Returns bare
index-aligned numpy arrays (views-frames
ADR-017) — the caller holds the index and re-attaches geography downstream.
"""

import numpy as np
import views_frames_summarize as vfs

from views_crafdapi.forecast.frames.builder import build_prediction_frame
from views_crafdapi.forecast.summarize.result import CollapseResult
from views_crafdapi.forecast.summarize.severe import expected_shortfall


def tower_collapse(values, mass: float, enforce_non_negative: bool = False):
    """Collapse posterior samples to ``(hdi_lower, hdi_upper, map)``, each shape ``(N,)``.

    Parameters
    ----------
    values : array-like, shape ``(N, S)`` or ``(S,)``
        Posterior samples per row (a 1-D input is treated as a single row).
    mass : float
        Credible mass for the HDI.
    enforce_non_negative : bool
        Clip the MAP to ``>= 0`` (HDI bounds are left untouched, matching the legacy contract).

    Notes
    -----
    All-NaN rows yield NaN in every output. Non-finite values in otherwise-valid rows are
    zeroed before the collapse (the estimator is defined on finite samples).
    """
    flat = np.asarray(values, dtype=np.float32)
    if flat.ndim == 1:
        flat = flat.reshape(1, -1)
    n = flat.shape[0]
    lower = np.full(n, np.nan, dtype=np.float64)
    upper = np.full(n, np.nan, dtype=np.float64)
    map_vals = np.full(n, np.nan, dtype=np.float64)

    valid = ~np.isnan(flat).all(axis=1)
    if valid.any():
        v = np.nan_to_num(flat[valid], nan=0.0, posinf=0.0, neginf=0.0)
        # Per-row collapse: identifiers are positional (the estimator is per-row); real
        # geography is injected only at cross-level aggregation (ADR-014).
        frame = build_prediction_frame(
            v, time=np.zeros(v.shape[0], dtype=np.int64), unit=np.arange(v.shape[0])
        )
        tp = vfs.tower_point(frame)
        m = np.asarray(getattr(tp, "values", tp)).reshape(-1)
        h = np.asarray(vfs.hdi_tower(frame, masses=(mass,))).reshape(-1, 2)
        if enforce_non_negative:
            m = np.maximum(m, 0.0)
        map_vals[valid] = m
        lower[valid] = h[:, 0]
        upper[valid] = h[:, 1]
    return lower, upper, map_vals


def collapse(
    values,
    masses=(0.5, 0.9, 0.95),
    tail=0.05,
    enforce_non_negative=False,
    thresholds=(),
) -> CollapseResult:
    """Full posterior collapse → ``CollapseResult`` (MAP + nested HDIs at ``masses`` + severe).

    The ADR-025 serving reduction, in raw-count space (ADR-024). MAP and the HDIs are the
    order-statistic tower estimators (``tower_point`` / ``hdi_tower``, multi-mass native);
    ``severe`` is `expected_shortfall` (worst-``tail`` mean, float64 arithmetic — ADR-030 §6).
    A superset of `tower_collapse` (the single-mass tuple form the serving/aggregate paths still
    call); those callers migrate onto this in epic #222 / S4.

    ``values``: ``(N, S)`` or ``(S,)`` raw samples (a 1-D input is a single row). Returns a
    `CollapseResult` whose arrays are ``(N,)`` (``hdi[mass]`` is ``(N, 2)``); all-NaN rows are
    ``NaN`` throughout. ``enforce_non_negative`` clips the MAP only (HDI/severe untouched,
    matching `tower_collapse`).

    ``thresholds`` (ADR-034 §3): credible exceedance cutpoints, in the same raw-count units as
    ``values``. When given, the result carries ``exceedance = {c: (N,)}`` where each entry is the
    posterior probability ``P(Y > c)`` (strict ``>``, empirical survival fraction over the sample
    axis, via ``views_frames_summarize.exceedance``). Empty by default → no exceedance computed,
    so existing HDI/MAP callers are unchanged. All-NaN rows are ``NaN`` in every exceedance array
    (not a spurious ``0``), matching ``map``.
    """
    masses = tuple(masses)
    if not masses:
        raise ValueError("collapse requires at least one credible mass")
    thresholds = tuple(thresholds)
    flat = np.asarray(values, dtype=np.float32)
    if flat.ndim == 1:
        flat = flat.reshape(1, -1)
    n = flat.shape[0]

    map_vals = np.full(n, np.nan, dtype=np.float64)
    hdi = {m: np.full((n, 2), np.nan, dtype=np.float64) for m in masses}
    severe = np.full(n, np.nan, dtype=np.float64)
    bimodality = np.full(n, np.nan, dtype=np.float64)
    exceedance = {c: np.full(n, np.nan, dtype=np.float64) for c in thresholds}

    valid = ~np.isnan(flat).all(axis=1)
    if valid.any():
        # `nan_to_num` looks like it defeats `vfs.exceedance`'s non-finite guard — it was filed
        # as such (#66) — but a *partially* NaN row cannot reach here. `wire_reader` calls
        # `validate_value_plausibility()` before assembly, and `assert_prediction_samples_plausible`
        # REFUSES a run carrying any NaN/Inf sample (C-72). So on the wire path `flat[valid]` rows
        # are either wholly finite or wholly NaN (excluded by `valid` above), and this call is a
        # no-op. Measured on the delivered run: 0 partially-NaN rows in 388,452.
        #
        # It is kept as defence for the legacy path, which has no such gate. The coupling is
        # worth knowing: if C-72 is ever relaxed, this line turns a refusal into a plausible
        # wrong number — `vfs.exceedance` would otherwise raise with a message naming the cause.
        v = np.nan_to_num(flat[valid], nan=0.0, posinf=0.0, neginf=0.0)
        frame = build_prediction_frame(
            v, time=np.zeros(v.shape[0], dtype=np.int64), unit=np.arange(v.shape[0])
        )
        tp = vfs.tower_point(frame)
        m = np.asarray(getattr(tp, "values", tp)).reshape(-1)
        if enforce_non_negative:
            m = np.maximum(m, 0.0)
        map_vals[valid] = m

        h = np.asarray(vfs.hdi_tower(frame, masses=masses)).reshape(v.shape[0], len(masses), 2)
        for j, mass in enumerate(masses):
            hdi[mass][valid] = h[:, j, :]

        severe[valid] = expected_shortfall(v, tail=tail)
        bimodality[valid] = np.asarray(vfs.bimodality(frame)).reshape(-1)  # 0/1 per row (ADR-025 A.3)

        if thresholds:
            # `(n_valid, K)` survival fractions P(Y > c); NaN-filled rows stay NaN (untouched).
            ex = np.asarray(vfs.exceedance(frame, thresholds)).reshape(v.shape[0], len(thresholds))
            for k, c in enumerate(thresholds):
                exceedance[c][valid] = ex[:, k]

    return CollapseResult(
        map=map_vals, hdi=hdi, severe=severe, bimodality=bimodality, exceedance=exceedance
    )
