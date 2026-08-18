"""ADR-030 S7: the aggregate path must not round-trip its samples through pandas.

The canonical store is a contiguous `(N, S)` float32 array. The pre-S7 aggregate path took
four steps to use it: explode it into N object cells to satisfy the DataFrame interface
(`_GridDataset.get_subset_dataframe`), stack them back for the views-frames leaf
(`_stack_cells`), scatter the group sums into object cells again, then stack a third time for
`collapse`. For the delivered run that meant ~7M ndarray objects and ~10.9 GB resident.

These tests pin the *shape* of the work, like the C-235 batching tests beside them: the two
explode sites are asserted to be unreachable from the aggregate path. A wall-clock or
byte-count assertion would be flaky, and `make_fao_df` caps at 4 cells, so no fixture here is
large enough for a memory bound to mean anything. Call sites are deterministic and are exactly
what a future edit would silently regress.
"""
import inspect

import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.data.handlers.grid_dataset import _GridDataset
from views_crafdapi.forecast.aggregate.reduction import (
    encode_level_codes,
    has_level_code,
    joint_sum_to_level,
)

pytestmark = pytest.mark.layer2_data


def _record_calls(monkeypatch, owner, name):
    """Count calls to `owner.name` while leaving its behaviour intact."""
    original = getattr(owner, name)
    calls = []

    def recorded(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    if isinstance(inspect.getattr_static(owner, name), staticmethod):
        # `_stack_cells` is a staticmethod: setting a plain function in its place rebinds it as
        # an instance method, and the call raises TypeError before reaching the assertion below.
        recorded = staticmethod(recorded)
    monkeypatch.setattr(owner, name, recorded)
    return calls


def _dataset(**kwargs):
    params = dict(n_cells=4, n_months=4, n_samples=16, seed=7,
                  targets=("pred_lr_ged_sb", "pred_lr_ged_ns"))
    params.update(kwargs)
    return ForecastDataset(make_fao_df(**params))


def test_aggregate_path_never_explodes_samples_into_object_cells(monkeypatch):
    """`get_subset_dataframe` is the `list(arr)` site — one ndarray object per row per target."""
    ds = _dataset()
    calls = _record_calls(monkeypatch, _GridDataset, "get_subset_dataframe")

    out = ds.calculate_hdi_map(aggregate=True, level="gaul1", with_metadata=False)

    assert len(out) > 0, "the fixture produced no aggregated rows — the test would be vacuous"
    assert calls == [], (
        f"ADR-030 S7: the aggregate path called _GridDataset.get_subset_dataframe "
        f"{len(calls)} time(s). That method rebuilds the contiguous (N, S) store as "
        f"object-dtype cells (`pd.Series(list(arr), dtype=object)`), which is the allocation "
        f"the S7 reduction exists to avoid. Read the samples via `_sample_array` instead."
    )


def test_aggregate_path_never_stacks_cells_back_into_an_array(monkeypatch):
    """The complement: `_stack_cells` is the re-stack that the explode made necessary.

    Asserting only on the explode would pass a path that skipped `get_subset_dataframe` but
    still went cells-to-array somewhere else.
    """
    ds = _dataset()
    calls = _record_calls(monkeypatch, _GridDataset, "_stack_cells")

    ds.calculate_hdi_map(aggregate=True, level="gaul1", with_metadata=False)

    assert calls == [], (
        f"ADR-030 S7: the aggregate path called _stack_cells {len(calls)} time(s). Stacking "
        f"object cells back into (N, S) means something exploded them first — the round trip "
        f"S7 removed."
    )


def test_cells_without_a_level_code_are_excluded_not_summed_together():
    """Register C-146: ~1.1% of real cells carry no GAUL code.

    `pd.factorize` gave those cells the code -1, which — if carried into the joint-sum — makes
    a phantom unit holding the sum of every unmapped cell. They must be dropped instead. This
    is now a named predicate rather than a sentinel, so it is asserted directly.
    """
    values = np.arange(16, dtype=np.float32).reshape(4, 4)
    time = np.array([1, 1, 1, 1])
    codes = np.array(["A", None, "A", float("nan")], dtype=object)

    assert list(has_level_code(codes)) == [True, False, True, False]

    keep, unit_ids, labels = encode_level_codes(codes)
    keys, block = joint_sum_to_level(values[keep], time[keep], unit_ids)
    assert [labels[u] for _, u in keys] == ["A"], "unmapped cells leaked into the aggregate"
    # Rows 0 and 2 only — rows 1 and 3 have no code for this level.
    np.testing.assert_array_equal(block[0], values[0] + values[2])


def test_missing_codes_survive_pandas_nullable_dtypes():
    """`pd.NA` is not caught by the terse `c == c` NaN idiom — `bool(pd.NA)` raises.

    Reachable because `ForecastDataset.__init__` only casts `geo_metadata` to `category` when
    it arrives as `object`, so a `geo.parquet` carrying `string`/arrow-backed dtypes reaches
    the predicate unconverted. A raise here surfaces as HTTP 500 naming neither column nor
    level, on exactly the C-146 case the function is named for.
    """
    codes = pd.array(["A", pd.NA, "B"], dtype="string").to_numpy()
    assert list(has_level_code(codes)) == [True, False, True]


def test_joint_sum_adds_aligned_draws_not_summary_statistics():
    """Register C-70: sample *j* of a unit is the sum of sample *j* across its cells.

    Summing collapsed statistics instead would give the same mean and the wrong interval, and
    it would not show up in any row count or shape — only in the values.
    """
    values = np.array([[1.0, 10.0], [2.0, 20.0], [100.0, 100.0]], dtype=np.float32)
    time = np.array([5, 5, 6])
    codes = np.array([3, 3, 3])

    keep, unit_ids, labels = encode_level_codes(codes)
    keys, block = joint_sum_to_level(values[keep], time[keep], unit_ids)

    assert labels is None, "integer codes are their own labels"
    assert keys == [(5, 3), (6, 3)]
    np.testing.assert_array_equal(block[0], [3.0, 30.0])   # aligned draws, not 2x the mean
    np.testing.assert_array_equal(block[1], [100.0, 100.0])
