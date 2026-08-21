"""Stream a dense historical artifact straight into a disk value-dir (C-263 / #98).

The cold-start problem this exists for
--------------------------------------
The historical leg used to build its dataset the obvious way: decode the whole parquet into
one pandas frame, hand that frame to ``ForecastDataset``, let the constructor copy out of it,
then serialise the result. Measured on the real 28.4M-row artifact that costs ~9.8 GB above
baseline, because the decoded source frame and the constructor's own copies are resident **at
the same time** — a peak no amount of freeing afterwards can lower. On the production box that
is a 16.8 GB cold start against a 6.0 GB steady state, which is what makes C-262's memory
ceiling unsatisfiable at either value.

This module removes the co-residency instead of the copies: it never materialises the source
frame at all. Row group by row group, it writes each target's ``(N,1)`` float64 block into a
preallocated ``np.lib.format.open_memmap`` and appends the geography rows to an open
``ParquetWriter``. Peak is one row group plus the file-backed blocks, and the dataset is then
read back memory-mapped by the existing ``from_value`` path. It is the same shape as
``wire_reader.WireRunAssembler``, which does exactly this for the forecast leg.

Why this is allowed to be byte-identical, and how it is kept honest
------------------------------------------------------------------
Streaming in file order is only correct if file order already **is** ``sort_index()`` order and
the grid is already dense — otherwise the fill and the sort the constructor performs are real
work, not no-ops, and skipping them changes the output. The producer's artifact satisfies both
(439 months x 64,742 cells, month-ascending, cell-ascending within each month, the identical
cell vector every month), but this module does not assume it: :func:`stream_to_value` **verifies
the precondition as it reads** and raises :class:`NotStreamable` the moment it fails. The caller
catches that and falls back to the in-memory path, whose behaviour is unchanged.

That ordering is the whole safety argument, so it is checked per row group rather than sampled:
a violation anywhere means the value-dir would carry rows in an order the current code would not
have produced, and the served numbers would move with no error.

ADR-030 note. §1 keeps the historical/scalar leg on the pandas path and requires it to stay
**float64** — the frame leaf accumulates in float32 and compounds error across cells (register
C-258, caught after a green suite). This module honours that literally: it writes float64 blocks
and performs no arithmetic whatsoever. It moves *where the bytes are assembled*, not what they
are. §8 scopes the historical leg out of the representation migration; that scoping predates the
cold-start measurement and is worth revisiting, but nothing here changes the representation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional, Sequence, Union

import numpy as np
import pandas as pd
import pyarrow as pa
# `pyarrow.compute` is a submodule: `import pyarrow` does NOT bind it. It was reachable here only
# because pandas imports it for its own Arrow interop — verified, `import pyarrow, pyarrow.parquet`
# alone leaves `pa.compute` undefined. Relying on that would have failed as an AttributeError
# swallowed by the caller's fallback, silently reverting to the path this module exists to avoid.
import pyarrow.compute as pc
import pyarrow.parquet as pq

from views_crafdapi.data.value_format import _VALUE_SCHEMA_VERSION
from views_crafdapi.forecast.ingestion.plausibility import assert_geo_metadata_plausible

logger = logging.getLogger(__name__)

#: Index level names the streamed path understands. Anything else is not streamable.
_TIME_ID = "month_id"
_ENTITY_ID = "priogrid_id"
#: The upstream vocabulary shim (C-61): 774 platform parquet files bake `priogrid_gid` into
#: their Arrow schema. Accepted on read and normalised, exactly as `_init_dataframe` does.
_ENTITY_ALIASES = (_ENTITY_ID, "priogrid_gid")


class ImplausibleArtifact(Exception):
    """The artifact streamed cleanly but its geography violates C-72.

    Distinct from :class:`NotStreamable` because the two need opposite responses. "I cannot
    stream this" should fall back to the in-memory path; "this data is invalid" must **not** —
    the in-memory path would reach the same verdict after paying the full 12.2 GB peak, and
    C-72 requires the request to fail loud either way.

    The check runs per chunk, before anything is committed. It used to run on the reconstructed
    dataset *after* ``write_value_dir`` had already replaced the cache slot — so a bad artifact
    evicted the last good one, the request 500'd, and the **next** request took the disk-cache
    branch, which does not re-validate, and served the implausible geography.
    """


class NotStreamable(Exception):
    """The artifact does not satisfy the streaming precondition.

    Raised — never swallowed here — so the caller falls back to the in-memory constructor
    rather than writing a value-dir whose row order the current code would not have produced.
    """


def _entity_column(names: Sequence[str]) -> Optional[str]:
    for alias in _ENTITY_ALIASES:
        if alias in names:
            return alias
    return None


def assert_plausible_chunk(geo_df: pd.DataFrame, rg: int) -> None:
    """Run the C-72 geography check on one chunk, before any of it can be committed.

    Both checks `assert_geo_metadata_plausible` makes — coordinate ranges and ISO3 shape — are
    row-local, so chunk-wise is equivalent to whole-table and fails on the first bad row group.
    """
    try:
        assert_geo_metadata_plausible(geo_df)
    except ValueError as exc:
        raise ImplausibleArtifact(f"row group {rg}: {exc}") from exc


def stream_to_value(
    source: Union[str, Path],
    out_dir: Union[str, Path],
    targets: Sequence[str],
    metadata_cols: Sequence[str],
    categorical_cols: Sequence[str],
    fill_value: float = 0,
) -> int:
    """Assemble the value-dir for a dense historical artifact without decoding it whole.

    ``source`` is a parquet path. Returns the row count written. Raises :class:`NotStreamable`
    if the artifact is not already dense-and-sorted, in which case ``out_dir`` may hold a
    partial result and must be discarded by the caller.

    The layout written is the one :meth:`ForecastDataset.to_value` produces for a
    non-prediction dataset — ``features/<col>.npy`` (float64), ``geo.parquet``, ``index.npz``,
    ``manifest.json`` — so :meth:`ForecastDataset.from_value` reads it back unchanged.
    """
    source = Path(source)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pf = pq.ParquetFile(source)
    schema_names = list(pf.schema_arrow.names)
    entity_col = _entity_column(schema_names)
    if entity_col is None or _TIME_ID not in schema_names:
        raise NotStreamable(
            f"artifact has no ({_TIME_ID}, {'/'.join(_ENTITY_ALIASES)}) columns: {schema_names}"
        )
    if entity_col != _ENTITY_ID:
        # Mirror `_init_dataframe`'s WARNING for the C-61 vocabulary shim, so the same upstream
        # condition is observable whichever path ingested the artifact.
        logger.warning("artifact entity column is %r, normalising to %r", entity_col, _ENTITY_ID)
    missing_targets = [t for t in targets if t not in schema_names]
    if missing_targets:
        raise NotStreamable(f"targets absent from the artifact: {missing_targets}")
    missing_meta = [c for c in metadata_cols if c not in schema_names]
    if missing_meta:
        raise NotStreamable(f"geo metadata columns absent from the artifact: {missing_meta}")

    # CR-4: the in-memory path moves every remaining scalar column into `_feature_store` and
    # records it in the manifest; this one reads only the named targets. If the artifact carries a
    # value column `targets` omits, the two paths would disagree on the same input, so refuse.
    value_cols = [
        c for c in schema_names
        if c not in {_TIME_ID, *_ENTITY_ALIASES} and c not in set(metadata_cols)
    ]
    extra = [c for c in value_cols if c not in set(targets)]
    if extra:
        raise NotStreamable(
            f"artifact carries value column(s) {extra} outside the requested targets — the "
            f"in-memory path would keep them as features and this one would drop them"
        )

    n_rows = pf.metadata.num_rows
    if n_rows == 0:
        raise NotStreamable("artifact is empty")

    # Pass 1 — the category vocabulary, so every chunk encodes against ONE ordered category set.
    # This is not cosmetic: `groupby(observed=True)` on a categorical key emits groups in
    # *category* order, so the served row order at `/country/...` depends on it. `astype("category")`
    # on the whole column (what the in-memory path does) sorts the distinct values, so sorting the
    # union here reproduces it exactly. One row group is resident at a time.
    categories: Dict[str, set] = {c: set() for c in categorical_cols if c in schema_names}
    if categories:
        for rg in range(pf.metadata.num_row_groups):
            chunk = pf.read_row_group(rg, columns=list(categories))
            for col in categories:
                vals = pc.unique(chunk.column(col).combine_chunks())
                categories[col].update(v for v in vals.to_pylist() if v is not None)
    ordered_categories = {c: sorted(v) for c, v in categories.items()}

    # Preallocate one float64 (N,1) block per target — the exact dtype and shape `to_value`
    # writes for the scalar path (C-258: float64, never float32).
    feat_dir = out_dir / "features"
    feat_dir.mkdir(parents=True, exist_ok=True)
    blocks = {
        t: np.lib.format.open_memmap(
            feat_dir / f"{t}.npy", mode="w+", dtype=np.float64, shape=(n_rows, 1)
        )
        for t in targets
    }
    time_out = np.empty(n_rows, dtype=np.int64)
    unit_out = np.empty(n_rows, dtype=np.int64)

    geo_writer: Optional[pq.ParquetWriter] = None
    row = 0
    prev_month: Optional[int] = None
    prev_cell: int = -1
    read_cols = [_TIME_ID, entity_col] + list(targets) + list(metadata_cols)

    try:
        for rg in range(pf.metadata.num_row_groups):
            chunk = pf.read_row_group(rg, columns=read_cols)
            # int64 before any differencing: `to_numpy()` preserves the parquet dtype, and on an
            # unsigned column `np.diff` wraps a descending step into a large positive one — the
            # ordering guard would then pass on exactly the input it exists to reject (CR-6).
            months = chunk.column(_TIME_ID).to_numpy().astype(np.int64)
            cells = chunk.column(entity_col).to_numpy().astype(np.int64)
            n = len(months)
            if n == 0:
                continue

            # Ordering precondition, checked on every row group rather than sampled.
            if prev_month is not None and months[0] < prev_month:
                raise NotStreamable(
                    f"row group {rg} starts at month {months[0]} after month {prev_month} — "
                    f"file order is not month-ascending, so it is not sort_index() order"
                )
            # CR-2: a month SPANS row groups — at 64,742 cells against ~1M-row groups that is the
            # normal case, not an edge one. Comparing only months at the boundary and only cells
            # within a chunk left the seam unchecked, so a month written upper-half-first passed
            # every guard (density and C-87 included) and produced a value-dir in an order
            # `sort_index()` would never emit. Carry the last cell across the boundary too.
            if prev_month is not None and months[0] == prev_month and cells[0] <= prev_cell:
                raise NotStreamable(
                    f"row group {rg} resumes month {months[0]} at cell {cells[0]}, which does not "
                    f"ascend from cell {prev_cell} where the previous row group left it — file "
                    f"order is not sort_index() order"
                )
            if np.any(np.diff(months) < 0):
                raise NotStreamable(f"row group {rg} is not month-ascending internally")
            # Within each month present in this chunk, cells must ascend strictly.
            edges = np.flatnonzero(np.diff(months)) + 1
            starts = np.concatenate(([0], edges))
            ends = np.concatenate((edges, [n]))
            for s, e in zip(starts, ends):
                seg = cells[s:e]
                if seg.size > 1 and np.any(np.diff(seg) <= 0):
                    raise NotStreamable(
                        f"row group {rg}: cells within month {months[s]} are not strictly "
                        f"ascending — file order is not sort_index() order"
                    )

            for t in targets:
                blocks[t][row : row + n, 0] = chunk.column(t).to_numpy(zero_copy_only=False)
            time_out[row : row + n] = months
            unit_out[row : row + n] = cells

            # `.to_pandas()` on a column returns a Series carrying its own RangeIndex, and
            # `pd.DataFrame({col: series}, index=...)` ALIGNS on that index rather than treating
            # it as positional — against a MultiIndex nothing matches and every value silently
            # becomes NaN. Hand pandas raw arrays so the assignment stays positional.
            geo_df = pd.DataFrame(
                {c: chunk.column(c).to_pandas().to_numpy() for c in metadata_cols},
                index=pd.MultiIndex.from_arrays(
                    [months, cells], names=[_TIME_ID, _ENTITY_ID]
                ),
            )
            # CR-3: `ForecastDataset.__init__` overwrites EVERY row of an entity that has any
            # null geo column with that entity's first non-null values. Streaming writes each
            # chunk verbatim, so a partially-null cell would give a different geo table — and a
            # different cell set at aggregation — depending on which path ingested it. Refuse
            # rather than reimplement the backfill: the real artifact has none (measured, 0
            # missing), so this costs nothing and cannot drift from the constructor's rule.
            null_cols = [c for c in metadata_cols if geo_df[c].isna().any()]
            if null_cols:
                raise NotStreamable(
                    f"row group {rg} has null geo metadata in {null_cols} — the in-memory path "
                    f"would backfill those rows per entity and this one would not"
                )
            assert_plausible_chunk(geo_df, rg)
            for col, cats in ordered_categories.items():
                geo_df[col] = pd.Categorical(geo_df[col], categories=cats)
            table = pa.Table.from_pandas(geo_df, preserve_index=True)
            if geo_writer is None:
                geo_writer = pq.ParquetWriter(out_dir / "geo.parquet", table.schema)
            geo_writer.write_table(table)
            del geo_df, table, chunk

            prev_month = int(months[-1])
            prev_cell = int(cells[-1])
            row += n
    finally:
        if geo_writer is not None:
            geo_writer.close()
        for block in blocks.values():
            block.flush()
        blocks.clear()

    if row != n_rows:
        raise NotStreamable(f"streamed {row} rows but the artifact declares {n_rows}")

    # Density, verified from the streamed index rather than assumed. The in-memory path would
    # have dense-filled here; if anything is missing, the two paths would disagree.
    unique_months = np.unique(time_out)
    unique_cells = np.unique(unit_out)
    if len(unique_months) * len(unique_cells) != n_rows:
        raise NotStreamable(
            f"grid is not dense: {len(unique_months)} months x {len(unique_cells)} cells "
            f"!= {n_rows} rows — the in-memory path would fill it, this one would not"
        )
    # C-87: every entity must be present in the last time step, else the dense fill would have
    # dropped it and the constructor would have refused. Density above plus an equal per-month
    # count is the same guarantee, reached without materialising the grid.
    last_month_cells = unit_out[time_out == unique_months[-1]]
    if len(last_month_cells) != len(unique_cells):
        raise NotStreamable(
            "the last time step does not carry every entity — the in-memory path would refuse "
            "this artifact (C-87)"
        )

    np.savez(out_dir / "index.npz", time=time_out, unit=unit_out)

    # The manifest `to_value` writes for a non-prediction dataset. `targets` is the source
    # column order; `features` is empty because every non-index column IS a target on this leg;
    # `pred_vars` is empty because no column is `pred_*`.
    manifest = {
        "value_schema_version": _VALUE_SCHEMA_VERSION,
        "is_prediction": False,
        "preprocess_input": True,
        "time_id": _TIME_ID,
        "entity_id": _ENTITY_ID,
        "targets": list(targets),
        "pred_vars": [],
        "features": [],
        "sample_size": None,
        "broadcast_features": False,
        "fill_value": fill_value,
        "original_columns": list(targets),
        "level": "pgm",
        "feature_columns": list(targets),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, default=str))

    logger.info(
        "Streamed historical value-dir: %d rows, %d months x %d cells, %d target(s)",
        n_rows, len(unique_months), len(unique_cells), len(targets),
    )
    return n_rows
