"""Construct views-frames value objects from faoapi's in-memory sample arrays (#88).

A `PredictionFrame` carries one variable's `(N, S)` posterior samples; a `TargetFrame` one
variable's `(N, 1)` observed values. Geography is deliberately NOT embedded (views-frames
ADR-013 scalar-only metadata / ADR-014 geography injected) — it rides in faoapi's separate
geo-metadata table and is injected at aggregation time. This module is the single place
faoapi turns its sample arrays into the frozen leaf's frame types.
"""

import numpy as np
from views_frames import PredictionFrame, SpatialLevel, SpatioTemporalIndex, TargetFrame


def build_index(time, unit, level: SpatialLevel = SpatialLevel.PGM) -> SpatioTemporalIndex:
    """Build a `SpatioTemporalIndex` from integer time/unit identifier arrays."""
    return SpatioTemporalIndex(
        time=np.asarray(time, dtype=np.int64),
        unit=np.asarray(unit, dtype=np.int64),
        level=level,
    )


def build_prediction_frame(
    values, time, unit, level: SpatialLevel = SpatialLevel.PGM
) -> PredictionFrame:
    """Build a `PredictionFrame` from `(N, S)` posterior samples for one variable.

    A 1-D `(S,)` input is treated as a single row `(1, S)`.
    """
    arr = np.asarray(values, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.ndim != 2:
        raise ValueError(f"PredictionFrame values must be (N, S); got shape {arr.shape}")
    return PredictionFrame(arr, build_index(time, unit, level))


def build_target_frame(
    values, time, unit, level: SpatialLevel = SpatialLevel.PGM
) -> TargetFrame:
    """Build a `TargetFrame` from `(N,)` or `(N, 1)` observed values for one variable."""
    arr = np.asarray(values, dtype=np.float32).reshape(-1, 1)
    return TargetFrame(arr, build_index(time, unit, level))
