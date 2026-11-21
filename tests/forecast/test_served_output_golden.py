"""The served slice, pinned: any change to what a partner receives fails this test.

The golden holds the slice's JSON rows (`outcome_table.build_outcome_table`, written with the
same `to_json` call as the route) for one outcome at each level, from a fixed synthetic forecast
and history. A deliberate change to the served numbers regenerates it, with the change recorded
(ADR-023). Regenerate by deleting the golden file and running this test twice.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.serialize import outcome_table

pytestmark = pytest.mark.layer2_data

_GOLDEN = Path(__file__).parent / "golden" / "served_slice.json"
_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


def _months(df, months):
    cells = df.index.get_level_values("priogrid_id")
    old = df.index.get_level_values("month_id")
    mapping = dict(zip(sorted(set(old)), months))
    df = df.copy()
    df.index = pd.MultiIndex.from_arrays([[mapping[m] for m in old], cells], names=["month_id", "priogrid_id"])
    return df


def _served_rows():
    forecast = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=32, seed=4242, targets=_TARGETS))
    history = ForecastDataset(_months(make_fao_df(n_cells=4, n_months=2, seed=7, targets=_TARGETS), [598, 599]))
    return {
        level: json.loads(
            outcome_table.build_outcome_table(forecast, history, level, "sb").to_json(
                orient="records", double_precision=15
            )
        )
        for level in outcome_table.LEVELS
    }


def test_served_slice_matches_golden():
    produced = _served_rows()
    if not _GOLDEN.exists():
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(json.dumps(produced, indent=2, sort_keys=True))
        pytest.skip("golden generated: re-run to assert against it")
    assert produced == json.loads(_GOLDEN.read_text()), (
        "The served slice changed against the golden. If the change is deliberate, regenerate "
        "the golden and record it (ADR-023); otherwise a refactor changed what partners get."
    )
