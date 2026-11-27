# Class Intent Contract: AppWriteFileManager

**Status:** Active  
**Owner:** Project maintainers  
**Last reviewed:** 2026-06-03  
**Related ADRs:** ADR-001 (Infrastructure category), ADR-008 (logs errors before returning failure results), ADR-009 (boundary contract with Appwrite SDK), ADR-018 (SDK response normalization)  

---

## 1. Purpose

> **What is this class for?**

`AppWriteFileManager` downloads files from Appwrite storage and caches them locally, and lists buckets (the partner key check). It abstracts Appwrite's storage and authentication APIs behind `OperationResult`. It only reads.

---

## 2. Non-Goals (Explicit Exclusions)

- This class does **not** perform prediction-specific logic. That responsibility belongs to `PredictionStoreManager`, which wraps this class.
- This class does **not** validate file contents. It stores and retrieves opaque bytes. It has no knowledge of file formats, schemas, or data semantics.
- This class does **not** own data semantics. Metadata keys and values are caller-defined; the class stores them as-is.
- This class does **not** expose raw Appwrite SDK types to callers. All returns are wrapped in `OperationResult`.
- This class does **not** manage user creation, project configuration, or Appwrite console operations. It operates within an existing Appwrite project.
- This class does **not** implement retry logic for transient Appwrite errors.
- This class does **not** write to Appwrite. Uploading, changing, deleting and creating buckets or tables were removed (#311): this service only reads.

---

## 3. Responsibilities and Guarantees

- **Cache validation:** Downloaded files are cached locally with a configurable TTL. Cache is invalidated if the remote file's `$updatedAt` timestamp is newer than the cached version, or if the TTL has expired.
- **Cached files stay inside the cache folder (#216):** a downloaded file is cached at `<cache folder>/<bucket id>/<file id>`, built from the ids only, never from the file's name in the store (which may contain `..` or start with `/`). Every read and delete derives the path the same way; the `path` recorded in `cache_metadata.json` is never read. An id that is not usable as one file name (`""`, `.`, `..`, or containing `/`) is refused, never shortened, and the download is served without caching; so is one whose bucket folder cannot be created. The bucket folder must lie strictly below the cache folder. Entries are keyed `<bucket id>/<file id>`, so two pairs never share a key. Entries cached before this change, under the store's file name, are a miss and are downloaded again. A bucket folder that is a link is refused, and a cached file that is a link is never read. Ids with control characters are refused. Entries written before #216 are dropped when the cache loads, and their files deleted if they lie inside the cache folder. `cache_metadata.json` is written to a temporary file and moved into place. Not covered: a process that can already write inside the cache folder could create a link between the check and the write; only the service account can write there.
- **Cached downloads are whole or absent (C-288, #169):** `download_file` writes the cache file to a unique temp name, fsyncs it and `os.replace`s it into place, so a kill mid-write leaves the previous copy or none, never a torn file. A body whose length differs from the store's declared `sizeOriginal` is refused (`DOWNLOAD_INCOMPLETE`; the caller sees a failed download) and nothing is cached. A body whose size cannot be checked — the metadata lookup failed — is served but not cached. A cached file whose size differs from the `size_bytes` recorded when it was cached — read once from the file just written and checked, so it is the size of a whole copy — makes `validate_cache` return `INVALID_SIZE`, so the next download re-fetches, and `get_cached_file_path` return `CACHE_FILE_WRONG_SIZE` (the `/files/{bucket}/{file}/cached` route that answered it with 409 was removed 2026-11-11, C-269). That holds for the re-encoded JSON manifest too. A file removed between lookups is a miss, not an error. A hard kill mid-write can leave a `<name>.tmp-<hex>` file in the bucket directory; nothing reads it, and nothing removes it.
- **OperationResult pattern:** All public methods return `OperationResult(success, data, error, code)`. Methods never raise exceptions on Appwrite SDK errors; they catch `AppwriteException` and wrap it in a failure result. The only exception is the constructor, which raises `ValueError` if authentication fails.
- **Authentication abstraction:** Supports both API key and session-based authentication via `AuthFactory`, selected by `AppwriteConfig.auth_method`.
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
  - `timeout_seconds` (int, default `DEFAULT_TIMEOUT_SECONDS`=30): Network timeout for all Appwrite SDK HTTP calls. Applied as `(connect=min(timeout_seconds, DEFAULT_CONNECT_TIMEOUT_SECONDS), read=timeout_seconds)` via per-instance `client.call` wrapping.
- **Appwrite server reachability:** The constructor establishes a client connection and authenticates. If authentication fails, the constructor raises `ValueError`.
- **Bucket existence:** Download operations assume the target bucket exists.

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
- **Status codes include:** `"SAVED_FROM_CACHE"`, `"RETURNED_FROM_CACHE"`, `"SAVED_FROM_REMOTE"`, `"RETURNED_FROM_REMOTE"`, `"NOT_FOUND"`, `"IO_ERROR"`, `"storage_bucket_not_found"`, and Appwrite error type strings.
- **Side effects:**
  - **Remote:** None. This class never writes to Appwrite.
  - **Local:** Creates and manages a local file cache directory. Writes cache metadata JSON. Downloads files to cache before returning bytes or copying to save path.
  - **Logging:** Errors are logged at ERROR level before returning failure results. Warnings logged for cache failures, fallback behaviors, and partial successes. Info/debug for normal operations.

### 5.1 Additional Public Methods

The following public methods are grouped by domain. All return `OperationResult` unless otherwise noted.

#### Storage Operations

- **`list_files(bucket_id, queries=None, limit=DEFAULT_PAGE_LIMIT, offset=0, order_field=None, order_type="ASC")`**
  Lists files in a storage bucket with pagination. Accepts optional Appwrite query filters, limit/offset for paging, and optional ordering by field name. Returns `data={"files": [...], "total": int}`.

- **`get_file(bucket_id, file_id)`**
  Retrieves file metadata (not the file content) from Appwrite storage. Returns the Appwrite file object as a plain dict. Used internally by `download_file()` for cache validation.

- **`get_bucket(bucket_id)`**
  Retrieves bucket metadata from Appwrite storage. Returns `code="storage_bucket_not_found"` when the bucket does not exist, allowing callers to distinguish "not found" from other errors.

#### Cache Operations

None. `clear_cache` and `get_cache_stats` were removed 2026-11-11 with the routes that called them: the file cache is shared by every key, so no per-key manager may read or empty it (C-269).

#### Bucket Operations

- **`list_buckets(search=None, limit=DEFAULT_PAGE_LIMIT, offset=0)`**
  Lists storage buckets with pagination and optional search. Returns `data={"buckets": [...], "total": int}`.

---

## 6. Failure Modes and Loudness

- **Constructor `ValueError`:** Raised if `AuthManager.setup()` returns a failure result. This is the only method that raises an exception. All other methods return `OperationResult(success=False)`.
- **Appwrite SDK errors:** All `AppwriteException` instances are caught, logged, and wrapped in `OperationResult(success=False, error=..., code=e.type)`. The class never leaks Appwrite exceptions to callers.
- **IO errors:** File system errors during download/cache operations are caught and returned as `OperationResult(success=False, code="IO_ERROR")`.
- **Cache degradation:** If cache setup fails (e.g., permission denied on directory), the class falls back to `.appwrite_cache` in the working directory and logs a warning. Cache is non-critical; cache misses trigger fresh downloads.
- **Must never fail silently:** Authentication failures and download failures are always surfaced through the `OperationResult` pattern with error messages and codes.

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
  - `MetadataManager` -- searches the metadata table for delivered files.
- **Must not depend on:** Domain layer (`data/`), Observability layer (`wandb/`), or application-level routing/API logic.
- **Trusts:** Appwrite SDK to handle network communication, request signing, and protocol compliance. SDK responses are normalized to plain dicts via `_as_dict()` before use --- the class does not trust the SDK to return a consistent type across versions. See ADR-018.

---

## 8. Examples of Correct Usage

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

---

## 10. Test Alignment

- **Green tests (must pass):**
  - Constructor correctly initializes Appwrite client with endpoint and project ID.
  - API key authentication sets the key on the client.
  - Session authentication creates an email/password session and stores the user ID.
  - `download_file()` returns cached content when cache is valid.
  - All methods return `OperationResult` with appropriate success/failure states.
  - SDK response normalization: `_as_dict()` correctly flattens Document, Preferences, File, Bucket, and SimpleNamespace objects to plain dicts. `_get()` resolves both `$`-prefixed aliases and regular keys.
  - Test file: `tests/test_sdk_compat.py` (21 contract tests using real SDK models).
  - Test file: `tests/test_appwrite_manager.py` (`TestAppWriteFileManager` class).

- **Beige tests (behavioral expectations):**
  - Cache TTL expiry triggers fresh download on next access.
  - `AppwriteConfig.__post_init__()` derives `bucket_name` and `database_name` from IDs when not provided.
  - Test file: `tests/test_appwrite_manager.py` (`TestAppwriteConfig` class).

- **Red tests (regression guards):**
  - `OperationResult` pattern: no Appwrite exceptions leak to callers from any public method (except constructor).
  - Test file: `tests/test_datastore_manager.py` (integration-level tests via `PredictionStoreManager`).

---

## 11. Evolution Notes

- **Stable:** The `OperationResult` return pattern is a cross-cutting contract used by all callers. Changing its shape requires updating `PredictionStoreManager`, `CrafdApiManager`, and all tests.
- **Stable:** The `AppwriteConfig` dataclass is the single configuration entry point. Its fields are referenced by `PredictionStoreManager` and `CrafdApiManager`.
- **Candidate for change (resolved):** The commented-out code blocks were removed in a prior cleanup pass.
- **Stable (conditional):** The dual-SDK normalization layer (`_as_dict`, `_get`) is a current constraint while production runs SDK 13.3.0. When production upgrades to SDK 19.x, the dict passthrough branch remains useful but the layer could be simplified. Any modification to `_as_dict()` must preserve the `to_dict()` + flatten strategy for models with `_data` PrivateAttr --- reverting to `model_dump()` reintroduces C-22.
- **Would require contract revision:** Adding async/await support, switching to a different storage backend, changing the deduplication strategy (e.g., content-addressable storage), or removing the `OperationResult` pattern.

---

## End of Contract

This document defines the **intended meaning** of `AppWriteFileManager`.

Changes to behavior that violate this intent are bugs.  
Changes to intent must update this contract.
