"""S6 (#160 / epic #154): `.dataframe` is sealed for sample columns.

The object-dtype `pred_*` sample cells are no longer reachable via `.dataframe` — the
seal is realized by the S4d **hard drop** (the cells are gone), not by a reconstructing
property that would resurrect them under load (C-148/D-12, resolved toward sealing via
D-19/D-20). The canonical, named path to the draws is `samples()` / `_sample_array`
(C-153). This file pins that boundary so a future change can't silently re-expose or
rebuild object-dtype sample cells on `.dataframe`.
"""
import numpy as np
import pytest

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import ForecastDataset

pytestmark = pytest.mark.layer2_data


def test_dataframe_carries_no_sample_columns():
    """A prediction `.dataframe` carries the index + metadata only — never the sample
    columns, and never a resurrected object-dtype cell."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=16, seed=1))
    assert ds.targets  # this is a prediction dataset
    for var in ds.targets:
        assert var not in ds.dataframe.columns
    # no object-dtype column lingers (no reconstruct-on-demand dual store — C-148)
    assert not any(ds.dataframe[c].dtype == object for c in ds.dataframe.columns)


def test_sample_access_via_dataframe_is_sealed():
    """Reaching for a sample column through `.dataframe` raises (the seal) and never
    silently rebuilds the object-dtype frame; the draws come from `samples()`."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=16, seed=2))
    var = ds.targets[0]
    with pytest.raises(KeyError):
        _ = ds.dataframe[var]
    drawn = ds.samples(var)
    assert drawn.shape == (len(ds.dataframe), ds.sample_size)
    assert drawn.dtype == np.float32


def test_samples_is_the_named_canonical_path():
    """`samples()` is the public accessor (C-153): it serves the canonical store buffer
    (no copy) and rejects non-targets loudly."""
    ds = ForecastDataset(make_fao_df(n_cells=3, n_months=2, n_samples=8, seed=3))
    var = ds.targets[0]
    assert np.shares_memory(ds.samples(var), ds._sample_array(var))
    with pytest.raises(KeyError):
        ds.samples("not_a_target")
