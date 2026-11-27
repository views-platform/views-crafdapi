# Class Intent Contract: PredictionStoreManager

**Status:** Active
**Owner:** Project maintainers
**Last reviewed:** 2026-06-03
**Related ADRs:** ADR-003 (fail loud: the filter refusals), ADR-006 (this contract), ADR-028 (the producer's artifact is a checked input: C-71 quarantine/allowlist, C-86 lineage), ADR-033 (§2 manifest-first selection; §6a degraded serving); views-postprocessing ADR-013 §4.3/§11.4 (wire-run resolution, the Hop-B transition guard — the producer's ADR, not this repo's ADR-013)

---

## 1. Purpose

`PredictionStoreManager` finds and downloads delivered forecast files by wrapping `AppWriteFileManager` with model-aware filtering and latest-file resolution. It only reads.

It acts as the prediction-domain facade over Appwrite's generic file storage, ensuring that queries are automatically scoped to the current model.

**Location:** `src/views_crafdapi/managers/prediction/manager.py`

---

## 2. Non-Goals (Explicit Exclusions)

- This class does **not** compute statistics on predictions (HDI, MAP, or any distributional analysis -- that responsibility belongs to `PosteriorDistributionAnalyzer` and `ForecastDataset`).
- This class does **not** manage `DataFrame` or tensor representations of prediction data.
- This class does **not** handle API routing, HTTP request/response lifecycle, or authentication.
- This class does **not** interpret or transform prediction data contents; it treats prediction files as opaque binary blobs with structured metadata.
- This class does **not** write to Appwrite. Upload, change and delete were removed (#311).
- This class does **not** manage Appwrite infrastructure (database/collection creation, schema migration). It assumes the required database, collection, and bucket infrastructure exists or can be auto-created for buckets only.

---

## 3. Responsibilities and Guarantees

### Model-aware filtering
- `get_predictions_by_metadata` automatically adds a `name` filter set to `self.model_path.model_name` when the model path has a `model_name` attribute. This scopes all metadata queries to the current model.

### Latest-file resolution
- `get_latest_file_id` returns the `fileId` of the newest prediction matching the given filters (sorted by `$createdAt` descending).
- `get_latest_provenance` returns `Optional[PredictionProvenance]` — the lineage of the newest matching artifact: its identity (file ID, hash, `$createdAt`, filename), the declared upstream `source`/`pipeline` (`"unknown"` if unstamped), and the faoapi `methodology_version` (from `views_crafdapi.methodology.METHODOLOGY_VERSION`, ADR-023) that computes the published HDI/MAP. The serving layer logs this when an artifact enters service (no route returns it since #207), so a silent viewser→datafactory source switch or methodology re-baseline is auditable (C-86).

#### Quarantine / rollback override (C-71)
- `get_predictions_by_metadata` excludes any document whose `fileId` is listed in the `APPWRITE_CRAFD_QUARANTINED_FILE_IDS` environment variable (comma-separated bucket file IDs) **before** sorting. All latest-file resolution flows through this method, so quarantining the current "latest" file rolls selection back to the previous known-good upload — reversibly and without deleting anything from Appwrite. The env var is read at selection time (no redeploy needed); empty/unset means no quarantine. Each exclusion is logged at WARNING level.
- `get_predictions_by_metadata` also honours an optional **approval allowlist** `APPWRITE_CRAFD_APPROVED_FILE_IDS` (comma-separated bucket file IDs), applied after the quarantine filter: when **non-empty**, only listed files are eligible for selection (a proactive promote-to-production gate — a new upload is not served until approved); when **unset/empty**, selection is unrestricted (the default, behaviour-preserving). Quarantine (blocklist) and approval (allowlist) compose — a file must be approved *and* not quarantined to be selected.
- **Manifest-first: quarantine the manifest = whole-run rollback (ADR-013 §4.4).** For a wire-contract forecast run, the same blocklist operates at *run* granularity: selection is manifest-first, so quarantining the **run manifest's** fileId makes `get_latest_manifest` return the previous run's manifest — an atomic run-level rollback across the run's 100+ shards by quarantining a single file (the env blocklist applies on restart, which re-reads it). Independently, `DatasetService` keys the served cache on the manifest `fileId` and re-checks it each request (S5/#207), so a newly *published* run is picked up promptly without a restart. Operators act on manifests, never shards. See `reports/ops/forecast_serving.md`.

### Result wrapping
- All operations return `OperationResult` objects (or lists of dicts for query methods), providing a consistent `success`/`error`/`data` interface.

---

## 4. Inputs and Assumptions

### Constructor inputs
- `appwrite_file_manager_config: AppwriteConfig` -- a configuration object that provides:
  - `path_manager` (an `APIPathManager` instance with a `model_name` attribute).
  - `bucket_id`, `bucket_name` for storage operations.
  - `collection_name`, `collection_id`, `database_id` for metadata operations.
  - Appwrite connection credentials (project ID, endpoint, API key).

### Assumptions
- The Appwrite service is reachable and authenticated via the credentials in the config.
- The database and metadata collection referenced by the config already exist.
- The storage bucket exists (the producer's delivery created it).
- `model_path.model_name` is set and meaningful for scoping predictions to a model.

---

## 5. Outputs and Side Effects

### Outputs
- `download_prediction(...)` returns `OperationResult`; on success, the file is written to `save_path` or a default cache location.
- `get_latest_file_id(...)` returns `Optional[str]` -- the file ID or `None`.
- `get_latest_provenance(...)` returns `Optional[PredictionProvenance]` -- a lineage record (file ID, declared upstream `source`/`pipeline`, hash, timestamp, filename, name, category, targets, description) for the newest matching artifact, or `None` (C-86).
- `get_predictions_by_metadata(...)` returns `List[Dict]` sorted by `$createdAt` descending.

### Side effects
- **File system:** `download_prediction` writes files to disk at the specified `save_path` or a cache directory.
- **Remote state:** None. This class never writes to Appwrite.
- **Logging:** All operations log informational and error messages via the module-level `logger`.
- **Caching:** Downloads support local caching via `use_cache` and `validate_cache` parameters, delegated to `AppWriteFileManager`.

---

## 6. Failure Modes and Loudness

### Errors raised
- `TypeError` from `PredictionMetadata` if `loa`, `name`, `type`, `targets`, or `description` have incorrect types.
- `ValueError` from `PredictionMetadata` if `category` is not `"forecast"` or `"historical"`.

### A failed search raises; an empty one does not (C-233, #158)
- `get_predictions_by_metadata` raises **`MetadataLookupError`** when the metadata search fails — an Appwrite HTTP error, a network error or a timeout (which the pinned SDK all report as the same `AppwriteException`), or any other exception raised inside the search. The error carries `status` (the HTTP status: 401/403 not authorised, 0/None no response, 5xx server-side), `appwrite_type`, and `is_unauthorized`. It returns `[]` **only** when the search succeeded and matched nothing. Every `get_latest_*` helper and `resolve_artifact_file_ids` inherit this: they raise on a failed search and return `None` / `{}` only for a genuine absence.
- *Why:* returning `[]` for both made an outage, a revoked key and "no documents" identical, so the serving path reported an outage as "no manifest" (ADR-008: errors must not be silently swallowed; ADR-033: "run present but unservable" must not look like "no run").
- `get_predictions_by_metadata` copies the caller's `filters`; the name filter and §11.4 type pin never leak into the caller's dict (C-359).

### Soft failures (OperationResult)
- Other Appwrite API failures (on the file surface) are returned as `OperationResult(success=False, error=..., code=<Appwrite type>)` rather than raised, except where noted above. `status` (the HTTP status) is set only by `search_files_by_metadata` and `list_buckets` — the two calls the serving path classifies; elsewhere it is `None`, which there means *not recorded*, not *no response*.
- A missing database/collection id raises `RuntimeError` (a deployment error), not `MetadataLookupError`.
- `get_latest_file_id` returns `None` if no matching files exist.

### Must never fail silently

---

## 7. Boundaries and Interactions

### Layer
Infrastructure layer (`src/views_crafdapi/managers/`).

### Depends on
- `AppWriteFileManager` and `AppwriteConfig` (`src/views_crafdapi/managers/appwrite/`) -- all Appwrite storage and metadata operations are delegated to this component. Treated as a trusted infrastructure wrapper.
- `APIPathManager` (`src/views_crafdapi/managers/model.py`) -- provides `model_name` for automatic query scoping.
- `OperationResult` (`src/views_crafdapi/managers/appwrite/`) -- the standard result envelope.

### Called by
- The forecast loader, through `DatasetService` (`managers/dataset_service.py`), to find and download a delivery; `/health` in `CrafdApiManager`, for the newest manifest's date.

### Must not depend on
- Domain layer (`data/`) -- `PredictionStoreManager` must not import or reference `ForecastDataset`, `_ViewsDataset`, `PosteriorDistributionAnalyzer`, or any statistical/analytical components.
- API/HTTP layer -- this class does not handle routing or request parsing.

---

## 8. Examples of Correct Usage

The loader is the only caller; see `managers/dataset_service.py`.

---

## 9. Examples of Incorrect Usage

None recorded.

---

## 10. Test Alignment

### Invariants that tests must enforce
- `PredictionMetadata` raises `TypeError` for each invalid field type (`loa` as int, `targets` as string, etc.).
- `PredictionMetadata` raises `ValueError` when `category` is not `"forecast"` or `"historical"`.
- `get_predictions_by_metadata` adds `model_name` filter automatically.
- `get_latest_file_id` returns `None` when no files match, not an exception.
- `get_predictions_by_metadata` excludes file IDs listed in `APPWRITE_CRAFD_QUARANTINED_FILE_IDS`; quarantining the newest file makes the next-newest the selected "latest" (C-71 rollback).
- `get_predictions_by_metadata` restricts to `APPWRITE_CRAFD_APPROVED_FILE_IDS` when that allowlist is non-empty; an unset allowlist is unrestricted (C-71 proactive gate).
- `get_latest_provenance` reports `source="unknown"` when the artifact has no `source`/`pipeline` field, carries `methodology_version`, and returns `None` when no file matches or the latest has no `fileId` (C-86 / ADR-023).
- All public methods return the documented types.

### Test files
- `tests/test_datastore_manager.py` — `PredictionMetadata` validation, `PredictionStoreManager` method behavior, `TestQuarantineRollback` (C-71 quarantine/rollback), `TestApprovalAllowlist` (C-71 proactive gate), `TestLatestProvenance` + `TestMethodologyVersionInProvenance` (C-86 lineage / ADR-023 version)

### Test categories
- **Green tests:** `PredictionMetadata` validation logic, method return types, filter construction.
- **Beige tests:** Appwrite interaction patterns (requires mocking `AppWriteFileManager`).
- **Red tests:** none run by default; `tests/test_integration_appwrite.py` needs a live key.

---

## 11. Evolution Notes

### Expected to change
- Additional metadata fields may be added to `PredictionMetadata` as the prediction catalog grows.
- Caching strategy for downloads may be refined or made configurable.

### Considered stable
- The model-aware filtering pattern via `model_path.model_name`.
- The `OperationResult` return convention.

### Changes requiring contract revision
- Changing from model-name-based scoping to a different identity scheme.
- Replacing Appwrite with a different storage backend (would require updating all delegation patterns).

---

## End of Contract

This document defines the **intended meaning** of `PredictionStoreManager`.

Changes to behavior that violate this intent are bugs.
Changes to intent must update this contract.
