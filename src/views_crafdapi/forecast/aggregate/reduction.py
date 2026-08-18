"""Joint-sum a month's cell samples to a geographic level — as arrays, not through pandas.

ADR-030 S7. This is the aggregate path's reduction with the DataFrame taken out of the
middle of it. The path it replaces made a four-step round trip through pandas:

    explode  `(N, S)` store -> N object cells    (grid_dataset.get_subset_dataframe)
    stack    object cells   -> `(N, S)`          (_stack_cells, for the views-frames leaf)
    scatter  group sums     -> object cells      (result[var] = [sums[idx] ...])
    stack    object cells   -> `(n_units, S)`    (for `collapse`)

Every step had an array-native counterpart already present, so the DataFrame was carrying
only itself. Here the samples never enter pandas: `(N, S)` in, summed block out.

**Prediction samples only.** ADR-030 §1 keeps the historical/scalar leg on the legacy float64
`elementwise_sum`; the views-frames leaf is float32-native and would silently re-baseline it
(register C-258, which is exactly how that went wrong once). The caller dispatches.

Streamed a month at a time by the caller, mirroring the cell path (S6b-1 / #208). Groups are
`(time, unit)` and a month is exactly one time value, so no group ever spans two months:
per-month reduction is byte-identical to reducing every month at once, both in which draws
are summed and in what order they are summed.
"""

import numpy as np
from views_frames import SpatialLevel

from views_crafdapi.forecast.aggregate.cross_level import aggregate_via_leaf


def _is_missing(code) -> bool:
    """True if `code` is any flavour of "no value" — None, NaN, or a pandas NA scalar.

    `code != code` catches float NaN. For `pd.NA` that expression returns `pd.NA`, whose
    truth value raises rather than being False, so the raise is caught and read as missing —
    which it is. Written out because the terse `c == c` idiom silently breaks on nullable
    dtypes, and `geo_metadata` is only cast to `category` when it arrives as `object`.
    """
    if code is None:
        return True
    try:
        return bool(code != code)
    except TypeError:
        return True


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
    return np.array([not _is_missing(c) for c in codes], dtype=bool)


def encode_level_codes(codes: np.ndarray):
    """Map level codes to the leaf's integer unit space, once for a whole request.

    Returns `(keep, unit_ids, labels)`: `keep` is the `has_level_code` mask, `unit_ids` are
    the integer ids for the kept cells, and `labels` maps an id back to its original code
    (`None` when the codes were already integers and are their own labels).

    Called once per request rather than once per (month, target): the codes do not depend on
    either, and the sort behind the mapping is not free — it measured 27% of runtime when it
    sat inside the loop.
    """
    keep = has_level_code(codes)
    kept = codes[keep]
    if np.issubdtype(codes.dtype, np.integer):
        return keep, kept.astype(np.int64), None
    # `np.unique` (not `pd.factorize`) keeps this module pandas-free per the ADR-030 §7
    # ratchet. Which integer a code maps to is arbitrary — every unit is mapped straight back
    # to its original label by the caller.
    labels, inverse = np.unique(kept, return_inverse=True)
    return keep, inverse.astype(np.int64), labels


def joint_sum_to_level(values: np.ndarray, time: np.ndarray, unit_ids: np.ndarray):
    """Joint-sum `(N, S)` cell samples per `(time, unit)` group.

    Returns `(keys, block)`: `keys[i]` is the `(time, unit_id)` of row `i` of the
    `(n_units, S)` `block`. Joint-sum means sample *j* of a unit is the sum of sample *j*
    across its cells, which is what preserves cross-cell correlation (register C-70).

    `values`/`time`/`unit_ids` must already be restricted to cells that carry a code for the
    level — see `encode_level_codes`.
    """
    if len(values) == 0:
        width = values.shape[1] if values.ndim == 2 else 0
        return [], np.empty((0, width), dtype=np.float32)

    times = time.astype(np.int64)
    map_keys = np.column_stack([times, unit_ids])
    agg = aggregate_via_leaf(values, times, unit_ids, map_keys, unit_ids, SpatialLevel.PGM)
    keys = [
        (int(t), int(u))
        for t, u in zip(np.asarray(agg.index.time), np.asarray(agg.index.unit))
    ]
    return keys, np.asarray(agg.values)
