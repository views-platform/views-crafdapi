"""Build the injected `(time, priogrid) -> target_unit` mapping for cross-level aggregation.

views-frames does the conservation-correct joint-sum but never owns geography — the consumer
injects the mapping (ADR-014). This module derives that mapping from crafdapi's geo-metadata
table. Target unit ids must be integers (the leaf's `SpatioTemporalIndex.unit` is an integer
array); string level codes (e.g. `country_iso_a3`) are factorized to a stable integer space,
and the original codes are returned so the caller can re-attach human-readable labels.

Extracted/new in Phase 3 of #87 / #90.
"""

import numpy as np
import pandas as pd


def build_cell_to_unit_mapping(geo_metadata: pd.DataFrame, level_col: str):
    """Return `(map_keys, map_vals, code_lookup)`.

    - ``map_keys``: ``(M, 2)`` int array of ``(time, priogrid)`` for every cell-row.
    - ``map_vals``: ``(M,)`` int array of the target unit id for that cell.
    - ``code_lookup``: ``{unit_id: original_code}`` when `level_col` was non-integer
      (e.g. ISO3 strings), else ``None`` (the codes are already the unit ids).
    """
    idx = geo_metadata.index
    time = idx.get_level_values(0).to_numpy()
    cell = idx.get_level_values(1).to_numpy()
    codes = geo_metadata[level_col].to_numpy()

    if np.issubdtype(codes.dtype, np.integer):
        unit_vals = codes.astype(np.int64)
        code_lookup = None
    else:
        unit_ints, uniques = pd.factorize(codes)
        unit_vals = unit_ints.astype(np.int64)
        code_lookup = dict(enumerate(uniques))

    map_keys = np.column_stack([time, cell]).astype(np.int64)
    return map_keys, unit_vals, code_lookup
