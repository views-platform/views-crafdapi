"""Real-artifact served-output regression tripwire (C-136 cutover gate, C-295).

The synthetic golden (`test_served_output_golden.py`) pins served HDI/MAP shape on toy
data; this pins the *real* numbers CRAF'd receives, computed by the current estimator on a
deterministic slice of the delivered forecast run. A future change to the served numbers
trips this test locally.

The run is read from the disk cache's newest valid forecast slot (`apis/un_crafd/cache/
datasets/<partition>_forecast_value` + its `_meta.json` — what the API itself serves from),
mmap'd. The slice goes through the same calls the hdi-map routes make: `calculate_hdi_map` with
the route's defaults, then `to_consumer_columns`, then `dataframe_to_dict`.

The repository is public and the numbers are a partner delivery, so BOTH the run and the
generated golden live under the git-ignored `apis/` tree — this test runs locally (where the
run exists) and **skips in CI**. It is a local guard, not a CI gate.

The golden is keyed by run id: a newly delivered run generates its own golden on first sight
instead of failing against last month's numbers. So the guard says "the code still serves this
run's numbers", which is what a code change must not move. What it leaves unguarded: a change
that lands *before the first local run on a new delivery* is baked into that run's golden — run
it once when a delivery lands, before merging. (The synthetic golden guards every change in CI.)
Regenerate intentionally (a sanctioned re-baseline) by deleting the golden and re-running;
record the diff under ADR-023. Goldens of runs no longer cached can be deleted.
"""

import json
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer2_data

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DATASETS = _REPO_ROOT / "apis/un_crafd/cache/datasets"
_GOLDEN_DIR = _REPO_ROOT / "apis/un_crafd/cache/golden"

# Deterministic, bounded slice: the first month; whole countries, so the aggregated numbers are
# ones CRAF'd is actually served (a partial unit would not be). Chosen for conflict signal — a
# slice of all-zero cells would let most changes through. Codes absent from a run are dropped.
_COUNTRIES = ["AFG", "COD", "MLI", "NGA", "SDN", "SYR", "UKR", "YEM"]
# The pg view and the sub-national levels are restricted to these, to keep the golden compact.
_PG_COUNTRIES = ["SYR", "YEM"]

# Route defaults (`api.py` hdi-map handler).
_ALPHA = 0.9

# A run id becomes a filename; anything else is refused rather than written outside the golden dir.
_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def _slot_meta(value_dir):
    """The slot's meta as the API would accept it, else `(None, why)`.

    Mirrors `CrafdDiskCacheManager.read`: no meta is a miss (a crashed write, C-361), and a
    schema mismatch is a slot the API deletes rather than serves. A run id is required — the
    golden is keyed by it, and the partition name is the same for every run.
    """
    from views_crafdapi.managers.disk_cache import CACHE_SCHEMA_VERSION

    meta_path = value_dir.with_name(value_dir.name.removesuffix("_value") + "_meta.json")
    if not value_dir.is_dir() or not meta_path.exists():
        return None, f"no servable forecast slot at {value_dir} (value-dir or meta absent)"
    meta = json.loads(meta_path.read_text())
    if meta.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None, f"{meta_path.name}: schema_version is not {CACHE_SCHEMA_VERSION}; not servable"
    run_id = (meta.get("provenance") or {}).get("run_id")
    if not run_id or not _SAFE_RUN_ID.fullmatch(run_id):
        return None, f"{meta_path.name}: no usable run_id ({run_id!r})"
    return meta, None


def _current_slot():
    """`(value_dir, meta, None)` for the slot to pin, else `(None, None, why)`.

    The newest servable slot by write time. There is one slot per API-key partition, expired
    slots are kept for the last-good fallback (C-360), so an old key's slot can hold an old run:
    sorting by partition hash would pin whichever run hashes first.
    """
    override = os.environ.get("CRAFDAPI_REAL_FORECAST_VALUE")
    candidates = [Path(override)] if override else sorted(_CACHE_DATASETS.glob("*_forecast_value"))
    if not candidates:
        return None, None, f"no forecast value-dir under {_CACHE_DATASETS}; local-only guard"
    servable, why = [], None
    for value_dir in candidates:
        meta, why = _slot_meta(value_dir)
        if meta is not None:
            servable.append((meta.get("timestamp", 0), value_dir, meta))
    if not servable:
        return None, None, why
    _, value_dir, meta = max(servable, key=lambda t: t[0])
    return value_dir, meta, None


def _codes_within(ds, level, countries):
    """Every code of `level` that falls in `countries`, sorted — whole units only."""
    gm = ds.geo_metadata
    col = ds.levels[level]
    in_countries = gm[gm[ds.levels["country"]].isin(countries)]
    return sorted(in_countries[col].dropna().unique().tolist())


def _served_output_on_real_slice(value_dir, run_id):
    from views_crafdapi.data.handlers import ForecastDataset
    from views_crafdapi.forecast.serialize.json_contract import to_consumer_columns
    from views_crafdapi.managers.api import dataframe_to_dict

    ds = ForecastDataset.from_value(value_dir, mmap=True)
    month = int(sorted(ds._time_values)[0])
    present = set(ds.geo_metadata[ds.levels["country"]].dropna())
    countries = [c for c in _COUNTRIES if c in present]
    pg_countries = [c for c in _PG_COUNTRIES if c in present]
    assert countries and pg_countries, f"none of {_COUNTRIES} in this run; re-pick the slice"
    pg_cells = sorted(c for code in pg_countries for c in ds._get_pg_cells("country", code))

    # (label, level, entity_ids, aggregate). The pg view is what `/pg/...` sends: no level,
    # integer cell ids. With `level` set, `entity_ids` are level codes that the dataset expands
    # to cells — so cell ids must never be passed at an aggregate level.
    views = [
        ("pg", None, pg_cells, False),
        ("country", "country", countries, True),
        ("gaul0", "gaul0", _codes_within(ds, "gaul0", countries), True),
        ("gaul1", "gaul1", _codes_within(ds, "gaul1", pg_countries), True),
        ("gaul2", "gaul2", _codes_within(ds, "gaul2", pg_countries), True),
    ]
    out = {"_meta": {"run_id": run_id, "month": month, "countries": countries}}
    for label, level, entity_ids, aggregate in views:
        served = ds.calculate_hdi_map(
            alpha=_ALPHA,
            time_ids=[month],
            entity_ids=entity_ids,
            level=level,
            aggregate=aggregate,
        )
        out[label] = dataframe_to_dict(to_consumer_columns(served))
    return out


def test_served_output_matches_real_golden():
    value_dir, meta, why = _current_slot()
    if value_dir is None:
        pytest.skip(why)
    run_id = meta["provenance"]["run_id"]

    produced = _served_output_on_real_slice(value_dir, run_id)
    produced = json.loads(json.dumps(produced, sort_keys=True, default=str))

    # The test reads without the cache's lock. If the running API rewrote the slot meanwhile,
    # the numbers and the run id may belong to different runs — so neither write nor compare.
    if _slot_meta(value_dir)[0] != meta:
        pytest.skip(f"{value_dir.name} was rewritten during the test; re-run")

    golden_path = _GOLDEN_DIR / f"served_hdi_map_real_{run_id}.json"
    if not golden_path.exists():
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(produced, indent=2, sort_keys=True))
        pytest.skip(f"real golden generated ({golden_path.name}) — re-run to assert against it")

    golden = json.loads(golden_path.read_text())
    assert produced == golden, (
        "Served HDI/MAP on the real run changed vs the local golden. If this is a sanctioned "
        "re-baseline, delete the golden, regenerate, and record the ADR-023 diff; otherwise a "
        "change has silently altered the numbers served to CRAF'd (C-136)."
    )
