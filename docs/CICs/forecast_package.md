# Contract: the `forecast/` package modules

**Status:** Active
**Owner:** Project maintainers
**Last reviewed:** 2026-06-26
**Scope:** `src/views_crafdapi/forecast/` — the single-responsibility modules composed by `ForecastDataset` (see `ForecastDataset.md`). Each module has **one reason to change**; dependencies point one way, toward the frozen views-frames leaf (ADP/SDP). `summarize/` does not import `geography/` (CRP).

This is a consolidated, brief contract for the package; the facade contract is `ForecastDataset.md`.

---

## `ingestion/` — turn the artifact into a clean, validated, dense grid
- **`parquet_reader.to_array_columns(df)`** — decode list-valued columns to numpy arrays. The seam where a future views-frames arrow frame-load (#100) drops in. *One reason to change: how the artifact is decoded.*
- **`dense_grid.fill_dense_grid(df, time_values, time_id, entity_id, fill_value)`** — recreate every `(time, last-step entity)` cell; array columns filled with a sample-length array (C-87), `fill_value` default 0 (ADR-021); an entity absent from the last step fails loud (C-87). **Retained** in faoapi (views-frames has no fill primitive). *One reason: the fill rule.*
- **`plausibility.assert_prediction_samples_plausible(var, samples)`** / **`assert_geo_metadata_plausible(geo)`** — C-72 value + metadata trust-boundary checks; raise `ValueError`. *One reason: what makes ingested data trustworthy.*

## `frames/` — build views-frames value objects
- **`builder.build_prediction_frame(values, time, unit, level=PGM)`** / **`build_target_frame(...)`** — `(N,S)` → `PredictionFrame`, `(N,)`→`TargetFrame (N,1)`. Geography is **not** embedded (ADR-013/014). *One reason: how faoapi makes leaf frames.*

## `summarize/` — point + interval estimation (no geography — CRP; pandas-free, ADR-030 ratchet)
- **`estimator.collapse(values, masses=(0.5,0.9,0.95), tail=0.05, enforce_non_negative=False)`** —
  the ADR-025 reduction: MAP (`tower_point`) + three nested HDIs (`hdi_tower`, multi-mass) +
  `severe_scenario`, returning a **`result.CollapseResult`** value object. `estimator.tower_collapse(values, mass, ...)`
  is the single-mass tuple subset it retains. Raw space (ADR-024); no `log→collapse→exp`.
- **`result.CollapseResult`** — value object holding `.map`, `.hdi[mass]` (each `(N,2)`), `.severe`, `.bimodality`,
  with `.lower(mass)` / `.upper(mass)` / `.masses()`. Float64 (the served collapse dtype).
- **`severe.expected_shortfall(values, tail=0.05)`** — the `severe_scenario` reducer: mean of the
  worst (largest) `tail` fraction of draws. An **arithmetic** surface → computed in float64
  (ADR-030 §6). *One reason: the published-number methodology.*

## `geography/` — the GAUL metadata the leaf cannot carry (RETAINED)
- **`metadata_table.LEVELS` / `LEVEL_METADATA_COLUMNS` / `resolve_level_cells(...)`** — the admin-level vocabulary + cell→level resolution. Identity grounded in `views-postprocessing/unfao/gaul_schema.py`; country = GAUL admin-0, not M49 (ADR-025).
- **`level_mapping.build_cell_to_unit_mapping(geo, level_col)`** — the injected `(time, priogrid)→unit` map for the leaf (ADR-014); string codes factorized to integer units. *One reason: the GAUL/admin scheme.*

## `aggregate/` — joint-sampling roll-up (C-70)
- **`cross_level.elementwise_sum(arrays)`** — faoapi's joint-sum primitive (currently wired into serving). **`aggregate_via_leaf(values, time, unit, map_keys, map_vals, target_level)`** — the views-frames-native path (`aggregate_distributions_arrays`), parity-proven equal to the primitive; adopted in serving at Phase 4b. *One reason: the aggregation mechanics.* `HDI(Σ) ≠ Σ HDI`.

## `serialize/` — lay results onto the served column contract (pandas allowed — encode seam)
- **`schema`** — the single source of truth for the ADR-025 column contract (Common Closure):
  `MASSES` (50/90/95) + `mass_pct`, `SERIES` (`sb`/`ns`/`os`) + `series_of(var)` (fail-loud),
  `IDENTITY_SOURCE` (consumer identity ← GAUL metadata; country = admin-0, not M49), the per-series
  column builders + `bulk_columns()` (the 36-col layout: 33 base + per-series bimodality_flag), and `consumer_column` (the structured,
  quantity-scoped rename). Pure names — no pandas/numpy. *One reason: the FAO column contract.*
- **`json_contract`** — numpy/`CollapseResult` → the served columns: `series_value_dataframe` /
  `series_value_data` / `series_value_column_names` build `{var}_map` + `{var}_hdi{50,90,95}_{lower,upper}`
  + `{var}_severe_scenario`; **`to_consumer_columns(df)`** applies the `{var}_→{series}_` rename at the
  API/bulk boundary. (`map_dataframe`/`hdi_dataframe` remain for the legacy single-HDI shape.)
  *One reason: the output column shape/names (ADR-025).*
- **`bulk_parquet`** — the ADR-025 admin-1 bulk artifact: `build_bulk_table(forecast_ds, historical_ds)` /
  `write_bulk_parquet(...)` produce the 45-column wide parquet (6 identity + 3 series x 13) (forecast quantities + historical
  `actual` summed to admin-1). Its own contract: `BulkParquetWriter.md`. *One reason: the bulk product.*

## `conformance.py` — assert the leaf's published contracts on real faoapi frames (#91)
- **`assert_frame(frame)`** (frame contract + summarizer contract), **`assert_cross_level_law(index, mapping, target_level)`**, and the pinned **`CONFORMANCE_FLOOR`** (leaf ADR-016). Run as part of the pytest suite (the CI gate) on real forecast/historical frames + the injected GAUL mapping. *One reason: the leaf contract version faoapi is held to.*

---

## Test alignment
`tests/forecast/` mirrors the package: `test_ingestion_modules.py`, `test_frame_builder.py`, `test_summarize_estimator.py`, `test_cross_level_aggregate.py`, `test_serialize.py`, `test_facade.py`, plus the byte-identical gate `test_served_output_golden.py` and `test_disk_cache_resilience.py`.
