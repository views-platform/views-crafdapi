"""C-235: the aggregate reduction must call `collapse` per (month, variable), not per row.

`collapse` is vectorised over `(N, S)`. The cell-level path (S6b-1, #208) exploits that: it
reduces 2.33M rows in ~16 s with 108 calls. The aggregate path handed it a single row at a time
— 262,656 calls for the same run — which is why `/data/forecast/bulk` took ~500 s and returned
**504** at the proxy (#79), and why `/gaul2/...?aggregate=true` takes ~60 s.

This test pins the *shape* of the work rather than a wall-clock number: timings vary by machine
and would either be flaky or so loose they never fail. The call count is the thing that was
wrong, it is deterministic, and it is what a future edit would silently regress.
"""
import numpy as np
import pytest

import views_crafdapi.data.handlers.forecast_dataset as fd_module
from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset

pytestmark = pytest.mark.layer2_data


def _counting_collapse(monkeypatch):
    """Wrap the real `collapse`, recording how many times it is called and with how many rows."""
    original = fd_module.collapse
    calls = {"n": 0, "rows": 0}

    def counted(samples, **kwargs):
        arr = np.asarray(samples)
        calls["n"] += 1
        calls["rows"] += arr.shape[0] if arr.ndim > 1 else 1
        return original(samples, **kwargs)

    monkeypatch.setattr(fd_module, "collapse", counted)
    return calls


def test_aggregate_collapse_is_called_per_month_and_variable_not_per_row(monkeypatch):
    """The bound is O(months x variables). Anything proportional to the row count is C-235."""
    months, targets = 6, ("pred_lr_ged_sb", "pred_lr_ged_ns")
    ds = ForecastDataset(
        make_fao_df(n_cells=4, n_months=months, n_samples=16, seed=1, targets=targets)
    )

    calls = _counting_collapse(monkeypatch)
    out = ds.calculate_hdi_map(aggregate=True, level="gaul1", with_metadata=False)

    assert len(out) > 0, "the fixture produced no aggregated rows — the test would be vacuous"
    ceiling = months * len(targets)
    assert calls["n"] <= ceiling, (
        f"C-235: collapse() called {calls['n']} times for {len(out)} aggregated rows "
        f"({calls['rows'] / max(calls['n'], 1):.1f} rows per call). The reduction is vectorised "
        f"over (N, S); calling it per row makes the cost linear in units x months x targets. "
        f"Expected at most {ceiling} calls (one per month per variable)."
    )


def test_aggregate_collapse_batches_many_rows_per_call(monkeypatch):
    """The complement: not just few calls, but calls that actually carry the rows.

    A single call on a single row would satisfy a call-count bound on a one-row fixture. This
    fails that dodge by requiring the batch to be wider than one.
    """
    ds = ForecastDataset(
        make_fao_df(n_cells=4, n_months=4, n_samples=16, seed=2,
                    targets=("pred_lr_ged_sb",))
    )

    calls = _counting_collapse(monkeypatch)
    out = ds.calculate_hdi_map(aggregate=True, level="gaul1", with_metadata=False)

    rows_per_call = calls["rows"] / max(calls["n"], 1)
    assert rows_per_call > 1.0, (
        f"C-235: {rows_per_call:.1f} rows per collapse() call over {len(out)} aggregated rows — "
        f"the reduction is still being driven one row at a time."
    )
