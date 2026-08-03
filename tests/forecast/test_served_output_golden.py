"""Phase 4a (#112) characterization net: pin the exact served HDI/MAP output across all
levels so the facade refactor is provably byte-identical (C-139).

The golden is generated on first run and committed; thereafter any change to the served
numbers/shape fails this test. (The deliberate frame-native re-baseline is Phase 4b, gated
under ADR-023 — it will regenerate this golden with a recorded diff.)
"""

import json
from pathlib import Path

import pytest

from tests.conftest import make_fao_df
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.managers.api import dataframe_to_dict

pytestmark = pytest.mark.layer2_data

_GOLDEN = Path(__file__).parent / "golden" / "served_hdi_map.json"

# (label, level, aggregate) across the served surface.
_VIEWS = [
    ("pg", None, False),
    ("country", "country", True),
    ("gaul0", "gaul0", True),
    ("gaul1", "gaul1", True),
    ("gaul2", "gaul2", True),
]


def _canonical_served_output():
    ds = ForecastDataset(make_fao_df(n_cells=4, n_months=2, n_samples=32, seed=4242))
    out = {}
    for label, level, aggregate in _VIEWS:
        df = ds.calculate_hdi_map(alpha=0.9, level=level, aggregate=aggregate)
        # Capture index + columns exactly as served (reset_index so the geo unit is included).
        out[label] = dataframe_to_dict(df.reset_index())
    return out


def test_served_output_matches_golden():
    produced = _canonical_served_output()
    # Round-trip through JSON so the comparison is on the actually-serialized form.
    produced = json.loads(json.dumps(produced, sort_keys=True, default=str))

    if not _GOLDEN.exists():
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(json.dumps(produced, indent=2, sort_keys=True))
        pytest.skip("golden generated — re-run to assert against it")

    golden = json.loads(_GOLDEN.read_text())
    assert produced == golden, (
        "Served HDI/MAP output changed vs the golden snapshot. If this is the deliberate "
        "Phase-4b frame-native re-baseline, regenerate the golden and record the ADR-023 diff; "
        "otherwise the facade refactor is NOT byte-identical (C-139)."
    )
