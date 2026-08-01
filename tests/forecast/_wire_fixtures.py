"""Self-generated conformant Sampled-Forecast Wire Contract run, for S2 (#204) tests.

Per the #204 scope guard, S2 tests against self-generated conformant shards (``arrow.save``
+ the §2 header) rather than the vendored golden fixture (that is S8). This builds one
small-S run: one arrow shard (one target, one month), a 10-column GAUL sidecar with one
missing-geography (NaN) row, and a matching Hop-B run manifest — as in-memory bytes, so
tests can feed them through a mocked store manager exactly as Appwrite would return them.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from views_frames.io import arrow

_TARGET = "lr_ged_sb"
_TIME_ID = 543


@dataclass
class WireRun:
    run_id: str
    target: str
    time_id: int
    sample_count: int
    units: np.ndarray
    values: np.ndarray  # (N, S) float32
    shard_name: str
    shard_bytes: bytes
    sidecar_name: str
    sidecar_bytes: bytes
    manifest: Dict[str, Any]
    shard_docs: List[Dict[str, Any]] = field(default_factory=list)
    sidecar_docs: List[Dict[str, Any]] = field(default_factory=list)
    manifest_doc: Dict[str, Any] = field(default_factory=dict)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_wire_run(
    run_id: str = "run_0",
    n_cells: int = 6,
    sample_count: int = 4,
    target: str = _TARGET,
    time_id: int = _TIME_ID,
    with_nan_geo: bool = True,
) -> WireRun:
    """Build one conformant single-shard run as in-memory bytes + matching store docs."""
    n, s = n_cells, sample_count
    units = np.arange(100001, 100001 + n, dtype=np.int64)
    time = np.full(n, time_id, dtype=np.int64)
    # Deterministic values with global draw variance (so the no-collapse / plausibility
    # checks are exercised): row 0 is draw-degenerate zeros, the rest vary across draws.
    values = np.zeros((n, s), dtype=np.float32)
    for i in range(1, n):
        values[i] = np.linspace(i, i + 1.5, s, dtype=np.float32)

    header = {
        "contract_version": "1.5",
        "frame_type": "prediction",
        "representation": "samples",
        "sample_count": s,
        "dtype": "float32",
        "spatial_level": "pgm",
        "target": target,
        "time_id": time_id,
        "run_id": run_id,
        "generated_at": "2026-07-20T00:00:00Z",
        "id_semantics": {"time": "views_month_id", "unit": "priogrid_id"},
        "provenance": {"ensemble": "fixture_ensemble", "pipeline_core_version": "0.0.0", "reconciled": False},
        "sharding": {"scheme": "per_month", "index": 0, "count": 1},
    }
    tmp = Path(tempfile.mktemp(suffix=".arrow.parquet"))
    arrow.save(tmp, values=values, time=time, unit=units, level="pgm", metadata=header)
    shard_bytes = tmp.read_bytes()
    tmp.unlink()
    shard_name = f"{run_id}__{target}__m{time_id:06d}.arrow.parquet"

    # 10-column sidecar (§5.1): priogrid_id first; *_code float64; *_name/iso3 strings.
    # Last row missing-geo (NaN/None), preserved on read.
    _iso = ["NGA", "KEN", "ETH", "SOM", "SSD", "TCD", "MLI", "NER"]
    sidecar = pd.DataFrame(
        {
            "priogrid_id": units.astype("int64"),
            "pg_xcoord": np.linspace(-10.0, 10.0, n),
            "pg_ycoord": np.linspace(-5.0, 5.0, n),
            "country_iso_a3": [_iso[i % len(_iso)] for i in range(n)],
            "admin1_gaul1_code": np.arange(n, dtype="float64"),
            "admin1_gaul1_name": [f"adm1_{i}" for i in range(n)],
            "admin1_gaul0_code": np.arange(n, dtype="float64"),
            "admin1_gaul0_name": [f"adm0_{i}" for i in range(n)],
            "admin2_gaul2_code": np.arange(n, dtype="float64"),
            "admin2_gaul2_name": [f"adm2_{i}" for i in range(n)],
        }
    )
    if with_nan_geo:
        for col in ["country_iso_a3", "admin1_gaul1_name", "admin1_gaul0_name", "admin2_gaul2_name"]:
            sidecar.loc[n - 1, col] = None
        for col in ["admin1_gaul1_code", "admin1_gaul0_code", "admin2_gaul2_code"]:
            sidecar.loc[n - 1, col] = np.nan
    buf = io.BytesIO()
    sidecar.to_parquet(buf)
    sidecar_bytes = buf.getvalue()
    sidecar_name = f"{run_id}__sidecar.parquet"

    manifest = {
        "contract_version": "1.5",
        "run_id": run_id,
        "targets": [target],
        "shards": [{"name": shard_name, "target": target, "time_id": time_id, "sha256": _sha256(shard_bytes)}],
        "expected_months": [time_id],
        "expected_cell_count": n,
        "sidecar": {"name": sidecar_name, "sha256": _sha256(sidecar_bytes)},
    }

    # Store documents as get_predictions_by_metadata would return them (newest-first).
    manifest_doc = {"fileId": "mani_1", "filename": f"{run_id}__manifest.json",
                    "type": "sampled_forecast_manifest", "category": "forecast", "name": "un_fao"}
    shard_docs = [{"fileId": "shard_1", "filename": shard_name,
                   "type": "sampled_forecast_shard", "category": "forecast", "name": "un_fao"}]
    sidecar_docs = [{"fileId": "sidecar_1", "filename": sidecar_name,
                     "type": "sampled_forecast_sidecar", "category": "forecast", "name": "un_fao"}]

    return WireRun(
        run_id=run_id, target=target, time_id=time_id, sample_count=s, units=units, values=values,
        shard_name=shard_name, shard_bytes=shard_bytes, sidecar_name=sidecar_name,
        sidecar_bytes=sidecar_bytes, manifest=manifest, shard_docs=shard_docs,
        sidecar_docs=sidecar_docs, manifest_doc=manifest_doc,
    )


def _build_sidecar(units: np.ndarray, run_id: str, with_nan_geo: bool = True):
    """Build a §5.1 sidecar (10 cols, priogrid_id first) covering `units`; last row NaN geo."""
    n = len(units)
    _iso = ["NGA", "KEN", "ETH", "SOM", "SSD", "TCD", "MLI", "NER"]
    sidecar = pd.DataFrame(
        {
            "priogrid_id": units.astype("int64"),
            "pg_xcoord": np.linspace(-10.0, 10.0, n),
            "pg_ycoord": np.linspace(-5.0, 5.0, n),
            "country_iso_a3": [_iso[i % len(_iso)] for i in range(n)],
            "admin1_gaul1_code": np.arange(n, dtype="float64"),
            "admin1_gaul1_name": [f"adm1_{i}" for i in range(n)],
            "admin1_gaul0_code": np.arange(n, dtype="float64"),
            "admin1_gaul0_name": [f"adm0_{i}" for i in range(n)],
            "admin2_gaul2_code": np.arange(n, dtype="float64"),
            "admin2_gaul2_name": [f"adm2_{i}" for i in range(n)],
        }
    )
    if with_nan_geo:
        for col in ["country_iso_a3", "admin1_gaul1_name", "admin1_gaul0_name", "admin2_gaul2_name"]:
            sidecar.loc[n - 1, col] = None
        for col in ["admin1_gaul1_code", "admin1_gaul0_code", "admin2_gaul2_code"]:
            sidecar.loc[n - 1, col] = np.nan
    buf = io.BytesIO()
    sidecar.to_parquet(buf)
    return f"{run_id}__sidecar.parquet", buf.getvalue()


@dataclass
class MultiShardRun:
    run_id: str
    targets: List[str]
    months: List[int]
    units: np.ndarray
    sample_count: int
    values: Dict[tuple, np.ndarray]  # (target, month, unit) -> (S,) vector
    shard_bytes: Dict[str, bytes]    # filename -> bytes
    sidecar_name: str
    sidecar_bytes: bytes
    manifest: Dict[str, Any]
    manifest_doc: Dict[str, Any]
    shard_docs: List[Dict[str, Any]]
    sidecar_docs: List[Dict[str, Any]]


def make_multi_shard_run(
    run_id: str = "run_m",
    targets=("lr_ged_sb", "lr_ged_ns"),
    months=(543, 544),
    n_cells: int = 6,
    sample_count: int = 4,
    with_nan_geo: bool = True,
) -> MultiShardRun:
    """A conformant multi-(target, month) run: one shard per (target, month), one sidecar
    covering all cells, one manifest listing them all — as in-memory bytes + store docs."""
    targets, months = list(targets), list(months)
    s = sample_count
    units = np.arange(100001, 100001 + n_cells, dtype=np.int64)
    values: Dict[tuple, np.ndarray] = {}
    shard_entries: List[Dict[str, Any]] = []
    shard_bytes: Dict[str, bytes] = {}
    shard_docs: List[Dict[str, Any]] = []

    for ti, target in enumerate(targets):
        for month in months:
            vals = np.zeros((n_cells, s), dtype=np.float32)
            for c in range(n_cells):
                base = (ti + 1) * 100.0 + (month - months[0]) * 10.0 + c
                vals[c] = np.linspace(base, base + 1.0, s, dtype=np.float32)
                values[(target, int(month), int(units[c]))] = vals[c]
            time = np.full(n_cells, month, dtype=np.int64)
            header = {
                "contract_version": "1.5", "frame_type": "prediction", "representation": "samples",
                "sample_count": s, "dtype": "float32", "spatial_level": "pgm", "target": target,
                "time_id": int(month), "run_id": run_id, "generated_at": "2026-07-20T00:00:00Z",
                "id_semantics": {"time": "views_month_id", "unit": "priogrid_id"},
                "provenance": {"ensemble": "fixture_ensemble", "pipeline_core_version": "0.0.0", "reconciled": False},
                "sharding": {"scheme": "per_month", "index": months.index(month), "count": len(months)},
            }
            tmp = Path(tempfile.mktemp(suffix=".arrow.parquet"))
            arrow.save(tmp, values=vals, time=time, unit=units, level="pgm", metadata=header)
            b = tmp.read_bytes()
            tmp.unlink()
            name = f"{run_id}__{target}__m{month:06d}.arrow.parquet"
            shard_bytes[name] = b
            shard_entries.append({"name": name, "target": target, "time_id": int(month), "sha256": _sha256(b)})
            shard_docs.append({"fileId": f"shard_{len(shard_docs)}", "filename": name,
                               "type": "sampled_forecast_shard", "category": "forecast", "name": "un_fao"})

    sidecar_name, sidecar_bytes = _build_sidecar(units, run_id, with_nan_geo)
    manifest = {
        "contract_version": "1.5", "run_id": run_id, "targets": targets,
        "shards": shard_entries, "expected_months": sorted(set(months)), "expected_cell_count": n_cells,
        "sidecar": {"name": sidecar_name, "sha256": _sha256(sidecar_bytes)},
    }
    manifest_doc = {"fileId": "mani_1", "filename": f"{run_id}__manifest.json",
                    "type": "sampled_forecast_manifest", "category": "forecast", "name": "un_fao"}
    sidecar_docs = [{"fileId": "sidecar_1", "filename": sidecar_name,
                     "type": "sampled_forecast_sidecar", "category": "forecast", "name": "un_fao"}]
    return MultiShardRun(
        run_id=run_id, targets=targets, months=months, units=units, sample_count=s, values=values,
        shard_bytes=shard_bytes, sidecar_name=sidecar_name, sidecar_bytes=sidecar_bytes,
        manifest=manifest, manifest_doc=manifest_doc, shard_docs=shard_docs, sidecar_docs=sidecar_docs,
    )
