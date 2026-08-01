"""Phase 4a (#112): the ForecastDataset facade — the FAO_PGMDataset back-compat alias,
pickle round-trip (disk-cache path), and the explicit copy() clone (C-137)."""

import pickle

import numpy as np
import pytest

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import FAO_PGMDataset, ForecastDataset

pytestmark = pytest.mark.layer2_data


def test_fao_pgmdataset_is_forecastdataset_alias():
    """The rename keeps FAO_PGMDataset working (pickles, isinstance, ~18 test modules)."""
    assert FAO_PGMDataset is ForecastDataset


def test_isinstance_holds_through_alias():
    ds = ForecastDataset(make_fao_df(seed=1))
    assert isinstance(ds, FAO_PGMDataset)


def test_pickle_roundtrip():
    """The disk cache pickles the dataset object; the rename must not break that."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=6, seed=1))
    restored = pickle.loads(pickle.dumps(ds))
    assert isinstance(restored, ForecastDataset)
    assert restored.dataframe.shape == ds.dataframe.shape
    assert restored.geo_metadata.equals(ds.geo_metadata)


def test_copy_is_independent_structure_but_shares_sample_buffers():
    """copy() uses the optimised __deepcopy__: a new DataFrame object (independent structure)
    that shares the underlying (N,S) sample buffers in `_sample_store` (the ~1,700x clone,
    C-137). Post-S4d the canonical store is the frame, not object cells."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=8, seed=2))
    clone = ds.copy()

    assert isinstance(clone, ForecastDataset)
    assert clone.dataframe is not ds.dataframe  # independent container
    # same underlying sample buffer → shared on clone (the perf optimisation, C-137)
    assert np.shares_memory(clone._sample_array("pred_test"), ds._sample_array("pred_test"))
    # the split-tensor cache is reset on copy
    assert clone._split_tensor_cache == {}
