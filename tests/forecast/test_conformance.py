"""Phase 5 (#91): faoapi proves the views-frames published contracts on real frames, in CI.

This module runs as part of the normal pytest suite (which is the CI gate), so the leaf's
frame / summarizer / cross-level-alignment contracts are asserted on real faoapi-built
forecast and historical frames and the injected GAUL mapping on every run.
"""

import pytest
from views_frames import SpatialLevel

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.conformance import (
    CONFORMANCE_FLOOR,
    assert_cross_level_law,
    assert_frame,
)
from views_crafdapi.forecast.geography.level_mapping import build_cell_to_unit_mapping

pytestmark = pytest.mark.layer2_data


def test_conformance_floor_pinned():
    """Pin the governed floor faoapi tests against (leaf ADR-016)."""
    assert CONFORMANCE_FLOOR == "1.0.0"


def test_forecast_prediction_frames_conform():
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=16, seed=7))
    frames = ds.to_frames()
    assert set(frames) == {"pred_test", "pred_other"}
    for frame in frames.values():
        assert_frame(frame)  # frame contract + summarizer contract


def test_historical_target_frames_conform():
    # Historical = scalar target (S=1) -> TargetFrame.
    df = make_fao_df(n_cells=4, n_months=2, n_samples=1, seed=7).drop(
        columns=["pred_test", "pred_other"]
    )
    df["lr_sb_best"] = [1.0, 2.0, 0.0, 3.0, 1.0, 0.0, 2.0, 1.0][: len(df)]
    ds = ForecastDataset(df, targets=["lr_sb_best"])
    for frame in ds.to_frames().values():
        assert_frame(frame)


def test_cross_level_alignment_law_on_injected_gaul_mapping():
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=16, seed=7))
    frame = ds.to_frames()["pred_test"]
    # faoapi's injected (time, priogrid) -> GAUL admin-0 (country) mapping.
    keys, vals, _ = build_cell_to_unit_mapping(ds.geo_metadata, "admin1_gaul0_code")
    mapping = {
        (int(t), int(u)): int(v) for (t, u), v in zip(map(tuple, keys), vals)
    }
    assert_cross_level_law(frame.index, mapping, SpatialLevel.CM)
