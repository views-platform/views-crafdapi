# Class Intent Contract: FAO_PGMDataset

**Status:** Superseded by `ForecastDataset.md` (alias retained)
**Owner:** Project maintainers
**Last reviewed:** 2026-06-26
**Related ADRs:** ADR-021 (Dense Grid Fill Value Semantics)

> **RENAME (Phase 4a of #87, #112):** the class was renamed **`FAO_PGMDataset → ForecastDataset`**; `FAO_PGMDataset` is now a back-compat **alias** (preserves disk-cache pickles, `isinstance`, the test modules, and `notebooks/geo_meta.ipynb`). The authoritative contract is now **`ForecastDataset.md`**; the per-module contracts are in **`forecast_package.md`**. The content below describes the same class under its former name.

> **AMENDMENT (M1, 2026 — register C-81):** HDI/MAP is now computed by the **views-frames tower
> estimator** (`views_frames_summarize.tower_point` / `hdi_tower`) via `_ViewsDataset._tower_collapse`,
> **not** `PosteriorDistributionAnalyzer` (retained only as a parity reference). The public
> `calculate_hdi_map()` contract below (return columns, index, two-path aggregate, error modes) is
> **unchanged**; only the estimator changed (values re-baselined). The aggregation invariant —
> distributions are summed element-wise *before* the collapse (`HDI(Σ) ≠ Σ HDI`) — still holds.
> References to `PosteriorDistributionAnalyzer` below describe the prior estimator.
>
> **AMENDMENT (Phase 1 of #87, 2026):** `validate_metadata_plausibility` now **delegates** to
> `forecast.ingestion.plausibility.assert_geo_metadata_plausible` (behaviour unchanged). The
> retained geography layer (geo-metadata table + level mapping) moves into `forecast/geography/`
> in Phase 3 (#90); full per-module contracts land with the facade in Phase 4 (#112).
>
> **AMENDMENT (Phase 3 of #87, #90):** the level vocabulary (`levels`), the per-level metadata
> columns, and `_get_pg_cells` now come from `forecast.geography.metadata_table`; `_elementwise_sum`
> delegates to `forecast.aggregate.cross_level.elementwise_sum`. The views-frames-native joint-sum
> (`aggregate_via_leaf` over `aggregate_distributions_arrays`, fed by
> `forecast.geography.level_mapping.build_cell_to_unit_mapping`) is proven equivalent to the faoapi
> primitive (`tests/forecast/test_cross_level_aggregate.py`); the serving path keeps the primitive
> (byte-identical) until the frame-native facade cutover (Phase 4, #112). The `HDI(Σ) ≠ Σ HDI`
> invariant (C-70) is unchanged.

---

## 1. Purpose

`FAO_PGMDataset` represents a VIEWS PRIO-GRID Monthly (PGM) dataset enriched with geographic metadata, supporting HDI/MAP statistical analysis and geographic aggregation across administrative levels (country, GAUL Level 0/1/2).

It extends `_PGDataset` (which extends `_ViewsDataset`) to add FAO-specific geographic metadata handling and the ability to aggregate probabilistic predictions from individual PRIO-GRID cells up to higher administrative units while preserving statistically correct uncertainty quantification.

**Location:** `src/views_faoapi/data/handlers/forecast_dataset.py`

---

## 2. Non-Goals (Explicit Exclusions)

- This class does **not** manage storage, persistence, or file I/O (that responsibility belongs to `PredictionStoreManager` and `FAOApiManager`).
- This class does **not** perform HTTP routing or handle API requests.
- This class does **not** own the statistical algorithms for HDI or MAP computation (it delegates to `PosteriorDistributionAnalyzer`).
- This class does **not** modify or mutate the underlying prediction sample arrays; it reads them and computes derived statistics.
- This class does **not** fetch or resolve geographic metadata at runtime (metadata must be present in the source DataFrame at construction time, produced by the `un_fao` postprocessor).
- This class does **not** handle country-month (CM) level-of-analysis data. It is strictly for PRIO-GRID monthly data.
- `__deepcopy__` uses shallow DataFrame copies (shared numpy backing arrays) for performance at scale. The copy is structurally independent but mutation of the underlying array data in a clone will affect the original.

---

## 3. Responsibilities and Guarantees

### Construction guarantees
- Validates that the source is a `pd.DataFrame`.
- Validates that all 9 required metadata columns are present: `pg_xcoord`, `pg_ycoord`, `country_iso_a3`, `admin1_gaul0_code`, `admin1_gaul0_name`, `admin1_gaul1_code`, `admin1_gaul1_name`, `admin2_gaul2_code`, `admin2_gaul2_name`.
- Strips metadata columns before delegating to the parent constructor (which preprocesses the data by filling missing time/entity combinations), then realigns `geo_metadata` to match the preprocessed index by looking up entity-level metadata from the original data (each entity's metadata is assumed consistent across time steps).
- If the source DataFrame has flat columns `month_id` and `priogrid_id` (or `priogrid_gid`) instead of a `MultiIndex`, the constructor auto-sets the index before delegating to the parent.
- Parent chain validates that the DataFrame has a two-level `MultiIndex` with level names `(month_id, priogrid_id)` (after auto-indexing, if applicable).

### Data structure guarantees
- `self.dataframe` is a `pd.DataFrame` with `MultiIndex(month_id, priogrid_id)`. In prediction mode, columns are `pred_*` containing `np.ndarray` values (one array of posterior samples per cell per time step). In historical/explicit mode, columns are the specified `targets` containing scalar values.
- `self.geo_metadata` is a `pd.DataFrame` aligned to the same index as `self.dataframe`, containing the 9 geographic metadata columns.
- `self.levels` is a dict mapping human-readable level names (`"country"`, `"gaul0"`, `"gaul1"`, `"gaul2"`) to their corresponding metadata column names.

### Statistical guarantee (aggregation)
- When computing HDI on aggregated distributions (`aggregate=True`), the class first sums posterior samples element-wise across all PRIO-GRID cells within a geographic unit for each time step, then computes HDI and MAP on the resulting summed distribution. This correctly represents the aggregated posterior distribution. The class never sums HDI bounds directly, which would be statistically incorrect.

### Subset and query guarantees
- `get_subset_dataframe` respects level-based entity resolution: when a `level` and `entity_ids` are provided (e.g., `level="country"`, `entity_ids=["NGA"]`), it resolves entity codes to constituent PRIO-GRID cell IDs before filtering.
- `calculate_hdi_map` supports two paths: (1) `aggregate=False` computes HDI/MAP per PRIO-GRID cell; (2) `aggregate=True` aggregates distributions before computing statistics.

### Plausibility validation (C-72)
- `validate_value_plausibility()` (inherited from `_ViewsDataset`) asserts every prediction posterior sample is finite and non-negative; no-op for non-prediction datasets.
- `validate_metadata_plausibility()` asserts geographic metadata is plausible: `pg_xcoord` ∈ [−180, 180], `pg_ycoord` ∈ [−90, 90], `country_iso_a3` matches a 3-letter alpha code, and GAUL codes are non-negative. Checks non-null values only (missing metadata is a separate concern). Both raise `ValueError` on violation; the API ingestion path runs them before caching/serving and surfaces a violation as HTTP 500.

---

## 4. Inputs and Assumptions

### Constructor inputs
- `source: pd.DataFrame` -- must contain all 9 required metadata columns. Two input modes are supported: (1) **prediction mode:** DataFrame has `pred_*` columns; `targets` parameter is ignored; (2) **historical/explicit mode:** no `pred_*` columns; `targets` parameter is required and names the columns to treat as dependent variables. The DataFrame may have a two-level `MultiIndex(month_id, priogrid_id)` or flat columns `month_id` + `priogrid_id`/`priogrid_gid` (auto-indexed at construction).
- `targets: list[str]` -- required when no `pred_*` columns are present; ignored in prediction mode.
- `broadcast_features: bool` -- controls whether scalar features are broadcast to match sample size (default `False`).
- `fill_value: float` -- value used to fill missing time/entity combinations during dense grid construction (default `0`). Governed by ADR-021.

### Assumptions
- The source DataFrame has been produced by the `un_fao` postprocessor pipeline, which enriches raw VIEWS predictions with the required geographic metadata columns.
- Prediction columns contain array-like values (lists or `np.ndarray`) of equal length across all cells and targets within a single dataset instance.
- Geographic metadata is consistent within each `priogrid_id` across time steps (the same cell always belongs to the same country/admin unit).
- `PosteriorDistributionAnalyzer` is available and functional for HDI/MAP computation.

---

## 5. Outputs and Side Effects

### Outputs
- `calculate_hdi_map(...)` returns a `pd.DataFrame` with columns `{var}_hdi_lower`, `{var}_hdi_upper`, `{var}_map`, `{var}_min`, `{var}_max` for each target variable, indexed by `(time_id, entity_id)` or `(time_id, geo_unit)` when aggregated.
- `get_subset_dataframe(...)` returns a `pd.DataFrame` containing raw sample distributions, optionally joined with geographic metadata, optionally aggregated to a higher administrative level.
- `_get_pg_cells(level, code)` returns a `list[int]` of PRIO-GRID cell IDs belonging to the specified geographic unit.
- `_aggregate_distributions(df, level)` returns a `pd.DataFrame` with distributions summed element-wise, grouped by `(time_id, geo_unit)`.

### Side effects
- Logging via the module-level `logger` for warnings and informational messages.
- Internal caching via `self._split_tensor_cache` (bounded by `_max_tensor_cache_size`).
- No filesystem writes, no network calls, no mutation of input data.

---

## 6. Failure Modes and Loudness

### Errors raised during construction
- `ValueError` if `source` is not a `pd.DataFrame`.
- `ValueError` if any of the 9 required metadata columns are missing from the source, with a message directing the user to the `un_fao` postprocessor.
- `ValueError` (from parent) if the DataFrame is empty, lacks a `MultiIndex`, does not have exactly two index levels, or if the index level names are not `(month_id, priogrid_id)`.
- `ValueError("Specified targets not found in data: ...")` if explicit targets are not present in the DataFrame columns.
- `ValueError("No prediction columns (pred_*) found and no targets specified. ...")` if neither pred columns nor explicit targets are provided.
- `ValueError("MultiIndex must be (month_id, priogrid_id) or (month_id, priogrid_gid), got ...")` if after auto-indexing, the index level names don't match expected patterns.
- `ValueError("Source must have a 2-level MultiIndex, got ...")` if the DataFrame doesn't have a valid 2-level MultiIndex after auto-indexing.

### Errors raised during operation
- `ValueError` from `_get_pg_cells` if the specified `level` is not one of `"country"`, `"gaul0"`, `"gaul1"`, `"gaul2"`.
- `ValueError` from `calculate_hdi_map` if `aggregate=True` but `level` is not specified.
- `ValueError` from `get_subset_dataframe` if `aggregate=True` but `level` is not specified.
- `ValueError` if `alpha` is not in the open interval `(0, 1)`.
- `ValueError` or `KeyError` from parent methods if invalid `time_ids`, `entity_ids`, `features`, or `sample_idx` are provided.

### Graceful degradation
- Returns `(np.nan, np.nan, np.nan)` for HDI/MAP when all samples in a cell are `NaN`, rather than raising an error.
- When `_get_pg_cells` finds no matching cells for a given code, it returns an empty list, which propagates to an empty result downstream.

### Must never fail silently
- Missing metadata columns at construction time.
- Invalid geographic level names.
- Misalignment between `geo_metadata` and `dataframe` indices (handled by explicit reindexing and forward-fill at construction).

---

## 7. Boundaries and Interactions

### Layer
Domain layer (`src/views_faoapi/data/`).

### Depends on
- `_PGDataset` / `_ViewsDataset` (parent classes in the same module) for DataFrame preprocessing, tensor conversion, and base HDI/MAP computation.
- `PosteriorDistributionAnalyzer` (`src/views_faoapi/data/statistics.py`) for statistical computation of HDI and MAP. Treated as a trusted, stateless service.
- `numpy`, `pandas` for data manipulation.

### Called by
- `FAOApiManager` (in `src/views_faoapi/managers/`) for serving API endpoint responses.

### Must not depend on
- Infrastructure layer (`managers/`) -- the dataset is a pure domain object and must not import or call `PredictionStoreManager`, `AppWriteFileManager`, or any API/HTTP components.
- Mapping/shapefile utilities at runtime (geographic metadata must arrive pre-computed in the source DataFrame).

---

## 8. Examples of Correct Usage

### Constructing a dataset and computing cell-level HDI/MAP
```python
import pandas as pd
from views_faoapi.data.handlers import FAO_PGMDataset

# source_df has MultiIndex(month_id, priogrid_id), pred_* columns, and 9 metadata columns
dataset = FAO_PGMDataset(source=source_df)

# Cell-level HDI/MAP for all time steps and cells
result = dataset.calculate_hdi_map(alpha=0.9, with_metadata=True)
```

### Computing country-level aggregated HDI/MAP
```python
# Aggregate predictions to country level for Nigeria
result = dataset.calculate_hdi_map(
    alpha=0.9,
    level="country",
    entity_ids=["NGA"],
    aggregate=True,
    enforce_non_negative=True,
)
# result is indexed by (month_id, country_iso_a3) with proper uncertainty from summed distributions
```

---

## 9. Examples of Incorrect Usage

### Summing HDI bounds instead of aggregating distributions
```python
# WRONG: Computing cell-level HDI then summing the bounds
cell_hdi = dataset.calculate_hdi_map(alpha=0.9, aggregate=False)
country_hdi = cell_hdi.groupby("country_iso_a3").sum()
# This produces statistically INCORRECT results.
# HDI(sum of distributions) != sum of HDI bounds.

# CORRECT: Use aggregate=True to sum distributions first, then compute HDI
country_hdi = dataset.calculate_hdi_map(alpha=0.9, level="country", aggregate=True)
```

### Constructing without required metadata columns
```python
# WRONG: Passing a raw VIEWS DataFrame without the un_fao postprocessor output
raw_df = viewser.query(...)  # Missing geographic metadata columns
dataset = FAO_PGMDataset(source=raw_df)
# Raises ValueError: Missing necessary metadata columns [...]
```

### Using aggregate without specifying a level
```python
# WRONG: aggregate=True without level
dataset.calculate_hdi_map(aggregate=True)
# Raises ValueError: Must specify 'level' when aggregate=True
```

---

## 10. Test Alignment

### Invariants that tests must enforce
- Construction with all 9 metadata columns succeeds; construction missing any column raises `ValueError`.
- `geo_metadata` index is identical to `dataframe` index after construction, including rows added by preprocessing.
- `_get_pg_cells("country", "NGA")` returns only `priogrid_id` values whose metadata maps to `country_iso_a3 == "NGA"`.
- Element-wise sum via `_elementwise_sum` produces correct results: `[1,2,3] + [4,5,6] = [5,7,9]`.
- Aggregated HDI/MAP computed via `aggregate=True` differs from naive sum of cell-level HDI bounds (regression test for the statistical guarantee).
- `calculate_hdi_map` with `aggregate=True` and no `level` raises `ValueError`.
- `_get_pg_cells` with an invalid level raises `ValueError`.
- All-NaN sample arrays produce `NaN` HDI/MAP values, not exceptions.

### Test categories
- **Green tests:** Construction validation, metadata alignment, element-wise aggregation arithmetic.
- **Beige tests:** HDI/MAP statistical correctness (depends on `PosteriorDistributionAnalyzer` behavior).
- **Red tests:** Integration with real prediction data shapes and geographic metadata completeness.

### Test files
- `tests/test_aggregation.py` — `TestGetPgCells`, `TestElementwiseSum`, `TestAggregateDistributions`, `TestGetSubsetDataframe`, `TestCalculateHdiMap`
- `tests/test_views_dataset.py::TestFAOPGMDataset` — construction validation, property checks
- `tests/test_dataset_validation.py::TestFAOPGMDatasetValidation` — metadata column and index validation
- `tests/test_handler_statistics.py` — float32 precision integration tests

---

## 11. Evolution Notes

### Expected to change
- The set of supported geographic levels may expand (e.g., adding GAUL Level 3 or custom regions).
- The metadata column set may grow if additional geographic attributes are required by downstream consumers.
- The commented-out `_get_metadata` method suggests a future path where metadata is resolved at runtime via a mapper rather than required in the source DataFrame.

### Considered stable
- The two-path design of `calculate_hdi_map` (cell-level vs. aggregated).
- The element-wise summation approach for aggregating probabilistic distributions.
- The `MultiIndex(month_id, priogrid_id)` index contract.
- The inheritance chain `FAO_PGMDataset -> _PGDataset -> _ViewsDataset`.

### Changes requiring contract revision
- Switching from pre-computed metadata to runtime metadata resolution.
- Adding support for non-summation aggregation strategies (e.g., weighted averages).
- Changing the index structure or adding a third index level.

---

## End of Contract

This document defines the **intended meaning** of `FAO_PGMDataset`.

Changes to behavior that violate this intent are bugs.
Changes to intent must update this contract.
