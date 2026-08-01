"""Cross-level, joint-sampling aggregation of posterior distributions.

`elementwise_sum` is faoapi's joint-sum primitive (currently wired into the serving path).
`aggregate_via_leaf` is the views-frames-native path: it injects faoapi's geography mapping
into `views_frames_summarize.aggregate_distributions_arrays`, which performs the same
conservation-correct joint-sum (register C-70: sum the aligned draws across cells *before*
collapsing). The two are proven equivalent in `tests/forecast/test_cross_level_aggregate.py`;
the serving path adopts the leaf path when the dataset becomes frame-native (Phase 4 / #112).

Extracted from `ForecastDataset._elementwise_sum` (Phase 3 of #87 / #90).
"""

import numpy as np
import views_frames_summarize as vfs

from views_crafdapi.forecast.frames.builder import build_prediction_frame


def elementwise_sum(arrays) -> np.ndarray:
    """Sum a group of equal-length sample arrays element-wise (joint sampling).

    ``arrays``: an iterable / pandas Series of `(S,)` arrays. Returns the `(S,)` sum, so
    sample *i* of the result is the sum of sample *i* across all constituent cells.
    """
    stacked = np.stack(list(getattr(arrays, "values", arrays)))
    return stacked.sum(axis=0)


def aggregate_via_leaf(values, time, unit, map_keys, map_vals, target_level):
    """Joint-sum `(N, S)` cell samples up to `target_level` via the views-frames leaf.

    Builds a `PredictionFrame` from the cell-level samples + `(time, unit)` index, injects the
    `(time, unit) -> target_unit` mapping (`map_keys`, `map_vals`), and returns the aggregated
    `PredictionFrame` (rows grouped by `(time, target_unit)`, samples summed element-wise).
    """
    frame = build_prediction_frame(values, time, unit)
    return vfs.aggregate_distributions_arrays(frame, map_keys, map_vals, target_level)
