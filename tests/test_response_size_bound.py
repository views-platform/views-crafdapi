"""The C-284 size bound: refuse a request whose rendering the box cannot afford.

Two things are under test and they fail differently.

``served_payload_shape`` must describe the frame ``get_subset_dataframe`` actually builds. If it
drifts, the guard mis-sizes every request silently and nothing else notices -- so the drift test
materialises the real frame and compares, rather than asserting the helper against a hand-written
number that would drift with it.

The route guard must refuse above the budget and admit below it, *with filters applied*. A bound
computed on the full table would refuse the narrow requests CRAF'd actually makes, which is the
failure mode that matters here and the one views-faoapi's unfiltered equivalent cannot have.
"""

import pytest

from views_crafdapi.data.handlers.forecast_dataset import ForecastDataset
from views_crafdapi.managers.serialization import (
    dataframe_to_dict,
    estimate_records_bytes,
)

from .conftest import make_fao_df


@pytest.fixture
def ds():
    return ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=10))


# --- the estimate must describe the frame that gets built ------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"with_metadata": False},
        {"time_ids": [600]},
        {"entity_ids": [100, 101]},
        {"features": ["pred_test"]},
        {"sample_idx": [0, 1, 2]},
        {"time_ids": [600], "features": ["pred_test"], "with_metadata": False},
        {"level": "country", "entity_ids": ["AAA"]},
    ],
    ids=lambda k: ",".join(k) or "unfiltered",
)
def test_payload_shape_matches_the_materialised_frame(ds, kwargs):
    """The shape helper and the payload must agree on rows, keyed columns and sample width.

    This is the test that makes the WET column-selection branch in ``served_payload_shape``
    safe: it mirrors ``_GridDataset.get_subset_dataframe``, and if either side changes without
    the other, this fails instead of the guard quietly sizing the wrong thing.
    """
    rows, keyed, elements = ds.served_payload_shape(**kwargs)
    frame = ds.get_subset_dataframe(**kwargs)

    assert rows == len(frame)
    # dataframe_to_dict calls reset_index(), so index levels are keyed columns in every record.
    assert keyed == frame.index.nlevels + len(frame.columns)

    if elements:
        record = dataframe_to_dict(frame)[0]
        counted = sum(
            len(v) for v in record.values() if isinstance(v, (list, tuple))
        )
        assert elements == counted


def test_an_empty_selection_costs_nothing(ds):
    assert ds.served_payload_shape(entity_ids=[]) == (0, 0, 0)
    assert ds.served_payload_shape(time_ids=[]) == (0, 0, 0)


def test_filters_shrink_the_estimate(ds):
    """The property the whole design turns on: a narrow request must estimate small.

    If this fails, the bound refuses the requests CRAF'd depends on.
    """
    full = estimate_records_bytes(*ds.served_payload_shape())
    narrow = estimate_records_bytes(
        *ds.served_payload_shape(time_ids=[600], features=["pred_test"], sample_idx=[0])
    )
    assert 0 < narrow < full


def test_aggregate_does_not_shrink_the_estimate(ds):
    """Aggregation runs after the pg-grain frame exists, so it cannot lower the peak.

    Estimating on the post-aggregation grain would describe the response and miss the cost.
    """
    pg_grain = ds.served_payload_shape(level="country")
    aggregated = ds.served_payload_shape(level="country", aggregate=True)
    assert aggregated[0] == pg_grain[0]


def test_a_wider_sample_costs_more_at_the_same_row_count():
    """Row counts are equal; only draw width differs. A row bound could not tell these apart --
    which is the reason this bound is in bytes."""
    narrow = ForecastDataset(make_fao_df(n_samples=10))
    wide = ForecastDataset(make_fao_df(n_samples=1000))

    assert narrow.served_payload_shape()[0] == wide.served_payload_shape()[0]
    assert estimate_records_bytes(*wide.served_payload_shape()) > estimate_records_bytes(
        *narrow.served_payload_shape()
    )
