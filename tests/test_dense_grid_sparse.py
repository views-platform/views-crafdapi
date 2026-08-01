"""C-87: dense-grid fill must be correct on sparse inputs.

Two failure modes the fill had:
  1. recreated missing cells got a *scalar* fill value in array (sample) columns, so
     `_validate_prediction_structure` crashed on the recreated cell;
  2. an entity present only in earlier months (absent from the last month) was silently
     dropped from the served grid (silent data loss).
"""

import numpy as np
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset

pytestmark = pytest.mark.layer2_data


def test_recreated_cell_gets_sample_length_array_not_scalar():
    """Dropping one (month, entity) cell whose entity is still in the last month: the
    dense fill must recreate it as a sample-length array, and construction must succeed."""
    df = make_fao_df(n_cells=4, n_months=2, n_samples=8, seed=11)
    # Drop (600, 100) — entity 100 is still present in the last month (601), so the
    # dense grid must recreate this cell.
    df = df.drop(index=(600, 100))

    ds = ForecastDataset(df)  # must not raise

    # The recreated cell exists in the canonical store and carries an 8-length float
    # array, not a scalar. Sourced via the frame-canonical subset (S4d: cells no longer
    # persisted in `.dataframe`).
    cell = ds.get_subset_dataframe(time_ids=600, entity_ids=100)["pred_test"].iloc[0]
    assert isinstance(cell, np.ndarray)
    assert cell.shape == (8,)
    assert np.all(cell == 0.0)  # fill_value default
    assert ds.sample_size == 8


def test_recreated_cell_roundtrips_through_to_tensor():
    """A recreated cell must not poison the tensor path (the original crash site)."""
    df = make_fao_df(n_cells=4, n_months=2, n_samples=8, seed=12)
    df = df.drop(index=(600, 102))  # non-last month → cell is recreated by the fill
    ds = ForecastDataset(df)
    tensor = ds.to_tensor()
    assert np.isfinite(tensor).all()


def test_entity_absent_from_last_month_fails_loud():
    """An entity present only in an earlier month (not the last) must fail loud, not be
    silently dropped from the served grid (C-87)."""
    df = make_fao_df(n_cells=4, n_months=2, n_samples=8, seed=13)
    # Remove entity 103 from the last month (601) only — it now appears solely in 600.
    df = df.drop(index=(601, 103))

    with pytest.raises(ValueError, match="C-87"):
        ForecastDataset(df)


def test_dense_input_is_unaffected():
    """A fully dense grid must still construct and keep every cell."""
    df = make_fao_df(n_cells=4, n_months=2, n_samples=8, seed=14)
    ds = ForecastDataset(df)
    assert ds.num_entities == 4
    assert ds.num_time_steps == 2
    assert len(ds.dataframe) == 8
