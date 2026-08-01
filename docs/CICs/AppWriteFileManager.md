# Class Intent Contract: AppWriteFileManager

**Status:** Active  
**Owner:** Project maintainers  
**Last reviewed:** 2026-06-03  
**Related ADRs:** ADR-001 (Infrastructure category), ADR-008 (logs errors before returning failure results), ADR-009 (boundary contract with Appwrite SDK), ADR-018 (SDK response normalization)  

---

## 1. Purpose

> **What is this class for?**

`AppWriteFileManager` provides file storage, metadata management, and caching through the Appwrite SDK. It abstracts Appwrite's storage, database, and authentication APIs behind a unified file management interface, ensuring that all file operations are hash-deduplicated, metadata-tracked, and locally cached.

---

## 2. Non-Goals (Explicit Exclusions)

- This class does **not** perform prediction-specific logic. That responsibility belongs to `PredictionStoreManager`, which wraps this class.
- This class does **not** validate file contents. It stores and retrieves opaque bytes. It has no knowledge of file formats, schemas, or data semantics.
- This class does **not** own data semantics. Metadata keys and values are caller-defined; the class stores them as-is.
- This class does **not** expose raw Appwrite SDK types to callers. All returns are wrapped in `OperationResult`.
- This class does **not** manage user creation, project configuration, or Appwrite console operations. It operates within an existing Appwrite project.
- This class does **not** implement retry logic for transient Appwrite errors at the file operation level (retries exist only for attribute creation during schema setup).

---

## 3. Responsibilities and Guarantees

- **Hash-based deduplication:** Before uploading, files are checked by SHA-256 hash. If a file with the same hash already exists in the metadata collection, the upload is skipped and existing metadata is returned (or updated, if `allow_metadata_only_updates` is enabled in config). Optionally, duplicates can be overwritten.
- **Metadata consistency:** Every file upload via `upload_file_with_metadata()` stores a metadata document in an Appwrite database collection alongside the file in storage. Metadata includes `fileId`, `bucketId`, `filename`, `file_hash`, `uploaded_at`, and any caller-supplied fields.
- **Orphan cleanup:** When metadata references a file that no longer exists in storage, the orphaned metadata document is deleted before re-uploading.
- **Cache validation:** Downloaded files are cached locally with a configurable TTL. Cache is invalidated if the remote file's `$updatedAt` timestamp is newer than the cached version, or if the TTL has expired.
- **OperationResult pattern:** All public methods return `OperationResult(success, data, error, code)`. Methods never raise exceptions on Appwrite SDK errors; they catch `AppwriteException` and wrap it in a failure result. The only exception is the constructor, which raises `ValueError` if authentication fails.
- **Authentication abstraction:** Supports both API key and session-based authentication via `AuthFactory`, selected by `AppwriteConfig.auth_method`.
- **Metadata schema auto-creation:** Database, collection, and attributes are created automatically if they do not exist, using `MetadataManager.create_metadata_collection_if_not_exists()`. Dynamic attributes are inferred from metadata dict values.
- **SDK response normalization:** All Appwrite SDK responses are converted to plain dicts via `_as_dict()` at the point of return, before entering `OperationResult.data`. This ensures callers receive dicts regardless of installed SDK version (13.x returns dicts natively; 19.x returns Pydantic models). Governed by ADR-018.

---

## 4. Inputs and Assumptions

- **`config: AppwriteConfig`** (required at construction): A dataclass containing:
  - `endpoint` (str): Appwrite server URL.
  - `project_id` (str): Appwrite project ID.
  - `credentials` (str or dict): API key string for `API_KEY` auth, or `{"email": ..., "password": ...}` dict for `SESSION` auth.
  - `auth_method` (AuthMethod, default `API_KEY`): Authentication strategy.
  - `bucket_id` (str, default `"production_forecasts"`): Default storage bucket.
  - `database_id`, `collection_id`, `database_name`, `collection_name`: Metadata storage identifiers.
  - `cache_dir` (optional str): Local cache directory path. Falls back to `path_manager.cache / "appwrite_cache"` or `.appwrite_cache`.
  - `cache_ttl_hours` (int, default 24): Cache time-to-live.
  - `allow_metadata_only_updates` (bool, default True): Whether to update metadata without re-uploading when a hash-identical file already exists.
  - `timeout_seconds` (int, default `DEFAULT_TIMEOUT_SECONDS`=30): Network timeout for all Appwrite SDK HTTP calls. Applied as `(connect=min(timeout_seconds, DEFAULT_CONNECT_TIMEOUT_SECONDS), read=timeout_seconds)` via per-instance `client.call` wrapping.
- **Appwrite server reachability:** The constructor establishes a client connection and authenticates. If authentication fails, the constructor raises `ValueError`.
- **Bucket existence:** Upload and download operations assume the target bucket exists. `create_bucket()` is available but must be called explicitly.
- **Caller-managed metadata keys:** The class auto-creates Appwrite database attributes based on metadata dict keys and inferred types. Callers must not pass metadata keys that conflict with reserved keys (`fileId`, `bucketId`, `filename`, `mime_type`, `uploaded_at`, `file_hash`, `file_size`).

---

## 5. Outputs and Side Effects

- **All public methods return `OperationResult`:**
  ```python
  OperationResult(
      success: bool,
      data: Any,         # Method-specific payload on success
      error: Optional[str],   # Human-readable error message on failure
      code: Optional[str]     # Machine-readable status code
  )
  ```
- **Status codes include:** `"CREATED"`, `"EXISTS"`, `"DELETED"`, `"UPDATED"`, `"UPLOAD_SUCCESS"`, `"METADATA_UPDATED"`, `"PARTIAL_SUCCESS"`, `"SAVED_FROM_CACHE"`, `"RETURNED_FROM_CACHE"`, `"SAVED_FROM_REMOTE"`, `"RETURNED_FROM_REMOTE"`, `"FOUND_BY_HASH"`, `"FOUND_BY_NAME"`, `"NOT_FOUND"`, `"AUTH_METHOD_ERROR"`, `"MISSING_USER_ID"`, `"USER_SESSION"`, `"API_KEY"`, `"MISSING_CONFIG"`, `"storage_bucket_not_found"`, and Appwrite error type strings.
- **Side effects:**
  - **Remote:** Creates/updates/deletes files in Appwrite storage. Creates/updates/deletes documents in Appwrite databases. May create databases, collections, and attributes if they do not exist.
  - **Local:** Creates and manages a local file cache directory. Writes cache metadata JSON. Downloads files to cache before returning bytes or copying to save path.
  - **Logging:** Errors are logged at ERROR level before returning failure results. Warnings logged for cache failures, fallback behaviors, and partial successes. Info/debug for normal operations.

### 5.1 Additional Public Methods

The following public methods are grouped by domain. All return `OperationResult` unless otherwise noted.

#### Storage Operations

- **`upload_file_from_bytes(bucket_id, file_bytes, filename, file_id=None, permissions=None, check_duplicates=True, overwrite=False)`**
  Uploads a file from in-memory bytes (rather than a file path). Supports the same hash-based deduplication and overwrite logic as `upload_file()`. Uses `InputFile.from_bytes()` internally. Does **not** create a metadata document --- use `upload_file_from_bytes_with_metadata()` when metadata tracking is needed.

- **`list_files(bucket_id, queries=None, limit=DEFAULT_PAGE_LIMIT, offset=0, order_field=None, order_type="ASC")`**
  Lists files in a storage bucket with pagination. Accepts optional Appwrite query filters, limit/offset for paging, and optional ordering by field name. Returns `data={"files": [...], "total": int}`.

- **`get_file(bucket_id, file_id)`**
  Retrieves file metadata (not the file content) from Appwrite storage. Returns the Appwrite file object as a plain dict. Used internally by `download_file()` for cache validation and by `upload_file_with_metadata()` for orphan detection.

- **`get_bucket(bucket_id)`**
  Retrieves bucket metadata from Appwrite storage. Returns `code="storage_bucket_not_found"` when the bucket does not exist, allowing callers to distinguish "not found" from other errors.

#### User Operations

- **`get_current_user()`**
  Returns the authenticated user's info (`user_id`, `email`, `name`, `email_verified`). Requires `SessionAuth`; returns `code="AUTH_METHOD_ERROR"` if the manager was constructed with API key authentication.

- **`get_user_preferences(user_id=None)`**
  Returns user preferences. With session authentication, fetches preferences for the current session user (ignores `user_id`). With API key authentication, `user_id` is required; returns `code="MISSING_USER_ID"` if omitted. Response `code` is `"USER_SESSION"` or `"API_KEY"` to indicate the auth path taken.

#### Cache Operations

- **`clear_cache(bucket_id=None, older_than_hours=None)`**
  Clears the local file cache. When `bucket_id` is provided, only that bucket's cached files are removed. When `older_than_hours` is provided, only cache entries older than the threshold are removed. Delegates to `CacheManager.clear_cache()`.

- **`get_cache_stats()`**
  Returns cache statistics as a plain `Dict[str, Any]` (not wrapped in `OperationResult`). Delegates to `CacheManager.get_stats()`. This is the only public method that does not return `OperationResult`.

#### Debug Operations

- **`debug_collection_attributes(collection_id=None, database_id=None)`**
  Inspects the metadata collection schema by listing all Appwrite database attributes. Falls back to `config.collection_id` / `config.database_id` when parameters are omitted. Returns `code="MISSING_CONFIG"` if neither config nor parameters provide the required IDs. Logs each attribute key and type at INFO level as a side effect.

#### Bucket Operations

- **`list_buckets(search=None, limit=DEFAULT_PAGE_LIMIT, offset=0)`**
  Lists storage buckets with pagination and optional search. Returns `data={"buckets": [...], "total": int}`.

- **`create_bucket(bucket_id, name=None, permissions=None, file_security=True, enabled=True, maximum_file_size=None, allowed_file_extensions=None, encryption=False, compression="none", antivirus=True, create_metadata_db=True)`**
  Creates a storage bucket in Appwrite. When `create_metadata_db=True`, also creates the metadata database and collection. Returns the created bucket metadata on success.

#### Combined Upload Operations

- **`upload_file_from_bytes_with_metadata(bucket_id, file_bytes, filename, metadata, file_id=None, permissions=None, collection_name=None, collection_id=None)`**
  Uploads a file from in-memory bytes AND creates a metadata document in a single operation. Unlike `upload_file_with_metadata()`, this method attempts rollback (deleting the uploaded file) if the metadata document write fails.

---

## 6. Failure Modes and Loudness

- **Constructor `ValueError`:** Raised if `AuthManager.setup()` returns a failure result. This is the only method that raises an exception. All other methods return `OperationResult(success=False)`.
- **`PARTIAL_SUCCESS`:** `upload_file_with_metadata()` returns `OperationResult(success=False, code="PARTIAL_SUCCESS", data={"file_id": ...})` when the file was uploaded to storage but the metadata document write failed. Note: `success` is `False` despite the partial completion — callers checking `result.success` will correctly treat this as a failure. The `file_id` is included so the caller can take corrective action. This is logged at ERROR level. Note: `upload_file_from_bytes_with_metadata()` handles this case differently by attempting rollback (deleting the uploaded file) on metadata failure.
- **Appwrite SDK errors:** All `AppwriteException` instances are caught, logged, and wrapped in `OperationResult(success=False, error=..., code=e.type)`. The class never leaks Appwrite exceptions to callers.
- **IO errors:** File system errors during download/cache operations are caught and returned as `OperationResult(success=False, code="IO_ERROR")`.
- **Cache degradation:** If cache setup fails (e.g., permission denied on directory), the class falls back to `.appwrite_cache` in the working directory and logs a warning. Cache is non-critical; cache misses trigger fresh downloads.
- **Must never fail silently:** Authentication failures, upload failures, and metadata write failures are always surfaced through the `OperationResult` pattern with error messages and codes.

---

## 7. Boundaries and Interactions

- **Layer:** Infrastructure (`src/views_crafdapi/managers/appwrite/`).
- **External dependency:** Appwrite Python SDK (`appwrite.client`, `appwrite.services.storage`, `appwrite.services.databases`, `appwrite.services.account`, `appwrite.services.users`). The class wraps all SDK calls and never exposes raw Appwrite types to callers.
- **Callers:**
  - `PredictionStoreManager` (`managers/prediction/manager.py`) -- constructs an `AppWriteFileManager` from `AppwriteConfig` and delegates all file operations to it.
  - `CrafdApiManager` (`managers/api.py`) -- maintains a cache of `AppWriteFileManager` instances keyed by API key hash.
- **Internal composition:**
  - `AuthManager` (via `AuthFactory`) -- handles authentication setup. Abstract base with `ApiKeyAuth` and `SessionAuth` implementations.
  - `CacheManager` -- manages local file cache with TTL validation and metadata persistence.
  - `MetadataManager` -- manages Appwrite database CRUD for file metadata documents, including schema auto-creation.
- **Must not depend on:** Domain layer (`data/`), Observability layer (`wandb/`), or application-level routing/API logic.
- **Trusts:** Appwrite SDK to handle network communication, request signing, and protocol compliance. SDK responses are normalized to plain dicts via `_as_dict()` before use --- the class does not trust the SDK to return a consistent type across versions. See ADR-018.

---

## 8. Examples of Correct Usage

**Upload a file with metadata:**

```python
from views_crafdapi.managers.appwrite import AppWriteFileManager, AppwriteConfig

config = AppwriteConfig(
    endpoint="https://cloud.appwrite.io/v1",
    project_id="my_project",
    credentials="my-api-key",
    bucket_id="production_forecasts",
    database_id="file_metadata",
    collection_id="pipeline_forecasts",
)

manager = AppWriteFileManager(config)

result = manager.upload_file_with_metadata(
    bucket_id="production_forecasts",
    file_path="/tmp/forecast.parquet",
    filename="forecast_2026_05.parquet",
    metadata={"model": "fatalities002", "run_id": "run_abc123"},
)

if result.success:
    file_id = result.data["file_id"]
```

**Download with caching:**

```python
result = manager.download_file(
    bucket_id="production_forecasts",
    file_id="abc123",
    use_cache=True,
)

if result.success:
    if result.code in ("RETURNED_FROM_CACHE", "RETURNED_FROM_REMOTE"):
        file_bytes = result.data["file_bytes"]
```

---

## 9. Examples of Incorrect Usage

**Accessing raw Appwrite SDK objects through the manager:**

```python
# WRONG: Reaching into internal state to call Appwrite SDK directly.
# This bypasses the OperationResult contract and cache/metadata tracking.
manager.storage.create_file(bucket_id, file_id, input_file)
```

All file operations must go through the manager's public methods, which ensure metadata consistency, deduplication, and caching.

**Using upload_file() when metadata tracking is needed:**

```python
# WRONG: upload_file() stores the file but does NOT create a metadata document.
# Use upload_file_with_metadata() instead when metadata tracking is required.
result = manager.upload_file("my_bucket", "/tmp/data.parquet")
# No metadata document exists -- PredictionStoreManager cannot find this file later.
```

**Passing reserved metadata keys that conflict with internal fields:**

```python
# WRONG: "fileId" and "bucketId" are managed internally.
# Passing them in metadata can cause unexpected overwrites.
result = manager.upload_file_with_metadata(
    bucket_id="my_bucket",
    file_path="/tmp/data.parquet",
    filename="data.parquet",
    metadata={"fileId": "my_custom_id", "bucketId": "wrong_bucket"},
)
```

---

## 10. Test Alignment

- **Green tests (must pass):**
  - Constructor correctly initializes Appwrite client with endpoint and project ID.
  - API key authentication sets the key on the client.
  - Session authentication creates an email/password session and stores the user ID.
  - `upload_file()` calls `storage.create_file()` with correct parameters.
  - Hash-based deduplication: `upload_file()` with `check_duplicates=True` skips upload when hash matches.
  - `download_file()` returns cached content when cache is valid.
  - `delete_file()` removes file from storage and cache.
  - All methods return `OperationResult` with appropriate success/failure states.
  - SDK response normalization: `_as_dict()` correctly flattens Document, Preferences, File, Bucket, and SimpleNamespace objects to plain dicts. `_get()` resolves both `$`-prefixed aliases and regular keys.
  - Test file: `tests/test_sdk_compat.py` (21 contract tests using real SDK models).
  - Test file: `tests/test_appwrite_manager.py` (`TestAppWriteFileManager` class).

- **Beige tests (behavioral expectations):**
  - `upload_file_with_metadata()` creates metadata document in database after successful upload.
  - `PARTIAL_SUCCESS` returned when file uploads but metadata write fails.
  - Cache TTL expiry triggers fresh download on next access.
  - `AppwriteConfig.__post_init__()` derives `bucket_name` and `database_name` from IDs when not provided.
  - Test file: `tests/test_appwrite_manager.py` (`TestAppwriteConfig` class).

- **Red tests (regression guards):**
  - Orphaned metadata cleanup: when metadata references a file missing from storage, the metadata document is deleted before re-upload.
  - `OperationResult` pattern: no Appwrite exceptions leak to callers from any public method (except constructor).
  - Test file: `tests/test_datastore_manager.py` (integration-level tests via `PredictionStoreManager`).

---

## 11. Evolution Notes

- **Stable:** The `OperationResult` return pattern is a cross-cutting contract used by all callers. Changing its shape requires updating `PredictionStoreManager`, `CrafdApiManager`, and all tests.
- **Stable:** The `AppwriteConfig` dataclass is the single configuration entry point. Its fields are referenced by `PredictionStoreManager` and `CrafdApiManager`.
- **Candidate for change (resolved):** The commented-out code blocks were removed in a prior cleanup pass.
- **Candidate for change:** `upload_file_from_bytes_with_metadata()` attempts rollback (deleting the uploaded file) on metadata failure, but `upload_file_with_metadata()` does not. This inconsistency may be unified.
- **Stable (conditional):** The dual-SDK normalization layer (`_as_dict`, `_get`) is a current constraint while production runs SDK 13.3.0. When production upgrades to SDK 19.x, the dict passthrough branch remains useful but the layer could be simplified. Any modification to `_as_dict()` must preserve the `to_dict()` + flatten strategy for models with `_data` PrivateAttr --- reverting to `model_dump()` reintroduces C-22.
- **Would require contract revision:** Adding async/await support, switching to a different storage backend, changing the deduplication strategy (e.g., content-addressable storage), or removing the `OperationResult` pattern.

---

## End of Contract

This document defines the **intended meaning** of `AppWriteFileManager`.

Changes to behavior that violate this intent are bugs.  
Changes to intent must update this contract.
