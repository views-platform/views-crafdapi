"""Joint-sum a month's cell samples to a geographic level, then collapse them — as arrays.

ADR-030 S7. This is the aggregate path's reduction with the DataFrame taken out of the
middle of it. The path it replaces made a four-step round trip through pandas:

    explode  `(N, S)` store -> N object cells    (grid_dataset.get_subset_dataframe)
    stack    object cells   -> `(N, S)`          (_stack_cells, for the views-frames leaf)
    scatter  group sums     -> object cells      (result[var] = [sums[idx] ...])
    stack    object cells   -> `(n_units, S)`    (for `collapse`)

Every step had an array-native counterpart already present, so the DataFrame was carrying
only itself. Here the samples never enter pandas: `(N, S)` in, reduced statistics out.
`calculate_hdi_map` keeps pandas for the group index and the metadata columns — the
serialize seam ADR-030 §1 allows — and assembles the frame from stacked arrays at the end.

Streamed a month at a time by the caller, mirroring the cell path (S6b-1 / #208). Groups are
`(time, unit)` and a month is exactly one time value, so no group ever spans two months:
per-month reduction is byte-identical to reducing every month at once, both in which draws
are summed and in what order they are summed.
"""

import numpy as np
import views_frames_summarize as vfs
from views_frames import SpatialLevel

from views_crafdapi.forecast.frames.builder import build_prediction_frame


def has_level_code(codes: np.ndarray) -> np.ndarray:
    """Boolean mask over cells: True where the cell carries a code for this level.

    Register C-146: ~1.1% of real cells have no GAUL code, and those cells must be
    *excluded* from the aggregate rather than summed together into a phantom unit. The
    pandas path expressed this as `pd.factorize`'s -1 sentinel; here it is a named test,
    because "which cells are in the aggregate" is the kind of state worth reading directly.
    """
    if np.issubdtype(codes.dtype, np.integer):
        return np.ones(len(codes), dtype=bool)
    if np.issubdtype(codes.dtype, np.floating):
        return ~np.isnan(codes)
    # object dtype (ISO3 / GAUL name strings): missing arrives as None or a float NaN.
    return np.array([c is not None and c == c for c in codes], dtype=bool)


def joint_sum_to_level(values: np.ndarray, time: np.ndarray, codes: np.ndarray):
    """Joint-sum `(N, S)` cell samples per `(time, level code)` group.

    Returns `(keys, block)`: `keys[i]` is the `(time, code)` of row `i` of the `(n_units, S)`
    `block`. Joint-sum means sample *j* of a unit is the sum of sample *j* across its cells,
    which is what preserves cross-cell correlation (register C-70) — it is the same
    `aggregate_via_leaf` call the pandas path made, just without the round trip around it.

    Cells with no code for this level are dropped (see `has_level_code`).
    """
    keep = has_level_code(codes)
    if not keep.all():
        values, time, codes = values[keep], time[keep], codes[keep]
    if len(values) == 0:
        return [], np.empty((0, values.shape[1] if values.ndim == 2 else 0), dtype=np.float32)

    if np.issubdtype(codes.dtype, np.integer):
        unit_ids = codes.astype(np.int64)
        labels = None
    else:
        # The leaf's unit space is integer. `np.unique` (not `pd.factorize`) keeps this module
        # pandas-free per the ADR-030 §7 ratchet; which integer a code maps to is arbitrary —
        # every unit is mapped straight back to its original label below.
        labels, inverse = np.unique(codes, return_inverse=True)
        unit_ids = inverse.astype(np.int64)

    times = time.astype(np.int64)
    map_keys = np.column_stack([times, unit_ids])
    frame = build_prediction_frame(values, times, unit_ids)
    agg = vfs.aggregate_distributions_arrays(frame, map_keys, unit_ids, SpatialLevel.PGM)

    units = np.asarray(agg.index.unit)
    keys = [
        (int(t), labels[int(u)] if labels is not None else int(u))
        for t, u in zip(np.asarray(agg.index.time), units)
    ]
    return keys, np.asarray(agg.values)
