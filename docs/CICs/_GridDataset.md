# Class Intent Contract: _ViewsDataset → renamed `_GridDataset` (S8)

**Status:** Active — this contract now governs **`_GridDataset`** (renamed from `_ViewsDataset` in S8); the public leaf is `ForecastDataset.md`  
**Owner:** Project maintainers  
**Last reviewed:** 2026-06-28  
**Related ADRs:** ADR-001 (Domain category), ADR-005 (Testing Doctrine), ADR-006 (this contract), ADR-021 (fill value semantics), ADR-030 (representation migration)  

> **AMENDMENT (S8, #162 / epic #154, 2026-06-28 — register D-21):** the inheritance chain was rationalized from three classes to two. `_ViewsDataset` is **renamed `_GridDataset`** — the generic, geo-less `(time, entity)`-grid sample dataset that is now **frame-native** (the canonical store is the `(N,S)` `_sample_store`; the object-dtype spine epic #154 set out to drain is gone, S4). `_PGDataset` is **retired** (its lone `priogrid_id` check folded into `ForecastDataset.validate_indices`). The chain is **kept at two levels deliberately**: `_GridDataset` is the geo-less unit the parity/validation suites instantiate directly and the vehicle `check_integrity` uses to round-trip a metadata-free subset; a full single-class merge was **rejected** (D-21) as complecting the generic and FAO responsibilities and relocating a UN-facing fail-loud. This supersedes the C-139 "delete the chain" intent — the intent (drain the swamp) was met at S4; the means (delete `_ViewsDataset`) was retired.  

> **NOTE (Phase 4a of #87, #112):** the chain's leaf was renamed `ForecastDataset → ForecastDataset` (see `ForecastDataset.md`), and the extracted single-responsibility modules are contracted in `forecast_package.md`.  

> **AMENDMENT (M1, 2026 — register C-81):** MAP/HDI is now computed by the **views-frames tower
> estimator** (`views_frames_summarize.tower_point` / `hdi_tower`) via the vectorized
> `_ViewsDataset._tower_collapse`, **not** `PosteriorDistributionAnalyzer` (retained only as a
> parity reference). The collapse runs **once per variable over the whole `(N, S)` tensor**, not a
> fresh instance per sample vector. The public `calculate_hdi_map()` contract below — return columns
> (`{var}_hdi_lower/_hdi_upper/_map/_min/_max`), index, ordering, error modes — is **unchanged**;
> only the estimator changed (published MAP/HDI values re-baselined). References to
> `PosteriorDistributionAnalyzer` below describe the prior estimator.
>
> **AMENDMENT (Phase 1 of #87, 2026):** ingestion logic is extracted into the `forecast/`
> package and these methods now **delegate** (behaviour unchanged): `_convert_to_arrays` →
> `forecast.ingestion.parquet_reader.to_array_columns`; `_preprocess_dataframe` →
> `forecast.ingestion.dense_grid.fill_dense_grid`; `validate_value_plausibility` →
> `forecast.ingestion.plausibility.assert_prediction_samples_plausible`; and the
> `_tower_collapse` frame construction → `forecast.frames.builder.build_prediction_frame`.
> **(Phase 2, #89):** `_tower_collapse` now delegates the whole MAP/HDI estimate to
> `forecast.summarize.estimator.tower_collapse`, and `PosteriorDistributionAnalyzer` is no
> longer imported by the serving module (it lives in `data/statistics.py` for parity tests only).
> New: `to_frames()` builds a views-frames `PredictionFrame`/`TargetFrame` per target variable
> at `SpatialLevel.PGM`. Per-module contracts are authored when the inheritance chain is
> collapsed (Phase 4, #112).

---

## 1. Purpose

> **What is this class for?**

`_GridDataset` is a DataFrame-backed dataset that provides time x entity x feature tensor semantics. It owns the bidirectional conversion between a MultiIndex pandas DataFrame (with two index levels: time ID and entity ID) and a structured 3D/4D numpy array, maintains index mappings between entity/time IDs and positional indices, and provides subsetting, integrity checking, and statistical summarization (HDI/MAP) over posterior distribution samples.

---

## 2. Non-Goals (Explicit Exclusions)

- This class does **not** perform I/O. It accepts an already-loaded DataFrame; it does not read files, query databases, or fetch from network sources.
- This class does **not** perform geographic aggregation. Aggregation from PRIO-GRID cells to administrative units is the responsibility of `ForecastDataset` (grandchild class).
- This class does **not** interact with Appwrite or any external storage backend.
- This class does **not** own model training, inference, or prediction generation. It only wraps and reshapes prediction outputs that were produced elsewhere.
- This class does **not** manage API routing, caching strategies, or HTTP concerns. Those belong to `CrafdApiManager`.
- This class does **not** perform density estimation or statistical computation directly. HDI/MAP calculation is delegated to `PosteriorDistributionAnalyzer`.

---

## 3. Responsibilities and Guarantees

- **Index validation:** Guarantees that the internal DataFrame has a two-level MultiIndex (time ID, entity ID). Construction fails with `ValueError` if this invariant is violated.
- **Dense grid fill:** Guarantees that after preprocessing, the DataFrame contains entries for all entity x time combinations of the last time step's entities. Missing combinations are recreated and filled with `fill_value` (default 0, per ADR-021) — array (sample) columns are filled with a sample-length array of `fill_value`, not a scalar (C-87). The dense grid is defined by the entities present in the last time step; an entity present in the input but **absent** from the last time step raises `ValueError` rather than being silently dropped (C-87) — consistent with the "does not silently drop data" guarantee below.
- **Index mapping:** Maintains sorted `_time_values` and `_entity_values` indices. Provides O(1) positional lookup from time ID or entity ID via `_get_time_index()` and `_get_entity_index()`. Missing IDs raise `KeyError`.
- **Tensor conversion:** Guarantees lossless round-trip conversion between DataFrame and tensor via `to_tensor()` / `to_dataframe()` after initial normalization (prediction columns are converted to float32 during `_validate_prediction_structure`, so float64 input data loses precision at construction time, not during round-trip). The tensor layout is `(time x entity x samples x features)` for both prediction and feature modes. Round-trip integrity is verifiable via `check_integrity()`.
- **Prediction/feature mode:** Automatically detects prediction mode by the presence of `pred_*` columns. In prediction mode, `targets` are the `pred_*` columns and `features` is empty. In feature mode, `targets` must be explicitly provided or a `ValueError` is raised.
- **Subsetting:** `get_subset_tensor()` and `get_subset_dataframe()` provide consistent subsetting along time, entity, feature, and sample dimensions. Invalid IDs raise `KeyError` or `ValueError`.
- **HDI/MAP delegation:** `calculate_hdi_map()` delegates per-cell statistical analysis to `PosteriorDistributionAnalyzer`, creating a fresh instance per sample vector. Returns a DataFrame with `{var}_hdi_lower`, `{var}_hdi_upper`, `{var}_map`, `{var}_min`, and `{var}_max` columns.
- **Tensor caching:** Caches computed tensors (`_prediction_tensor_cache`, `_features_tensor_cache`, `_split_tensor_cache`) to avoid redundant computation. Note: `_clear_tensor_cache_if_needed()` is defined with a 128-entry bound but is not currently wired into any insertion path — the cache grows unboundedly in practice. This is a known gap.
- **Deep copy performance:** `__deepcopy__` performs shallow DataFrame copies (sharing underlying numpy arrays), which is ~1,700x faster than default deepcopy at global scale. Safe as long as consumers do not mutate cell contents in-place.

---

## 4. Inputs and Assumptions

- **`source`** (`pd.DataFrame`): Must be a non-empty pandas DataFrame with a two-level MultiIndex. The first level is the time ID, the second is the entity ID. Despite the constructor signature accepting `Union[pd.DataFrame, str, Path]`, only `pd.DataFrame` is supported; other types raise `ValueError`.
- **`targets`** (`Optional[List[str]]`): In feature mode, must be a list of column names present in the DataFrame. Omitting targets in feature mode raises `ValueError`. In prediction mode (columns prefixed `pred_*`), targets are auto-detected and the `targets` parameter is ignored with a warning.
- **`broadcast_features`** (`bool`, default `False`): Controls whether scalar features are broadcast to match the sample size of array-valued columns. When `False`, scalars are wrapped in size-1 arrays and tensor operations are disabled (`sample_size = None`).
- **`fill_value`** (`float`, default `0`): Value used to fill missing entity x time grid cells during preprocessing. Default follows the views-datafactory dense grid convention (ADR-021).
- **Column naming convention:** Prediction columns must be prefixed with `pred_`. All prediction columns must have consistent array lengths (sample sizes). In prediction mode, no non-prediction columns may be present.
- **Index name `priogrid_gid`:** If the second index level is named `priogrid_gid`, it is automatically renamed to `priogrid_id` with a warning. This is a **permanent** input-normalization shim — upstream carries a mixed `priogrid_gid`/`priogrid_id` vocabulary (register C-61/C-63) — not a temporary compatibility hack to be removed.

---

## 5. Outputs and Side Effects

- **`to_tensor()`**: Returns a 4D numpy array of shape `(time x entity x samples x features/targets)`. In prediction mode, returns the prediction tensor directly. In feature mode with `include_targets=False`, returns only the feature dimensions.
- **`to_dataframe(tensor)`**: Converts a tensor back to a DataFrame with the original MultiIndex structure. The returned DataFrame is indexed to match `self.original_index`.
- **`get_subset_tensor()` / `get_subset_dataframe()`**: Return filtered views of the data along any combination of time, entity, feature, and sample dimensions.
- **`calculate_hdi_map()`**: Returns a DataFrame with columns `{var}_hdi_lower`, `{var}_hdi_upper`, `{var}_map`, `{var}_min`, `{var}_max` for each selected prediction variable.
- **`check_integrity()`**: Returns `True` if tensor round-trip reconstruction produces an identical DataFrame; `False` otherwise.
- **Properties:** `num_entities`, `num_time_steps`, `num_features` return integer counts.
- **Side effects:**
  - Internal tensor caches (`_prediction_tensor_cache`, `_features_tensor_cache`, `_split_tensor_cache`) are populated lazily on first tensor access.
  - `_preprocess_dataframe()` mutates the internal DataFrame by adding fill-value rows for missing grid cells.
  - `_entity_metadata_cache` is initialized to `None` and available for subclass use.

---

## 6. Failure Modes and Loudness

- **`ValueError("Invalid input type for ViewsDataset")`** -- Raised in `__init__` if `source` is not a `pd.DataFrame`.
- **`ValueError("Dataframe is empty or not a valid DataFrame")`** -- Raised in `_init_dataframe` if the DataFrame is empty.
- **`ValueError("DataFrame must have a MultiIndex")`** -- Raised by `validate_indices()` if the index is not a MultiIndex.
- **`ValueError("Must have exactly two index levels")`** -- Raised by `validate_indices()` if the MultiIndex does not have exactly two levels.
- **`ValueError("Missing targets: {missing_vars}")`** -- Raised if specified target columns are not found in the DataFrame.
- **`ValueError("Targets must be specified for non-prediction dataframes...")`** -- Raised in feature mode when `targets` is `None`.
- **`ValueError("Inconsistent sample sizes in prediction columns: ...")`** -- Raised if `pred_*` columns have different array lengths.
- **`ValueError("Prediction dataframe should only contain pred_* columns...")`** -- Raised if non-prediction feature columns are present alongside `pred_*` columns.
- **`TypeError("Invalid type {type} for prediction column {var}")`** -- Raised by `_validate_prediction_structure` if a prediction column contains an unsupported type (not int, float, list, or ndarray).
- **`ValueError("Prediction columns must contain array-like values after conversion")`** -- Raised by `_validate_prediction_structure` if conversion to ndarray fails.
- **`ValueError("Tensor operations are disabled when broadcast_features=False")`** -- Raised by `to_tensor()` when tensor conversion is attempted without broadcast.
- **`KeyError("Time ID ... not found")`** / **`KeyError("Invalid time IDs: ...")`** -- Raised when subsetting with nonexistent time IDs.
- **`KeyError("Entity ID ... not found")`** / **`KeyError("Invalid entity IDs: ...")`** -- Raised when subsetting with nonexistent entity IDs.
- **`ValueError("Feature dimension mismatch: ...")`** -- Raised by `_validate_tensor_dims` on shape inconsistency during tensor-to-DataFrame conversion.
- **Must never fail silently:** Index mismatches, missing columns, and shape violations must always raise. Dense grid fill proceeds deterministically and does not silently drop data.

---

## 7. Boundaries and Interactions

- **Layer:** Domain (`src/views_crafdapi/data/handlers/grid_dataset.py`).
- **Children (subclasses):**
  - `_PGDataset` -- adds `priogrid_id` index validation.
  - `ForecastDataset` (via `_PGDataset`) -- adds geographic metadata columns, aggregation to admin units, and Shapefile-based spatial operations.
- **Callers:** `ForecastDataset` (child), `CrafdApiManager` (via child). Test fixtures in `conftest.py`.
- **Dependencies:**
  - `PosteriorDistributionAnalyzer` (domain peer in `data/statistics.py`) -- instantiated fresh per sample vector inside `_analyze_samples`, `_compute_single_map`, and `_calculate_single_hdi`.
  - `pandas`, `numpy` -- structural dependencies for DataFrame/tensor operations.
  - `joblib.Parallel` -- patched by `tqdm_joblib` context manager for progress reporting.
- **Must not depend on:** Infrastructure layer (`managers/`), Observability layer (`wandb/`), API layer (`routers/`), or any I/O / network operations.
- **Trusts:** Callers to provide DataFrames with correct MultiIndex structure and semantically valid column data. The class validates structure but not semantic correctness of cell values.

---

## 8. Examples of Correct Usage

**Creating a prediction dataset and computing HDI/MAP:**

```python
import pandas as pd
import numpy as np

# DataFrame with MultiIndex (month_id, priogrid_id) and pred_* columns
index = pd.MultiIndex.from_product(
    [[500, 501], [100001, 100002]],
    names=["month_id", "priogrid_id"]
)
df = pd.DataFrame(
    {"pred_sb": [np.random.normal(5, 1, 100) for _ in range(4)]},
    index=index,
)

ds = _GridDataset(df)
assert ds.is_prediction is True

# Get tensor: shape (2 time, 2 entity, 100 samples, 1 target)
tensor = ds.to_tensor()

# Round-trip check
assert ds.check_integrity() is True

# HDI/MAP summary
summary = ds.calculate_hdi_map(alpha=0.9)
# Returns columns: pred_sb_hdi_lower, pred_sb_hdi_upper, pred_sb_map, pred_sb_min, pred_sb_max
```

**Creating a feature dataset with explicit targets:**

```python
index = pd.MultiIndex.from_product(
    [[500, 501], [100001, 100002]],
    names=["month_id", "priogrid_id"]
)
df = pd.DataFrame(
    {"ln_sb_best": [1.0, 2.0, 3.0, 4.0], "feature_a": [0.1, 0.2, 0.3, 0.4]},
    index=index,
)

ds = _GridDataset(df, targets=["ln_sb_best"], broadcast_features=True)
assert ds.is_prediction is False
assert ds.features == ["feature_a"]
```

---

## 9. Examples of Incorrect Usage

**Omitting targets in feature mode:**

```python
# WRONG: Non-prediction DataFrame requires explicit targets.
df = pd.DataFrame({"feature_a": [1, 2]}, index=some_multiindex)
ds = _GridDataset(df)  # Raises ValueError: "Targets must be specified..."
```

**Calling tensor operations without broadcast_features:**

```python
# WRONG: Tensor operations disabled when broadcast_features=False (the default).
ds = _GridDataset(df, targets=["ln_sb_best"], broadcast_features=False)
ds.to_tensor()  # Raises ValueError: "Tensor operations are disabled..."
```

**Calling calculate_hdi_map on a feature dataset:**

```python
# WRONG: HDI/MAP is only valid for prediction datasets.
ds = _GridDataset(df, targets=["ln_sb_best"], broadcast_features=True)
ds.calculate_hdi_map()  # Raises ValueError: "HDI and MAP calculation only valid for prediction dataframes"
```

**Mixing pred_* and non-pred columns:**

```python
# WRONG: Prediction DataFrames must contain only pred_* columns.
df = pd.DataFrame(
    {"pred_sb": [...], "feature_a": [...]},
    index=some_multiindex,
)
ds = _GridDataset(df)  # Raises ValueError: "Prediction dataframe should only contain pred_* columns..."
```

---

## 10. Test Alignment

- **Green tests (must pass):**
  - MultiIndex validation: rejects non-MultiIndex and wrong number of levels (`test_dataset_validation.py`).
  - Dense grid fill: verifies all entity x time combinations are present after preprocessing.
  - Tensor round-trip: `check_integrity()` returns `True` for both prediction and feature datasets.
  - `calculate_hdi_map()` returns expected columns and non-NaN values for valid prediction data (`test_handler_statistics.py`).
  - Subsetting: `get_subset_tensor` and `get_subset_dataframe` return correct shapes and raise on invalid IDs.
  - Prediction mode auto-detection: datasets with `pred_*` columns correctly set `is_prediction = True`.
  - Feature mode target validation: missing targets raise `ValueError`.

- **Beige tests (behavioral expectations):**
  - `fill_value` parameter correctly fills missing grid cells (ADR-021 compliance).
  - `__deepcopy__` produces a structurally independent copy that shares underlying array data.
  - Tensor caching: second call to `to_tensor()` returns cached result without recomputation.

- **Red tests (regression guards):**
  - Statistical pipeline integration: HDI/MAP values match expected distributions (`test_statistical_pipeline.py`).
  - Index mapping consistency: `_time_values` and `_entity_values` remain sorted and complete after preprocessing.
  - `priogrid_gid` -> `priogrid_id` rename: legacy index name is transparently corrected.

- **Fixtures:** `conftest.py` provides a shared `ForecastDataset` fixture (`fao_dataset`) and `make_fao_df` helper. `_GridDataset` instances are constructed inline in individual test files.

---

## 11. Evolution Notes

- **Stable:** The tensor layout `(time x entity x samples x features)`, MultiIndex contract, and dense grid fill semantics are used by all downstream consumers. Changing these would require updating `_PGDataset`, `ForecastDataset`, and `CrafdApiManager`.
- **Stable:** The `pred_*` column naming convention for auto-detecting prediction mode is a public contract.
- **Leading underscore:** The `_GridDataset` prefix marks this as an internal base class. It is not part of the public API and should not be instantiated directly in production code -- only through `ForecastDataset` (of which `ForecastDataset` is a back-compat alias). `_PGDataset` was retired (S8, #162).
- **Candidate for change:** The constructor signature accepts `Union[pd.DataFrame, str, Path]` but only `pd.DataFrame` is implemented. The `str`/`Path` branches were removed; the type signature should be narrowed.
- **Candidate for change:** `tqdm_joblib` is a static utility method that patches `joblib.Parallel` globally. It could be extracted to a standalone utility.
- **Would require contract revision:** Changing the fill value default, modifying the tensor axis ordering, adding new index levels, or changing the `pred_*` auto-detection convention.

---

## End of Contract

This document defines the **intended meaning** of `_GridDataset` (formerly `_ViewsDataset`).

Changes to behavior that violate this intent are bugs.  
Changes to intent must update this contract.
