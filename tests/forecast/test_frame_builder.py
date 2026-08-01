"""Phase 1 (#88): the frame builder + dataset.to_frames() produce well-formed
views-frames value objects from the ingested grid."""

import numpy as np
import pytest
from views_frames import PredictionFrame, SpatialLevel, TargetFrame
from views_frames.conformance import assert_frame_contract

from views_faoapi.data.handlers import FAO_PGMDataset
from views_faoapi.forecast.frames.builder import build_prediction_frame, build_target_frame
from tests.conftest import make_fao_df

pytestmark = pytest.mark.layer2_data


def test_build_prediction_frame_shape_and_contract():
    pf = build_prediction_frame(np.random.default_rng(0).random((6, 8)), time=[600] * 6, unit=range(6))
    assert isinstance(pf, PredictionFrame)
    assert pf.values.shape == (6, 8)
    assert pf.index.level is SpatialLevel.PGM
    assert_frame_contract(pf)  # the leaf's own contract holds


def test_build_target_frame_is_n_by_1():
    tf = build_target_frame(np.arange(5.0), time=[600] * 5, unit=range(5))
    assert isinstance(tf, TargetFrame)
    assert tf.values.shape == (5, 1)
    assert_frame_contract(tf)


def test_dataset_to_frames_forecast():
    ds = FAO_PGMDataset(make_fao_df(n_cells=4, n_months=2, n_samples=9, seed=5))
    frames = ds.to_frames()
    assert set(frames) == {"pred_test", "pred_other"}
    pf = frames["pred_test"]
    assert isinstance(pf, PredictionFrame)
    assert pf.values.shape == (8, 9)  # 4 cells x 2 months, 9 samples
    assert_frame_contract(pf)
