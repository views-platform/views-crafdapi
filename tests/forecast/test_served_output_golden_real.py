"""Real-artifact served-output regression tripwire (C-136 cutover gate).

The synthetic golden (`test_served_output_golden.py`) pins served HDI/MAP shape on toy
data; this pins the *real* numbers FAO receives, computed by the current (v2 tower)
estimator on a deterministic slice of a real cached forecast artifact. A future change
to the served numbers trips this test locally.

Per the public-release "no real data committed" convention (#123) and the maintainer's
choice, BOTH the artifact and the generated golden live under the git-ignored
`appwrite_cache/` tree — so this test runs locally (where the artifact exists) and
**skips in CI**. It is a local guard, not a CI gate; the committed ADR-023 before/after
summary (`reports/views_frames_integration/rebaseline_diff_2026-06-28.md`) is the
shareable evidence.

Regenerate intentionally (e.g. a sanctioned re-baseline) by deleting the golden and
re-running; record the diff under ADR-023.
"""

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer2_data

_REAL_CACHE = Path(
    os.environ.get(
        "FAOAPI_REAL_CACHE",
        "appwrite_cache/unfao_bucket/forecast_dataset_20260310_114703.parquet",
    )
)
_GOLDEN = Path("appwrite_cache/golden/served_hdi_map_real.json")

# Deterministic, bounded slice: one month, a fixed block of priogrid cells. Small enough
# to be fast and to keep the (git-ignored) golden compact, real enough to exercise the
# pg-level serve + country/GAUL aggregation on genuine posteriors.
_SUBSET_CELLS = 300

# (label, level, aggregate) across the served surface.
_VIEWS = [
    ("pg", None, False),
    ("country", "country", True),
    ("gaul0", "gaul0", True),
    ("gaul1", "gaul1", True),
    ("gaul2", "gaul2", True),
]


def _served_output_on_real_slice():
    import pandas as pd

    from views_faoapi.data.handlers import FAO_PGMDataset
    from views_faoapi.managers.api import dataframe_to_dict

    df = pd.read_parquet(_REAL_CACHE)
    month = sorted(df.index.get_level_values("month_id").unique())[0]
    in_month = df.index.get_level_values("month_id") == month
    sub = df[in_month]
    cells = sorted(sub.index.get_level_values("priogrid_id").unique())[:_SUBSET_CELLS]
    sub = sub[sub.index.get_level_values("priogrid_id").isin(cells)]

    ds = FAO_PGMDataset(sub)
    out = {"_meta": {"month": int(month), "n_cells": len(cells)}}
    for label, level, aggregate in _VIEWS:
        served = ds.calculate_hdi_map(alpha=0.9, level=level, aggregate=aggregate)
        out[label] = dataframe_to_dict(served.reset_index())
    return out


@pytest.mark.skipif(
    not _REAL_CACHE.exists(),
    reason=f"real forecast artifact absent ({_REAL_CACHE}); local-only guard",
)
def test_served_output_matches_real_golden():
    produced = _served_output_on_real_slice()
    produced = json.loads(json.dumps(produced, sort_keys=True, default=str))

    if not _GOLDEN.exists():
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(json.dumps(produced, indent=2, sort_keys=True))
        pytest.skip("real golden generated — re-run to assert against it")

    golden = json.loads(_GOLDEN.read_text())
    assert produced == golden, (
        "Served HDI/MAP on the real artifact changed vs the local golden. If this is a "
        "sanctioned re-baseline, delete the golden, regenerate, and record the ADR-023 diff; "
        "otherwise a change has silently altered the numbers served to FAO (C-136)."
    )
