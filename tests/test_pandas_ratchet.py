"""The pandas-to-edges ratchet (epic #154 / ADR-030, S2).

A CI gate operationalizing the screaming-architecture acceptance signal: pandas
must live only at the two seams (ingestion decode, serialize encode) plus the
inherently-tabular geography table — never in the compute core, and never in a
*new* module. The migration slices remove entries from the allowlist; this gate
prevents back-sliding (a new pandas import fails) and hard-locks the compute core.

As `data/handlers.py` is dissolved (S3–S8), it drops out of the allowlist; the
gate stays green throughout because the importer set may only shrink.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer4_infra

_SRC = Path(__file__).resolve().parent.parent / "src" / "views_crafdapi"
_IMPORT_RE = re.compile(r"^\s*(?:import pandas\b|from pandas\b|import pandas as\b)", re.M)

# The frozen allowlist of modules permitted to import pandas (baseline 2026-06-27).
# It may only SHRINK as the migration proceeds. Edges + the (to-be-dissolved) spine.
_ALLOWED = frozenset({
    # top-level edges
    "client.py", "plotting.py",
    # the spine — target for removal across S3–S8
    "data/handlers/grid_dataset.py",
    "data/handlers/forecast_dataset.py",
    # manager edges (HTTP cache/serialize boundary)
    "managers/api.py", "managers/dataset_service.py", "managers/prediction/manager.py",
    # forecast INGEST seam (decode the wire)
    "forecast/ingestion/parquet_reader.py",
    "forecast/ingestion/wire_reader.py",  # S2 (#204): decode a manifested wire-contract run
    "forecast/ingestion/dense_grid.py",
    "forecast/ingestion/plausibility.py",
    # forecast SERIALIZE seam (emit JSON)
    "forecast/serialize/json_contract.py",
    "forecast/serialize/bulk_parquet.py",  # S6 (#222): the ADR-025 bulk artifact (serialize seam)
    # forecast GEOGRAPHY (inherently scalar/tabular metadata table)
    "forecast/geography/level_mapping.py",
    "forecast/geography/metadata_table.py",
})

# The compute core must NEVER import pandas — it operates on (N,S) frames/arrays.
_CLEAN_CORE = (
    "forecast/summarize",
    "forecast/aggregate",
    "forecast/frames",
    "forecast/conformance.py",
)


def _imports_pandas(path: Path) -> bool:
    return bool(_IMPORT_RE.search(path.read_text()))


def _pandas_importers() -> set[str]:
    return {
        str(p.relative_to(_SRC)).replace("\\", "/")
        for p in _SRC.rglob("*.py")
        if _imports_pandas(p)
    }


def test_pandas_stays_at_the_edges_ratchet():
    """No module outside the allowlist may import pandas (the set may only shrink)."""
    importers = _pandas_importers()
    new = importers - _ALLOWED
    assert not new, (
        f"New pandas importer(s) outside the edges: {sorted(new)}. "
        "Pandas must stay at the seams (ingestion/serialize/geography) — see ADR-030."
    )
    # Visibility: how far the spine still has to shrink.
    print(f"\n[ratchet] pandas importers: {len(importers)}/{len(_ALLOWED)} of allowlist")


def test_forecast_compute_core_is_pandas_free():
    """summarize/aggregate/frames/conformance compute on frames — zero pandas."""
    offenders = []
    for rel in _CLEAN_CORE:
        target = _SRC / rel
        files = [target] if target.suffix == ".py" else list(target.rglob("*.py"))
        offenders += [str(f.relative_to(_SRC)) for f in files if _imports_pandas(f)]
    assert not offenders, f"Compute core must be pandas-free; offenders: {offenders}"
