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
