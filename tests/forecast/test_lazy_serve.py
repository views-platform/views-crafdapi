"""S6b-1 (#208): the cell-level serving path streams per month instead of materializing the
full `(n_time, n_entity, S, targets)` float64 grid — byte-identically, and the disk store mmaps.

The golden test (`test_served_output_golden.py`) is the absolute byte-identity guard; these tests
add the S6b-1-specific guarantees: multi-month subset consistency, that the full grid is never
built on the served path, and that a disk-served run pages via an mmap store.
"""
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import ForecastDataset, _GridDataset
from views_faoapi.forecast.serialize import schema
from views_faoapi.forecast.summarize.estimator import collapse
from views_faoapi.managers.disk_cache import FAODiskCacheManager

pytestmark = pytest.mark.layer2_data


def _whole_grid_hdi_map(ds, *, alpha=0.9, features=None, sample_idx=None,
                        time_ids=None, entity_ids=None, enforce_non_negative=False):
    """A faithful reproduction of the PRE-S6b-1 whole-grid reduction, using the intact
    `get_subset_tensor`/`_prediction_to_tensor` (which S6b-1 left in place for check_integrity).
    This is the independent oracle: the streaming base `calculate_hdi_map` must equal this for
    every subset config it re-implements inline (entity_ids / sample_idx / features / empty)."""
    selected_vars = features if features else ds.targets
    if features is not None and not isinstance(features, list):
        selected_vars = [features]
    tensor = ds.get_subset_tensor(
        features=selected_vars, sample_idx=sample_idx, time_ids=time_ids, entity_ids=entity_ids
    )
    results = []
    for var_idx, var in enumerate(selected_vars):
        vt = tensor[..., var_idx]
        flat = vt.reshape(-1, vt.shape[2])
        # ADR-025 reduction (#222/S4): MAP + fixed 50/90/95 HDIs + severe (min/max dropped);
        # collapse NaN-fills all-NaN rows, matching the streaming path.
        cr = collapse(flat, masses=schema.MASSES, enforce_non_negative=enforce_non_negative)
        shape = vt.shape[:2]
        hdi = {
            mm: (cr.lower(mm).reshape(shape), cr.upper(mm).reshape(shape))
            for mm in schema.MASSES
        }
        results.append(
            ds._create_series_value_dataframe(
                var, cr.map.reshape(shape), cr.severe.reshape(shape),
                cr.bimodality.reshape(shape), hdi, time_ids, entity_ids
            )
        )
    return pd.concat(results, axis=1)


def test_streaming_equals_whole_grid_oracle_across_subsets():
    """S6b-1: the streamed base `calculate_hdi_map` is byte-identical to the exact whole-grid
    path it replaced — for every subset config it re-implements inline. Closes the coverage gap
    that the golden (full serve, 2 months) doesn't reach."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=3, n_samples=32, seed=5))
    cells = ds.dataframe.index.get_level_values("priogrid_id").unique().tolist()
    months = ds.dataframe.index.get_level_values("month_id").unique().tolist()
    configs = [
        {},
        {"sample_idx": [0, 5, 31]},
        {"sample_idx": [7]},
        {"entity_ids": [cells[0], cells[2]]},
        {"features": [ds.targets[1]]},
        {"features": [ds.targets[0]], "sample_idx": [1, 2, 3]},
        {"time_ids": [months[2], months[0]]},          # descending order preserved
        {"enforce_non_negative": True},
        {"time_ids": []},                              # empty selection → empty frame, not a 500
    ]
    for kw in configs:
        got = _GridDataset.calculate_hdi_map(ds, **kw)   # the streaming path (base, no geo join)
        exp = _whole_grid_hdi_map(ds, **kw)              # the pre-refactor whole-grid oracle
        assert got.equals(exp), f"streaming diverges from whole-grid oracle for {kw}"


def test_per_month_subset_equals_slice_of_full():
    """Streaming per month must be internally coherent: serving a single month equals that
    month's rows of the full-map serve, across every month — at a scale (6 months) well beyond
    the 2-month golden, with even/odd S."""
    for n_samples in (33, 64):
        ds = ForecastDataset(make_fao_df(n_cells=4, n_months=6, n_samples=n_samples, seed=7))
        full = ds.calculate_hdi_map(aggregate=False)
        months = ds.dataframe.index.get_level_values("month_id").unique().tolist()
        assert len(months) == 6
        for m in months:
            sub = ds.calculate_hdi_map(aggregate=False, time_ids=m)
            expected = full.loc[full.index.get_level_values("month_id") == m]
            assert sub.equals(expected), f"month {m} subset diverges from the full-map slice"


def test_served_path_never_builds_the_full_grid():
    """The whole point of S6b-1: an aggregate=False serve must NOT call `_prediction_to_tensor`
    (the ~57 GB full-grid float64 allocation) — it streams per month instead."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=3, n_samples=32, seed=11))
    with patch.object(
        ForecastDataset, "_prediction_to_tensor",
        side_effect=AssertionError("full grid must not be built on the served path"),
    ) as spy:
        out = ds.calculate_hdi_map(aggregate=False)
    assert spy.call_count == 0
    assert len(out) == len(ds.dataframe)


def test_disk_served_run_uses_mmap_store_and_serves_identically(tmp_path):
    """A disk-served run's `_sample_store` is a read-only memmap (paged, not resident), and it
    serves byte-identical HDI/MAP to the in-RAM dataset."""
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=4, n_samples=40, seed=3))
    cache = FAODiskCacheManager(tmp_path)
    cache.write("h", "forecast", ds, "file_1", source_kind="wire", provenance={"run_id": "r"})

    out = cache.read("h", "forecast")
    served = out["dataset"]
    for var in served.targets:
        assert isinstance(served._sample_store[var], np.memmap)  # paged, not resident

    a = ds.calculate_hdi_map(aggregate=False)
    b = served.calculate_hdi_map(aggregate=False)
    assert a.equals(b)
    # aggregate path (fancy-indexes only touched rows) also identical off the mmap store
    a2 = ds.calculate_hdi_map(aggregate=True, level="country")
    b2 = served.calculate_hdi_map(aggregate=True, level="country")
    assert a2.equals(b2)
