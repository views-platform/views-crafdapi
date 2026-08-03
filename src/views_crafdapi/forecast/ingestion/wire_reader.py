"""Read a manifested Sampled-Forecast Wire Contract run and build a servable dataset.

S2 (#204) — the skeleton ingest. The forecast pipeline delivers a run to ``crafd_bucket``
as flat-columnar ``views_frames.io.arrow`` shards (one per ``(target, month)``) + one
gid-keyed GAUL **sidecar** + one **run manifest** uploaded last (the commit marker), per
ADR-013 §4. This module resolves that wire form into a ``ForecastDataset`` the existing
serving surface (HDI/MAP, posterior subsetting) can serve unchanged.

Scope fence (deliberately minimal — extension points marked inline):
  * S2: ONE small-S shard + the sidecar -> one servable ``ForecastDataset``; fail-safe (the
    caller falls back to legacy on any ingest failure) + the §4.5(b) ``sample``-ordering guard.
  * S3 (#205, done): the full §4.5 fail-LOUD integrity asserts now land here — (a) loaded S ==
    header ``sample_count``, (b) ``sample``-ordering, (c) shard + sidecar content-hash vs the
    manifest ``sha256`` (``verify_content_hash``, called from the service); plus the
    header-vs-manifest ``target``/``time_id`` cross-check and the coverage (§5.2) + completeness
    (``expected_cell_count``) asserts (promoted from S2 warnings). A violation raises → the
    fail-safe path serves legacy (loud in logs, safe for the endpoint).
  * S4 (#206, done): multi-shard/month/target assembly — ``build_dataset`` now stacks ALL of a
    run's shards into one servable dataset (union grid, one ``pred_<target>`` per target,
    cross-shard uniform-S, run-level coverage). A *documented capacity bound*
    (``estimate_assembled_bytes`` + the service guard) refuses oversized runs → fail-safe legacy;
    production-scale lazy per-month loading + the array-direct store are **S6 (#208)**.
  * Note: the served target keeps the wire vocabulary (``pred_lr_ged_sb``); mapping to the
    prefix-free FAO output schema (ADR-024/025) is a serving-layer concern, not S2.

Implementation note (S2 skeleton): we build the legacy object-cell source frame and hand it
to the proven ``ForecastDataset(...)`` constructor, so every construction invariant (C-155
alignment, dense-grid fill, geo collapse) is reused for free and the internal state is
byte-identical to the legacy path. S4 replaces the object-cell materialization with a direct
``(N, S)`` store for grid-scale memory (§4.6); for one small-S shard the round-trip is trivial.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import numpy as np
import pandas as pd

from views_crafdapi.data.value_format import _VALUE_SCHEMA_VERSION
from views_crafdapi.data.handlers import ForecastDataset

logger = logging.getLogger(__name__)


@dataclass
class RunManifest:
    """The Hop-B run manifest (ADR-013 §4.2) — the selection unit and shard index."""

    run_id: str
    contract_version: str
    targets: List[str]
    shards: List[Dict[str, Any]]  # each: {name, target, time_id, sha256}
    expected_months: List[int]
    expected_cell_count: Optional[int]
    sidecar_name: Optional[str]
    sidecar_sha256: Optional[str]
    raw: Dict[str, Any]

    @property
    def shard_names(self) -> List[str]:
        return [s["name"] for s in self.shards]

    def shard_entry(self, name: str) -> Dict[str, Any]:
        for s in self.shards:
            if s["name"] == name:
                return s
        raise KeyError(f"shard {name!r} not in manifest")

    def sha256_for(self, name: str) -> Optional[str]:
        return self.shard_entry(name).get("sha256")


def parse_run_manifest(raw: Union[bytes, str, Dict[str, Any]]) -> RunManifest:
    """Parse the run-manifest JSON (bytes/str from Appwrite, or an already-decoded dict)."""
    data = raw if isinstance(raw, dict) else json.loads(raw)
    sidecar = data.get("sidecar") or {}
    return RunManifest(
        run_id=data["run_id"],
        contract_version=data.get("contract_version", ""),
        targets=list(data.get("targets", [])),
        shards=list(data.get("shards", [])),
        expected_months=list(data.get("expected_months", [])),
        expected_cell_count=data.get("expected_cell_count"),
        sidecar_name=sidecar.get("name"),
        sidecar_sha256=sidecar.get("sha256"),
        raw=data,
    )


def load_shard_state(shard_bytes: bytes) -> Dict[str, Any]:
    """Load one arrow shard's bytes into the ``views_frames.io.arrow`` state dict:
    ``{"values": (N, S) float32, "time": (N,), "unit": (N,), "metadata": <§2 header>, ...}``.

    ``arrow.load`` takes a path, so the downloaded bytes are staged to a temp file.

    §4.5(b) ordering guard (enforced here, pulled forward from S3 after code review):
    ``arrow.load`` reconstructs ``(N, S)`` **positionally** and DISCARDS the parquet
    ``sample`` column, so a permuted or truncated ``sample`` column would silently place
    draws in the wrong slots (wrong ``sample_idx=`` results, invisible downstream). We read
    the raw ``sample`` column and assert it equals ``np.tile(np.arange(S), N)`` BEFORE
    trusting the reshaped values; a violation raises (the serving path then falls back to
    legacy rather than serving mis-slotted draws). S3 (#205) still owns the remaining
    asserts: S == manifest ``sample_count``, and shard content-hash vs the manifest.
    """
    import pyarrow.parquet as pq
    from views_frames.io import arrow as arrow_io

    tmp = tempfile.NamedTemporaryFile(suffix=".arrow.parquet", delete=False)
    try:
        tmp.write(shard_bytes)
        tmp.close()
        state = arrow_io.load(tmp.name)
        values = np.asarray(state["values"])
        n, s = (values.shape[0], values.shape[1]) if values.ndim == 2 else (len(state["time"]), 0)

        # §4.5(a) sample-count: the loaded S must equal the shard header's declared
        # sample_count (a shard whose array width disagrees with its own header is
        # malformed). Cross-shard uniformity of S across a run is S4 (#206).
        declared = (state.get("metadata") or {}).get("sample_count")
        if declared is not None and s != declared:
            raise ValueError(
                f"shard loaded S={s} disagrees with header sample_count={declared} "
                f"(ADR-013 §4.5a)"
            )

        # §4.5(b) ordering: arrow.load discards the `sample` column and reshapes positionally,
        # so a permuted/truncated column would mis-slot draws silently. Assert the raw column
        # equals tile(arange(S), N) before trusting the reshaped values.
        sample_col = pq.read_table(tmp.name, columns=["sample"]).column("sample").to_numpy()
        expected = np.tile(np.arange(s, dtype=np.int64), n)
        if sample_col.shape != expected.shape or not np.array_equal(sample_col.astype(np.int64), expected):
            raise ValueError(
                f"shard `sample` column is not cell-major tile(arange({s}), {n}) — draws would be "
                f"mis-slotted; refusing to trust the positional reshape (ADR-013 §4.5b)"
            )
        return state
    finally:
        os.unlink(tmp.name)


def verify_content_hash(data: bytes, expected_sha256: Optional[str], what: str) -> None:
    """§4.5(c): assert an artifact's bytes match the SHA-256 the run manifest pinned for it.

    A mismatch raises (the serving path then falls back to legacy rather than serving
    unverified/tampered bytes). ``expected_sha256=None`` means the manifest did not pin a
    hash for this artifact — we WARN (cannot verify) rather than fail, since hash *absence*
    is a manifest-completeness gap, not evidence of corruption."""
    if not expected_sha256:
        logger.warning("no sha256 in manifest for %s — skipping content-hash verification", what)
        return
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"{what} content hash {actual} != manifest sha256 {expected_sha256} — "
            f"refusing mismatched bytes (ADR-013 §4.5c)"
        )


def read_sidecar(sidecar_bytes: bytes) -> pd.DataFrame:
    """Read the GAUL sidecar parquet (ADR-013 §5.1): 10 columns, ``priogrid_id`` first,
    missing-geography rows PRESERVED with NaN/None (dropped only at aggregation, C-146)."""
    import io as _io

    return pd.read_parquet(_io.BytesIO(sidecar_bytes))


def estimate_assembled_bytes(n_targets: int, n_months: int, cells_per_month: int, sample_count: int) -> int:
    """§4.6 capacity estimate: the assembled ``_sample_store`` holds one ``(rows, S)`` float32
    block per target, where ``rows = n_months * cells_per_month`` (the dense (month, cell)
    grid). Used to refuse a run that would exceed the documented capacity bound *before*
    materializing it (production-scale lazy serving is S6/#208)."""
    return int(n_targets) * int(n_months) * int(cells_per_month) * int(sample_count) * 4


def _shard_axes(state: Dict[str, Any]) -> tuple:
    """Validate one shard's arrays and return (values (N,S) float32, time (N,), unit (N,), header)."""
    values = np.asarray(state["values"], dtype=np.float32)
    if values.ndim != 2:
        raise ValueError(f"wire shard values must be 2-D (N, S); got shape {values.shape}")
    time = np.asarray(state["time"])
    unit = np.asarray(state["unit"])
    n_rows = values.shape[0]
    if not (len(time) == len(unit) == n_rows):
        raise ValueError(
            f"wire shard axis mismatch: values N={n_rows}, time={len(time)}, unit={len(unit)}"
        )
    return values, time, unit, (state.get("metadata") or {})


def build_dataset(
    shard_states: List[Dict[str, Any]],
    sidecar_df: pd.DataFrame,
    manifest: RunManifest,
) -> ForecastDataset:
    """Assemble ONE servable ``ForecastDataset`` from ALL of a run's shards + the run sidecar.

    S4 (#206): each ``(target, month)`` shard becomes rows at its month for one ``pred_<target>``
    column; shards are stacked into the union ``(month_id, priogrid_id)`` grid with one
    ``pred_<target>`` column per target. The proven ``ForecastDataset`` constructor then
    dense-fills whole-missing (month, cell) rows, collapses geography, and builds the canonical
    per-target ``(N, S)`` store. Target-vs-target raggedness (a target absent from a populated
    row) is zero-filled here (the dense-grid fill only handles whole-missing rows).

    Integrity (S3, generalized to the run): per-shard header-vs-manifest ``target``/``time_id``
    + ``expected_cell_count``; cross-shard uniform S (§4.5a); run-level sidecar coverage (§5.2).
    A violation raises → the serving path (S2 fail-safe) serves legacy instead.
    """
    if not shard_states:
        raise ValueError("no shards to assemble")
    # Positional pairing below requires equal lengths; a mismatch would let zip() silently
    # truncate and assemble an INCOMPLETE run. Assert rather than trust the caller's order/count.
    if len(shard_states) != len(manifest.shards):
        raise ValueError(
            f"loaded {len(shard_states)} shard(s) but the manifest lists {len(manifest.shards)} "
            f"— refusing a mismatched run"
        )

    # Per-target {(month, cell): S-vector}, the union (month, cell) grid, and the S check.
    per_target: Dict[str, Dict[tuple, np.ndarray]] = {}
    all_rows: set = set()
    sample_count: Optional[int] = None

    # shard_states are appended in manifest.shard_names order by the service, so zip aligns
    # each loaded shard to its manifest entry (lengths asserted equal above).
    for state, entry in zip(shard_states, manifest.shards):
        values, time, unit, header = _shard_axes(state)
        n_rows, s = values.shape

        # Header-vs-manifest cross-check (S3): the manifest is the selection unit.
        if entry.get("target") is not None and header.get("target") not in (None, entry["target"]):
            raise ValueError(
                f"shard header target {header.get('target')!r} != manifest target "
                f"{entry['target']!r} (ADR-013 §4.5 header-vs-manifest)"
            )
        htime = header.get("time_id")
        if entry.get("time_id") is not None and htime is not None and htime != entry["time_id"]:
            raise ValueError(
                f"shard header time_id {htime} != manifest time_id {entry['time_id']} "
                f"(ADR-013 §4.5 header-vs-manifest)"
            )
        # Completeness (§5.2/§11.2): each shard covers the run's expected cell count.
        if manifest.expected_cell_count is not None and n_rows != manifest.expected_cell_count:
            raise ValueError(
                f"shard {entry.get('name')!r} has {n_rows} cells but the manifest expects "
                f"{manifest.expected_cell_count} — refusing an incomplete map (ADR-013 §5.2/§11.2)"
            )
        # Cross-shard uniform S (§4.5a): all targets in one dataset must share S.
        if sample_count is None:
            sample_count = s
        elif s != sample_count:
            raise ValueError(
                f"shard {entry.get('name')!r} has S={s} but the run's S={sample_count} — "
                f"targets/months must share sample_count (ADR-013 §4.5a)"
            )

        target = header.get("target") or entry.get("target")
        if target is None:
            raise ValueError(f"shard {entry.get('name')!r} declares no target")
        tmap = per_target.setdefault(target, {})
        for i in range(n_rows):
            key = (int(time[i]), int(unit[i]))
            if key in tmap:
                raise ValueError(
                    f"duplicate (target={target!r}, month={key[0]}, cell={key[1]}) across shards "
                    f"— manifest lists the same (target, month) twice"
                )
            tmap[key] = values[i]
            all_rows.add(key)

    zeros = np.zeros(sample_count, dtype=np.float32)

    # Run-level coverage (§5.2): every forecast cell must be in the sidecar gid-set.
    covered = set(int(u) for u in sidecar_df["priogrid_id"].to_numpy())
    uncovered = sorted({cell for (_m, cell) in all_rows if cell not in covered})
    if uncovered:
        raise ValueError(
            f"sidecar does not cover {len(uncovered)} forecast cell(s) (e.g. {uncovered[:5]}) — "
            f"refusing (their geography would be dropped at aggregation, ADR-013 §5.2)"
        )

    # Union index (sorted for determinism) + one pred_<target> column, zero-filling any
    # (month, cell) a target does not cover (dense-grid fill handles whole-missing rows, not
    # missing columns within a populated row).
    ordered = sorted(all_rows)
    index = pd.MultiIndex.from_tuples(ordered, names=["month_id", "priogrid_id"])
    data = {}
    for target, tmap in per_target.items():
        data[f"pred_{target}"] = [tmap.get(key, zeros) for key in ordered]
    source = pd.DataFrame(data, index=index)

    # Join the GAUL sidecar by priogrid_id (month-invariant; gids repeat across months).
    # reindex preserves rows whose geography is missing (NaN/None) — dropped at aggregation
    # (C-146), never here on read.
    priogrid = [cell for (_m, cell) in ordered]
    geo = sidecar_df.set_index("priogrid_id").reindex(priogrid)
    for col in ForecastDataset._METADATA_COLS:
        if col not in geo.columns:
            raise ValueError(f"sidecar is missing required GAUL column {col!r} (ADR-013 §5.1)")
        source[col] = geo[col].to_numpy()

    # The proven constructor: prediction-mode auto-detected, dense-grid fill (whole-missing
    # (month, cell) → zero S-vectors), geo collapse/backfill, per-target (N,S) store, C-155.
    return ForecastDataset(source)


class WireRunAssembler:
    """S6b-2 (#208): assemble a wire run into a ``ForecastDataset.to_value`` disk value-dir ONE
    MONTH at a time, so peak RAM is one month's ``ForecastDataset`` (+ one in-flight shard),
    never the whole run (~28.6 GB at full scale — the §4.6 ingest wall).

    Each month reuses the proven :func:`build_dataset` (a small single-month dataset); its
    ``_sample_store`` block is streamed into a preallocated ``np.lib.format.open_memmap`` ``(N, S)``
    per target. ``open_memmap`` writes a ``.npy`` byte-identical to ``np.save``, so the on-disk
    layout equals the whole-run ``to_value`` and is read back unchanged by
    :meth:`ForecastDataset.from_value` (``mmap=True``, S6b-1).

    Byte-identity to the whole-run store holds iff the run is **rectangular** (every month's cell
    set equals a reference set); the per-month ``append_month`` asserts this, which also preserves
    the C-87 last-month-coverage guarantee that a naive per-month loop would silently lose.
    """

    def __init__(self, out_dir: Union[str, Path], manifest: RunManifest, sidecar_df: pd.DataFrame):
        if not manifest.expected_months or manifest.expected_cell_count is None:
            raise ValueError("per-month assembly requires expected_months + expected_cell_count")
        self.out_dir = Path(out_dir)
        self.manifest = manifest
        self.sidecar_df = sidecar_df
        self.months = sorted(manifest.expected_months)
        self.cells_per_month = int(manifest.expected_cell_count)
        self.N = len(self.months) * self.cells_per_month
        # Canonical target COLUMN order = first-appearance across ALL shards — matches the
        # whole-run build_dataset (which iterates manifest.shards globally). Deriving it from
        # month 1 alone would (a) DROP a target that first appears in a later month and (b) can
        # reorder columns when the shard list is not month-1-first; both break byte-identity.
        seen: List[str] = []
        for entry in manifest.shards:
            t = entry.get("target")
            if t is not None and t not in seen:
                seen.append(t)
        self._vars: List[str] = [f"pred_{t}" for t in seen]
        self._memmaps: Optional[Dict[str, np.ndarray]] = None  # allocated on the first append
        self._S: Optional[int] = None
        self._row = 0
        self._time: List[int] = []
        self._unit: List[int] = []
        self._geo_parts: List[pd.DataFrame] = []
        self._cell_ref: Optional[set] = None
        self._scalars: Optional[Dict[str, Any]] = None

    def _allocate(self, ds: ForecastDataset) -> None:
        self._S = int(ds.sample_size)
        # manifest.json — mirrors ForecastDataset.to_value (drift caught by the assemble-both-ways
        # round-trip test). targets/pred_vars/original_columns use the canonical whole-run column
        # order (self._vars), NOT month 1's, so the served column order matches the whole-run path.
        self._scalars = {
            "value_schema_version": _VALUE_SCHEMA_VERSION,
            "is_prediction": ds.is_prediction,
            "preprocess_input": ds._preprocess_input_dataframe,
            "time_id": ds._time_id,
            "entity_id": ds._entity_id,
            "targets": list(self._vars),
            "pred_vars": list(self._vars),
            "features": list(ds.features),
            "sample_size": ds.sample_size,
            "broadcast_features": ds.broadcast_features,
            "fill_value": ds._fill_value,
            "original_columns": list(self._vars),
            "level": "pgm",
        }
        self._memmaps = {}
        for var in self._vars:
            fdir = self.out_dir / "frames" / var
            fdir.mkdir(parents=True, exist_ok=True)
            self._memmaps[var] = np.lib.format.open_memmap(
                fdir / "values.npy", mode="w+", dtype=np.float32, shape=(self.N, self._S)
            )

    def append_month(self, month_states: List[Dict[str, Any]],
                     month_shards: List[Dict[str, Any]], month: int) -> None:
        """Build this month's dataset (reusing :func:`build_dataset`), validate it, and stream
        its store block into the preallocated on-disk arrays."""
        sub = dataclasses.replace(self.manifest, shards=list(month_shards), expected_months=[month])
        ds = build_dataset(month_states, self.sidecar_df, sub)

        # Every month must carry EXACTLY the run's target set — else a target present only in
        # some months would be silently dropped (missing from self._vars) or KeyError. The
        # whole-run path zero-fills such gaps; per-month assembly refuses them (fail-safe legacy),
        # which is safer than serving a run with a silently-absent target.
        if set(ds.targets) != set(self._vars):
            raise ValueError(
                f"month {month} targets {sorted(ds.targets)} != run targets {sorted(self._vars)} "
                f"— every target must appear in every month (rectangular target × month grid)"
            )
        cell_set = {int(c) for c in ds.dataframe.index.get_level_values("priogrid_id")}
        if self._cell_ref is None:
            self._cell_ref = cell_set
        elif cell_set != self._cell_ref:
            raise ValueError(
                f"non-rectangular run: month {month}'s cell set differs from the reference "
                f"({len(cell_set)} vs {len(self._cell_ref)} cells) — refusing (preserves the "
                f"last-month-coverage guarantee, ADR-013 §5.2 / dense-grid C-87)"
            )
        # Fail-loud per month (both checks are per-row / month-invariant, §4.5/C-72).
        ds.validate_value_plausibility()
        ds.validate_metadata_plausibility()

        if self._memmaps is None:
            self._allocate(ds)

        rows_m = len(ds.dataframe)
        if rows_m != self.cells_per_month:
            raise ValueError(
                f"month {month} assembled {rows_m} rows, expected {self.cells_per_month}"
            )
        stop = self._row + rows_m
        for var in self._vars:
            self._memmaps[var][self._row:stop] = ds._sample_store[var]
        idx = ds.dataframe.index
        self._time.extend(int(t) for t in idx.get_level_values("month_id"))
        self._unit.extend(int(u) for u in idx.get_level_values("priogrid_id"))
        self._geo_parts.append(ds.geo_metadata)
        self._row = stop

    def finalize(self) -> int:
        """Write the remaining value-dir siblings (index/geo/manifest + per-frame identifiers/
        header) and return the row count N. Must be called after all months are appended."""
        if self._memmaps is None:
            raise ValueError("no months were assembled")
        if self._row != self.N:
            raise ValueError(f"assembled {self._row} rows but expected N={self.N}")
        for mm in self._memmaps.values():
            mm.flush()
        self._memmaps = None  # release the memmaps before writing the siblings

        time = np.array(self._time, dtype=np.int64)
        unit = np.array(self._unit, dtype=np.int64)
        np.savez(self.out_dir / "index.npz", time=time, unit=unit)
        pd.concat(self._geo_parts).to_parquet(self.out_dir / "geo.parquet")
        for var in self._vars:
            fdir = self.out_dir / "frames" / var
            np.savez(fdir / "identifiers.npz", time=time, unit=unit)
            (fdir / "header.json").write_text(
                json.dumps({"level": "pgm", "metadata": {}}, sort_keys=True, default=str)
            )
        (self.out_dir / "manifest.json").write_text(json.dumps(self._scalars, default=str))
        return self.N
