# Class Intent Contract: PredictionStoreManager

**Status:** Active
**Owner:** Project maintainers
**Last reviewed:** 2026-06-03
**Related ADRs:** None

---

## 1. Purpose

`PredictionStoreManager` provides prediction-specific file storage and retrieval operations by wrapping `AppWriteFileManager` with structured metadata validation, model-aware filtering, and latest-file resolution.

It acts as the prediction-domain facade over Appwrite's generic file storage, ensuring that every uploaded prediction carries validated metadata (via `PredictionMetadata`) and that queries are automatically scoped to the current model.

**Location:** `src/views_crafdapi/managers/prediction/manager.py`

---

## 2. Non-Goals (Explicit Exclusions)

- This class does **not** compute statistics on predictions (HDI, MAP, or any distributional analysis -- that responsibility belongs to `PosteriorDistributionAnalyzer` and `ForecastDataset`).
- This class does **not** manage `DataFrame` or tensor representations of prediction data.
- This class does **not** handle API routing, HTTP request/response lifecycle, or authentication.
- This class does **not** interpret or transform prediction data contents; it treats prediction files as opaque binary blobs with structured metadata.
- This class does **not** manage Appwrite infrastructure (database/collection creation, schema migration). It assumes the required database, collection, and bucket infrastructure exists or can be auto-created for buckets only.

---

## 3. Responsibilities and Guarantees

### Metadata validation
- Every upload is gated by `PredictionMetadata` validation, which enforces:
  - `loa` is a `str`.
  - `name` is a `str`.
  - `type` is a `str`.
  - `targets` is a `list[str]`.
  - `category` is exactly `"forecast"` or `"historical"`.
  - `description` is `str` or `None`.
- Invalid metadata types raise `TypeError` or `ValueError` before any Appwrite call is made.

### Model-aware filtering
- `get_predictions_by_metadata` automatically adds a `name` filter set to `self.model_path.model_name` when the model path has a `model_name` attribute. This scopes all metadata queries to the current model.
- `list_all_predictions` explicitly filters by `model_name`.
- `list_all_predictions_unfiltered` bypasses this filter for debugging purposes.

### Latest-file resolution
- `get_latest_file_id` returns the `fileId` of the newest prediction matching the given filters (sorted by `$createdAt` descending).
- `get_latest_file_metadata` returns `Optional[PredictionFileMetadata]` — the file ID plus `$createdAt`/`$updatedAt` timestamps for the newest matching prediction. Returns `None` when no predictions match.
- `get_latest_provenance` returns `Optional[PredictionProvenance]` — the lineage of the newest matching artifact: its identity (file ID, hash, `$createdAt`, filename), the declared upstream `source`/`pipeline` (`"unknown"` if unstamped), and the faoapi `methodology_version` (from `views_crafdapi.methodology.METHODOLOGY_VERSION`, ADR-023) that computes the published HDI/MAP. The serving layer logs this when an artifact enters service and exposes it at `GET /provenance/{category}`, so a silent viewser→datafactory source switch or methodology re-baseline is auditable (C-86).
- `download_latest_file` combines latest-file resolution with download in a single call.

#### Quarantine / rollback override (C-71)
- `get_predictions_by_metadata` excludes any document whose `fileId` is listed in the `APPWRITE_UNFAO_QUARANTINED_FILE_IDS` environment variable (comma-separated bucket file IDs) **before** sorting. All latest-file resolution flows through this method, so quarantining the current "latest" file rolls selection back to the previous known-good upload — reversibly and without deleting anything from Appwrite. The env var is read at selection time (no redeploy needed); empty/unset means no quarantine. Each exclusion is logged at WARNING level.
- `get_predictions_by_metadata` also honours an optional **approval allowlist** `APPWRITE_UNFAO_APPROVED_FILE_IDS` (comma-separated bucket file IDs), applied after the quarantine filter: when **non-empty**, only listed files are eligible for selection (a proactive promote-to-production gate — a new upload is not served until approved); when **unset/empty**, selection is unrestricted (the default, behaviour-preserving). Quarantine (blocklist) and approval (allowlist) compose — a file must be approved *and* not quarantined to be selected.
- **Manifest-first: quarantine the manifest = whole-run rollback (ADR-013 §4.4).** For a wire-contract forecast run, the same blocklist operates at *run* granularity: selection is manifest-first, so quarantining the **run manifest's** fileId makes `get_latest_manifest` return the previous run's manifest — an atomic run-level rollback across the run's 100+ shards by quarantining a single file (the env blocklist applies on restart, which re-reads it). Independently, `DatasetService` keys the served cache on the manifest `fileId` and re-checks it each request (S5/#207), so a newly *published* run is picked up promptly without a restart. Operators act on manifests, never shards. See `reports/ops/forecast_serving.md`.

### Upload resilience
- `upload_predictions` auto-creates the storage bucket if the initial upload fails with `storage_bucket_not_found`, then retries the upload.

### Result wrapping
- All operations return `OperationResult` objects (or lists of dicts for query methods), providing a consistent `success`/`error`/`data` interface.

---

## 4. Inputs and Assumptions

### Constructor inputs
- `appwrite_file_manager_config: AppwriteConfig` -- a configuration object that provides:
  - `path_manager` (a `ModelPathManager` instance with a `model_name` attribute).
  - `bucket_id`, `bucket_name` for storage operations.
  - `collection_name`, `collection_id`, `database_id` for metadata operations.
  - Appwrite connection credentials (project ID, endpoint, API key).

### Assumptions
- The Appwrite service is reachable and authenticated via the credentials in the config.
- The database and metadata collection referenced by the config already exist.
- The storage bucket may or may not exist (auto-created on first upload if missing).
- `model_path.model_name` is set and meaningful for scoping predictions to a model.
- Files uploaded as `Path` or `str` point to valid, readable files on the local filesystem.

---

## 5. Outputs and Side Effects

### Outputs
- `upload_predictions(...)` returns `OperationResult` with upload status and file metadata on success.
- `download_prediction(...)` returns `OperationResult`; on success, the file is written to `save_path` or a default cache location.
- `download_latest_file(...)` returns `OperationResult` for the newest matching file.
- `get_latest_file_id(...)` returns `Optional[str]` -- the file ID or `None`.
- `get_latest_file_metadata(...)` returns `Optional[PredictionFileMetadata]` -- file ID with creation/update timestamps, or `None`.
- `get_latest_provenance(...)` returns `Optional[PredictionProvenance]` -- a lineage record (file ID, declared upstream `source`/`pipeline`, hash, timestamp, filename, name, category, targets, description) for the newest matching artifact, or `None` (C-86).
- `get_predictions_by_metadata(...)` returns `List[Dict]` sorted by `$createdAt` descending.
- `list_all_predictions()` returns `List[Dict]` filtered by current model name.
- `list_all_predictions_unfiltered()` returns `List[Dict]` with no model filter.
- `get_file_metadata(...)` returns `OperationResult` with the metadata document.
- `update_prediction_metadata(...)` returns `OperationResult` with update status.
- `delete_prediction(...)` returns `OperationResult` with deletion status.

### Side effects
- **File system:** `download_prediction` and `download_latest_file` write files to disk at the specified `save_path` or a cache directory.
- **Remote state:** `upload_predictions` creates files and metadata documents in Appwrite. `delete_prediction` removes files from Appwrite storage and their associated metadata documents. `update_prediction_metadata` modifies metadata documents.
- **Bucket creation:** `upload_predictions` may create a new storage bucket in Appwrite if one does not exist.
- **Logging:** All operations log informational and error messages via the module-level `logger`.
- **Caching:** Downloads support local caching via `use_cache` and `validate_cache` parameters, delegated to `AppWriteFileManager`.

---

## 6. Failure Modes and Loudness

### Errors raised
- `TypeError` from `PredictionMetadata` if `loa`, `name`, `type`, `targets`, or `description` have incorrect types.
- `ValueError` from `PredictionMetadata` if `category` is not `"forecast"` or `"historical"`.
- `FileNotFoundError` from `download_latest_file` if no predictions match the given filters.
- `NotImplementedError` from `upload_predictions` if a `pd.DataFrame` is passed directly as the `file` argument (not yet supported).
- `TypeError` from `upload_predictions` if `file` is not a `Path`, `str`, or `pd.DataFrame`.

### Soft failures (OperationResult)
- Appwrite API failures (network errors, permission errors, missing resources) are returned as `OperationResult(success=False, error=...)` rather than raised as exceptions, except where noted above.
- `get_predictions_by_metadata` returns an empty list if the search fails or finds no matching documents.
- `get_latest_file_id` returns `None` if no matching files exist.
- `get_file_metadata` returns `OperationResult(success=False, code="NOT_FOUND")` if no metadata exists for the given file ID, and `OperationResult(success=False, code="UNKNOWN_ERROR")` for unexpected exceptions.

### Must never fail silently
- Metadata validation errors on upload (must raise before any Appwrite call).
- Missing files when `download_latest_file` is called (must raise `FileNotFoundError`).
- Failed bucket creation during auto-retry (must return `OperationResult(success=False)`).

---

## 7. Boundaries and Interactions

### Layer
Infrastructure layer (`src/views_crafdapi/managers/`).

### Depends on
- `AppWriteFileManager` and `AppwriteConfig` (`src/views_crafdapi/managers/appwrite/`) -- all Appwrite storage and metadata operations are delegated to this component. Treated as a trusted infrastructure wrapper.
- `ModelPathManager` (`src/views_crafdapi/managers/model.py`) -- provides `model_name` for automatic query scoping.
- `OperationResult` (`src/views_crafdapi/managers/appwrite/`) -- the standard result envelope.
- `PredictionMetadata` (defined in the same module) -- validates metadata before upload.
- `PredictionFileMetadata` (defined in the same module) -- dataclass wrapping `file_id`, `created_at`, `updated_at` for latest-file resolution with timestamps.

### Called by
- `CrafdApiManager` (or equivalent orchestrator) for prediction upload, download, listing, and management operations.

### Must not depend on
- Domain layer (`data/`) -- `PredictionStoreManager` must not import or reference `ForecastDataset`, `_ViewsDataset`, `PosteriorDistributionAnalyzer`, or any statistical/analytical components.
- API/HTTP layer -- this class does not handle routing or request parsing.

---

## 8. Examples of Correct Usage

### Uploading a prediction file with validated metadata
```python
from views_crafdapi.managers.prediction import PredictionStoreManager
from views_crafdapi.managers.appwrite import AppwriteConfig

config = AppwriteConfig(...)
manager = PredictionStoreManager(appwrite_file_manager_config=config)

result = manager.upload_predictions(
    file="/path/to/predictions.parquet",
    filename="predictions_2026_05.parquet",
    loa="pgm",
    name="fatalities_model_v3",
    type="ensemble",
    targets=["pred_ln_sb_best"],
    category="forecast",
    description="May 2026 forecast run",
)
assert result.success
```

### Downloading the latest prediction matching filters
```python
result = manager.download_latest_file(
    filters={"loa": "pgm", "category": "forecast"},
    save_path="/tmp/latest_prediction.parquet",
)
# result.success is True and file is at /tmp/latest_prediction.parquet
```

---

## 9. Examples of Incorrect Usage

### Passing a DataFrame directly as the file argument
```python
# WRONG: DataFrame upload is not implemented
import pandas as pd
df = pd.DataFrame({"pred_ln_sb_best": [[1, 2, 3]]})
result = manager.upload_predictions(
    file=df,  # Raises NotImplementedError
    filename="test.parquet",
    loa="pgm",
    name="model",
    type="ensemble",
    targets=["pred_ln_sb_best"],
    category="forecast",
)
```

### Skipping metadata validation by calling AppWriteFileManager directly
```python
# WRONG: Bypassing PredictionStoreManager to upload without metadata validation
manager._PredictionStoreManager__appwrite_file_manager.upload_file_with_metadata(
    bucket_id="...",
    file_path="/path/to/file",
    filename="file.parquet",
    metadata={"loa": 123},  # Invalid: loa should be str, no validation happens
    collection_name="...",
    collection_id="...",
)
# Always use upload_predictions() to ensure metadata is validated via PredictionMetadata.
```

### Assuming list_all_predictions returns all models' predictions
```python
# WRONG: Expecting cross-model results
all_preds = manager.list_all_predictions()
# This only returns predictions for the current model (filtered by model_name).
# Use list_all_predictions_unfiltered() to see all predictions across models.
```

---

## 10. Test Alignment

### Invariants that tests must enforce
- `PredictionMetadata` raises `TypeError` for each invalid field type (`loa` as int, `targets` as string, etc.).
- `PredictionMetadata` raises `ValueError` when `category` is not `"forecast"` or `"historical"`.
- `upload_predictions` calls `PredictionMetadata` validation before any Appwrite interaction (mock Appwrite and verify no calls on invalid metadata).
- `upload_predictions` auto-creates bucket on `storage_bucket_not_found` error and retries.
- `download_latest_file` raises `FileNotFoundError` when no files match filters.
- `get_predictions_by_metadata` adds `model_name` filter automatically.
- `list_all_predictions_unfiltered` does not add `model_name` filter.
- `get_latest_file_id` returns `None` when no files match, not an exception.
- `get_latest_file_metadata` returns `None` when no files match or the latest file has no `fileId`.
- `get_predictions_by_metadata` excludes file IDs listed in `APPWRITE_UNFAO_QUARANTINED_FILE_IDS`; quarantining the newest file makes the next-newest the selected "latest" (C-71 rollback).
- `get_predictions_by_metadata` restricts to `APPWRITE_UNFAO_APPROVED_FILE_IDS` when that allowlist is non-empty; an unset allowlist is unrestricted (C-71 proactive gate).
- `get_latest_provenance` reports `source="unknown"` when the artifact has no `source`/`pipeline` field, carries `methodology_version`, and returns `None` when no file matches or the latest has no `fileId` (C-86 / ADR-023).
- All 12 public methods return the documented types.

### Test files
- `tests/test_datastore_manager.py` — `PredictionMetadata` validation, `PredictionStoreManager` method behavior, `TestQuarantineRollback` (C-71 quarantine/rollback), `TestApprovalAllowlist` (C-71 proactive gate), `TestLatestProvenance` + `TestMethodologyVersionInProvenance` (C-86 lineage / ADR-023 version)
- `tests/test_staleness_detection.py::TestPredictionFileMetadata` — `get_latest_file_metadata` and `PredictionFileMetadata` dataclass

### Test categories
- **Green tests:** `PredictionMetadata` validation logic, method return types, filter construction.
- **Beige tests:** Appwrite interaction patterns (requires mocking `AppWriteFileManager`).
- **Red tests:** Integration with live Appwrite instance for upload/download/delete round-trips.

---

## 11. Evolution Notes

### Expected to change
- `upload_predictions` will likely gain `pd.DataFrame` support (currently raises `NotImplementedError`).
- Additional metadata fields may be added to `PredictionMetadata` as the prediction catalog grows.
- Caching strategy for downloads may be refined or made configurable.

### Considered stable
- The `PredictionMetadata` validation gate on uploads.
- The model-aware filtering pattern via `model_path.model_name`.
- The `OperationResult` return convention.
- The 11-method public API surface.

### Changes requiring contract revision
- Adding direct DataFrame serialization and upload support.
- Changing from model-name-based scoping to a different identity scheme.
- Replacing Appwrite with a different storage backend (would require updating all delegation patterns).

---

## End of Contract

This document defines the **intended meaning** of `PredictionStoreManager`.

Changes to behavior that violate this intent are bugs.
Changes to intent must update this contract.
