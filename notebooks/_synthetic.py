"""Synthetic, leak-free demo data for the offline notebook (03_offline_demo.ipynb).

Generates a FAO-shaped forecast frame on a **toy lattice with fictional geography** — no
real countries, coordinates, or UN data — so the offline demo is fully reproducible and
runnable by anyone with zero credentials (mirrors the views-frames `notebooks/_synthetic.py`
convention). Fixed seed => deterministic.

The frame matches what `ForecastDataset` expects: a 2-level ``(month_id, priogrid_id)``
MultiIndex, the nine geo-metadata columns, and one `(S,)`-array posterior sample column per
``pred_*`` target. Geography nests cleanly country -> gaul0 -> gaul1 -> gaul2 so the
aggregation demo is meaningful at every level.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Fictional ISO3-style codes + names (deliberately not real places).
_COUNTRIES = [("WST", "Westland"), ("EST", "Eastland")]


def make_demo_dataframe(
    n_x: int = 8,
    n_y: int = 8,
    n_months: int = 3,
    n_samples: int = 256,
    *,
    first_month_id: int = 600,
    seed: int = 0,
) -> pd.DataFrame:
    """Build a synthetic FAO forecast DataFrame on an ``n_x by n_y`` toy lattice.

    Each grid cell carries `n_samples` posterior draws for two targets (`pred_lr_ged_sb`,
    `pred_lr_ged_ns`) with a spatial hotspot in one corner, so maps and HDI/MAP summaries show
    real structure. Returns a frame ready for ``ForecastDataset(df)``.
    """
    rng = np.random.default_rng(seed)
    n_cells = n_x * n_y

    # --- toy lattice coordinates + a clean country/admin hierarchy by position ---
    xs, ys, priogrid_ids = [], [], []
    iso, g0c, g0n, g1c, g1n, g2c, g2n = [], [], [], [], [], [], []
    intensity = []  # per-cell mean conflict intensity (spatial hotspot)
    for i in range(n_x):
        for j in range(n_y):
            xs.append(float(i))
            ys.append(float(j))
            priogrid_ids.append(1000 + i * n_y + j)
            west = i < n_x / 2                      # country: left/right halves
            south = j < n_y / 2                     # gaul0: top/bottom halves
            ci, cname = _COUNTRIES[0] if west else _COUNTRIES[1]
            iso.append(ci)
            g0 = (0 if west else 2) + (0 if south else 1)   # gaul0: 4 provinces
            g0c.append(10 * (g0 + 1))
            g0n.append(f"{cname} Province {g0 % 2 + 1}")
            g1 = g0 * 2 + (i % 2)                            # gaul1: split each gaul0 again
            g1c.append(100 * (g1 + 1))
            g1n.append(f"Region {g1 + 1}")
            g2c.append(1000 + i * n_y + j)                  # gaul2: one per cell
            g2n.append(f"District {i}-{j}")
            # hotspot near the lattice's NE corner, smooth falloff
            d = np.hypot(i - (n_x - 1), j - (n_y - 1))
            intensity.append(0.2 + 4.0 * np.exp(-(d ** 2) / (2 * (max(n_x, n_y) / 3) ** 2)))

    # --- per-month frames: gamma draws scaled by the cell's intensity ---
    frames = []
    for m in range(n_months):
        month_id = first_month_id + m
        pred_lr_ged_sb, pred_lr_ged_ns = [], []
        for c in range(n_cells):
            scale = intensity[c] * (1.0 + 0.1 * m)   # mild trend over time
            pred_lr_ged_sb.append(rng.gamma(shape=1.5, scale=scale, size=n_samples).astype(np.float32))
            pred_lr_ged_ns.append(rng.gamma(shape=2.0, scale=scale * 0.5, size=n_samples).astype(np.float32))
        frames.append(pd.DataFrame({
            "pg_xcoord": xs, "pg_ycoord": ys,
            "country_iso_a3": iso,
            "admin1_gaul1_code": g1c, "admin1_gaul1_name": g1n,
            "admin1_gaul0_code": g0c, "admin1_gaul0_name": g0n,
            "admin2_gaul2_code": g2c, "admin2_gaul2_name": g2n,
            "pred_lr_ged_sb": pred_lr_ged_sb, "pred_lr_ged_ns": pred_lr_ged_ns,
        }, index=pd.MultiIndex.from_arrays(
            [[month_id] * n_cells, priogrid_ids], names=["month_id", "priogrid_id"],
        )))
    return pd.concat(frames)
