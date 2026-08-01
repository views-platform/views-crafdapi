"""`expected_shortfall` — the ``severe_scenario`` reducer (ADR-025).

The mean of the worst tail of posterior draws per row: a coherent, reproducible worst-case
severity, deliberately **not** the raw sample maximum (which is high-variance and
non-reproducible). For raw fatality **counts** (ADR-024) the "worst" outcomes are the
**largest** draws, so this averages the top ``tail`` fraction of each row.

Unlike the order-statistic MAP/HDI (`estimator.tower_collapse` / `collapse`), this is an
**arithmetic** surface — a mean — so it is computed in **float64** regardless of the float32
sample store (ADR-030 §6: the one place dtype materially matters). Pandas-free (the ADR-030
`summarize/` ratchet): numpy only.
"""

from __future__ import annotations

import numpy as np


def expected_shortfall(values: np.ndarray, tail: float = 0.05) -> np.ndarray:
    """Per-row mean of the worst (largest) ``tail`` fraction of draws.

    Parameters
    ----------
    values : array-like, shape ``(N, S)`` or ``(S,)``
        Posterior samples per row (a 1-D input is treated as a single row). Raw counts.
    tail : float, in ``(0, 1]``
        Fraction of the most-severe draws to average. ``0.05`` = mean of the worst 5%.

    Returns
    -------
    np.ndarray, shape ``(N,)``, float64
        The severe-scenario value per row. All-NaN rows yield ``NaN``; non-finite values in
        otherwise-valid rows are zeroed before the reduction (defined on finite samples).
    """
    if not 0.0 < tail <= 1.0:
        raise ValueError(f"tail must be in (0, 1], got {tail}")

    flat = np.asarray(values, dtype=np.float64)
    if flat.ndim == 1:
        flat = flat.reshape(1, -1)
    n, s = flat.shape

    out = np.full(n, np.nan, dtype=np.float64)
    valid = ~np.isnan(flat).all(axis=1)
    if valid.any():
        v = np.nan_to_num(flat[valid], nan=0.0, posinf=0.0, neginf=0.0)
        k = max(1, int(np.ceil(tail * s)))  # number of worst (largest) draws
        # The k largest per row (unordered within the top-k is fine — we take their mean).
        worst_k = np.partition(v, s - k, axis=1)[:, s - k:]
        out[valid] = worst_k.mean(axis=1)
    return out
