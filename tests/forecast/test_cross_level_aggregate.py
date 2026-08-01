"""Phase 3 (#90): geography modules + proof that the views-frames leaf does the same
joint-sampling aggregation as faoapi's hand-rolled primitive (the substitution validation)."""

import numpy as np
import pandas as pd
import pytest
from views_frames import SpatialLevel

from views_faoapi.data.handlers import FAO_PGMDataset
from views_faoapi.forecast.aggregate.cross_level import aggregate_via_leaf, elementwise_sum
from views_faoapi.forecast.geography.level_mapping import build_cell_to_unit_mapping
from views_faoapi.forecast.geography.metadata_table import (
    LEVELS,
    resolve_level_cells,
)
from tests.conftest import make_fao_df

pytestmark = pytest.mark.layer2_data


# ── geography ───────────────────────────────────────────────────────────────────
def test_levels_vocabulary():
    assert LEVELS["country"] == "country_iso_a3"
    assert LEVELS["gaul0"] == "admin1_gaul0_code"


def test_build_mapping_integer_level_passes_codes_through():
    ds = FAO_PGMDataset(make_fao_df(seed=2))
    keys, vals, lookup = build_cell_to_unit_mapping(ds.geo_metadata, "admin1_gaul0_code")
    assert keys.shape[1] == 2 and keys.shape[0] == len(ds.geo_metadata)
    assert lookup is None  # integer codes are the unit ids directly
    assert set(vals.tolist()) == {10, 20}  # fixture gaul0 codes


def test_build_mapping_string_level_factorizes():
    ds = FAO_PGMDataset(make_fao_df(seed=2))
    keys, vals, lookup = build_cell_to_unit_mapping(ds.geo_metadata, "country_iso_a3")
    assert lookup is not None  # ISO3 strings -> integer unit space
    assert set(lookup.values()) == {"AAA", "BBB"}
    assert np.issubdtype(vals.dtype, np.integer)


def test_resolve_level_cells():
    ds = FAO_PGMDataset(make_fao_df(seed=2))
    cells = resolve_level_cells(ds.geo_metadata, "admin1_gaul0_code", 10, "priogrid_id")
    assert set(cells) == {100, 101}  # fixture: cells 100,101 -> gaul0 code 10


# ── aggregate ───────────────────────────────────────────────────────────────────
def test_elementwise_sum_is_joint():
    out = elementwise_sum(pd.Series([np.array([1.0, 2.0, 3.0]), np.array([4.0, 5.0, 6.0])]))
    assert np.array_equal(out, [5.0, 7.0, 9.0])


def test_aggregate_via_leaf_manual_case():
    samples = np.array(
        [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]], dtype=np.float32
    )
    time = np.array([600, 600, 601, 601])
    cell = np.array([100, 101, 100, 101])
    keys = np.column_stack([time, cell]).astype(np.int64)
    vals = np.array([10, 10, 10, 10], dtype=np.int64)  # both cells -> unit 10

    agg = aggregate_via_leaf(samples, time, cell, keys, vals, SpatialLevel.CM)
    got = {
        (int(t), int(u)): agg.values[i]
        for i, (t, u) in enumerate(zip(agg.index.time, agg.index.unit))
    }
    assert np.allclose(got[(600, 10)], [5, 7, 9])
    assert np.allclose(got[(601, 10)], [17, 19, 21])


def test_leaf_equals_faoapi_primitive():
    """The leaf path must produce the same joint-sum as faoapi's `elementwise_sum` for a group."""
    cell_samples = np.random.default_rng(1).random((3, 16)).astype(np.float32)
    fao = elementwise_sum(pd.Series(list(cell_samples)))

    agg = aggregate_via_leaf(
        cell_samples,
        time=[600, 600, 600],
        unit=[100, 101, 102],
        map_keys=np.array([[600, 100], [600, 101], [600, 102]], dtype=np.int64),
        map_vals=np.array([10, 10, 10], dtype=np.int64),
        target_level=SpatialLevel.CM,
    )
    assert np.allclose(agg.values[0], fao, atol=1e-5)
