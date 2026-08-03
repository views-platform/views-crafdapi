# Class Intent Contract: ForecastDataset

**Status:** Active
**Owner:** Project maintainers
**Last reviewed:** 2026-06-26
**Related ADRs:** ADR-006 (this contract), ADR-021 (fill semantics), ADR-023 (re-baselining), ADR-024 (raw counts), ADR-025 (output schema); register C-81/C-87/C-136..C-142
**Supersedes (as the public leaf):** `ForecastDataset.md` (the class was renamed `ForecastDataset → ForecastDataset` in Phase 4a of #87; `ForecastDataset` is retained as a back-compat alias). See also `_GridDataset.md` (the renamed base class).

---

## 1. Purpose

`ForecastDataset` is the faoapi forecast data facade: the single entry point the API layer uses to turn a loaded forecast/historical artifact (a PRIO-GRID × month grid carrying per-cell posterior samples + a 9-column GAUL geo-metadata table) into served HDI/MAP output. It is the renamed leaf of the `_ViewsDataset → _PGDataset → ForecastDataset` chain and **composes** the extracted single-responsibility modules under `src/views_crafdapi/forecast/` (ingestion, frames, summarize, geography, aggregate, serialize), each of which it delegates to rather than re-implementing.

## 2. Non-Goals

- It does **not** do I/O (no Appwrite, no file reads); it accepts an already-loaded DataFrame.
- It does **not** apply or invert target transforms — values are raw counts as received (ADR-024).
- It does **not** own HTTP/caching concerns (that is `CrafdApiManager`).
- It does **not** re-implement the estimator/aggregation math — that is the frozen views-frames leaf via the `forecast/summarize` and `forecast/aggregate` modules.

## 3. Responsibilities and Guarantees

- **Construction:** validates the 9 GAUL metadata columns (`_METADATA_COLS`), a 2-level `(month_id, priogrid_id)` MultiIndex, builds the dense grid (delegates `forecast.ingestion.dense_grid`), and splits geography into `self.geo_metadata` (a separate scalar table, ADR-013).
- **Plausibility:** `validate_value_plausibility()` and `validate_metadata_plausibility()` delegate to `forecast.ingestion.plausibility` (C-72).
- **Estimation (ADR-025, epic #222):** `calculate_hdi_map(...)` returns a DataFrame with the
  per-variable columns `{var}_map`, `{var}_hdi{50,90,95}_{lower,upper}` (three nested HDIs at the
  fixed `schema.MASSES` = 50/90/95), and `{var}_severe_scenario` (worst-5% mean) — computed via
  `forecast.summarize.estimator.collapse` → `CollapseResult` and laid out by
  `forecast.serialize.json_contract.series_value_dataframe`. Raw `min`/`max` are **not** served
  (replaced by `severe_scenario`). The `alpha` parameter is retained for signature compatibility
  but is a **no-op** — the interval set is fixed config, not caller-selectable. Columns are
  **var-keyed** here; the consumer `sb/ns/os` rename is a boundary transform
  (`json_contract.to_consumer_columns`) applied at the API layer (S4), not in the reduction. Two
  paths: cell-level (`aggregate=False`) and admin-level (`aggregate=True`, joint-sampling sum then
  collapse — the `HDI(Σ) ≠ Σ HDI` invariant, C-70).
- **Subsetting:** `get_subset_dataframe(...)` / `get_subset_tensor(...)` filter by time/entity/feature/sample.
- **Frames:** `to_frames()` builds views-frames `PredictionFrame`/`TargetFrame` per target (#88).
- **Cloning:** `copy()` returns an independent copy via the optimised `__deepcopy__` (shares the underlying numpy sample buffers — ≈1,700× faster than a true deepcopy at global scale; safe as long as cell contents are not mutated in place). This is the public clone API the serving layer uses (C-137).
- **Back-compat:** `ForecastDataset` is a module-level alias of `ForecastDataset`; existing disk-cache pickles (class path `data.handlers.ForecastDataset`), `isinstance` checks, the test modules, and `notebooks/geo_meta.ipynb` continue to resolve. The `__init__` signature is unchanged, so the disk-cache schema version is unchanged.

## 4. Inputs and Assumptions

- `source`: a non-empty `pd.DataFrame` with a `(month_id, priogrid_id)` MultiIndex and the 9 metadata columns; `pred_*` columns (forecast) or scalar target columns (historical).
- `targets` (optional), `broadcast_features` (default False), `fill_value` (default 0, ADR-021).
- Values are raw counts (ADR-024); the prefix on a column name is not a scale signal (C-142).

## 5. Outputs and Side Effects

- `calculate_hdi_map(...)` → DataFrame indexed `(time, entity)` or `(time, geo_unit)` with the
  ADR-025 per-variable columns (`{var}_map` + three HDIs 50/90/95 + `{var}_severe_scenario`; no min/max).
- `.dataframe` exposes the internal grid (a plain attribute for now; removal deferred — register D-12).
- Side effect: lazy tensor caches; `copy()`/`__deepcopy__` reset the split-tensor cache.

## 6. Failure Modes and Loudness

- Missing metadata columns / wrong index → `ValueError` at construction.
- Non-finite or negative prediction samples, or implausible geo metadata → `ValueError` (C-72), surfaced as HTTP 500 by the ingestion path.
- An entity absent from the last time step → loud `ValueError` (C-87), never a silent drop.

## 7. Boundaries and Interactions

- **Depends on:** `forecast/` modules → `views_frames` / `views_frames_summarize` (the frozen leaf). Dependency direction is one-way toward the leaf (ADP/SDP).
- **Called by:** `CrafdApiManager._get_latest_dataframe` / `_get_latest_dataset`.

## 8. Test Alignment

- `tests/forecast/test_facade.py` — alias, `isinstance`, pickle round-trip, `copy()` buffer-sharing.
- `tests/forecast/test_served_output_golden.py` — pins the served output (the var-keyed reduction) across all 5 levels; re-baselined to the ADR-025 schema in #222/S4–S5 (`reports/adr025_output_schema/rebaseline_diff.md`). `tests/forecast/test_consumer_naming.py` pins the boundary `sb/ns/os` rename; `tests/forecast/test_bulk_parquet.py` the admin-1 bulk artifact.
- `tests/forecast/test_*` — the per-module units (ingestion, frames, summarize, geography, aggregate, serialize).
- `tests/test_aggregation.py`, `tests/test_views_frames_parity.py`, `tests/test_handler_statistics.py`, `tests/test_api_endpoints.py`.

## 9. Evolution Notes

- **In progress (#112):** the chain is renamed and the responsibilities are extracted into `forecast/`, but `_ViewsDataset`'s tensor/subset/index machinery is still inline; dissolving it into composed modules and deleting the chain is a future phase (deliberately not bundled — register C-139).
- **Phase 4b (gated):** flip serving to the views-frames-native `aggregate_via_leaf` (ADR-023 diff; C-136).
