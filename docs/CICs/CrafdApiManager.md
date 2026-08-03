# Class Intent Contract: CrafdApiManager

**Status:** Active  
**Owner:** Project maintainers  
**Last reviewed:** 2026-06-27  
**Related ADRs:** ADR-001 (Infrastructure category), ADR-002 (orchestration layer topology), ADR-008 (logging and observability), ADR-009 (boundary contracts and configuration validation)  

---

## 1. Purpose

> **What is this class for?**

`CrafdApiManager` orchestrates the views-faoapi FastAPI service. It manages the full API lifecycle -- startup, shutdown, endpoint routing, multi-tier caching, and data retrieval -- to deliver VIEWS conflict forecast data to the Complex Risk Analytics Fund (CRAF'd). It is the single top-level entry point that wires together domain logic (`ForecastDataset`), infrastructure services (`PredictionStoreManager`, `AppWriteFileManager`), and the HTTP transport layer (FastAPI/uvicorn).

---

## 2. Non-Goals (Explicit Exclusions)

- This class does **not** perform statistical computation. HDI-MAP calculation and posterior distribution analysis are delegated to `ForecastDataset`, which in turn delegates to `PosteriorDistributionAnalyzer`.
- This class does **not** own data semantics. Column meanings, feature definitions, and index structures are defined in the Domain layer (`data/`). The manager treats DataFrames as opaque tabular data.
- This class does **not** manage the Appwrite SDK directly. All Appwrite interactions (authentication, file upload/download, metadata CRUD) are delegated to `AppWriteFileManager` and `PredictionStoreManager`.
- This class does **not** implement its own authentication. API key validation is performed by constructing an `AppWriteFileManager` and calling `list_buckets()` against Appwrite. The class has no local user store.
- This class does **not** implement custom cache eviction logic. In-memory caches use `cachetools` (`LRUCache`, `TTLCache`) for bounded, automatic eviction. Disk caching is delegated to `CrafdDiskCacheManager`.
- This class does **not** implement the data-fetch pipeline itself (epic #144 / S2). The three-tier fetch (memory → disk → remote download), format-cascade parsing, plausibility validation (C-72), and provenance recording (C-86) are owned by the composed `DatasetService` (`managers/dataset_service.py`, see its CIC). `CrafdApiManager` owns the caches and injects them; `_get_latest_dataframe`/`_get_latest_dataset` are thin delegators.

---

## 3. Responsibilities and Guarantees

- **API lifecycle management:** Constructs a fully configured FastAPI application with all routes registered during `__init__`. Provides lifespan hooks that clear the in-memory caches on shutdown (the durable on-disk cache persists, C-66). Handles SIGINT/SIGTERM for graceful termination.
- **Three-tier cache:** Every data request is served through a three-tier cache hierarchy:
  1. In-memory bounded caches (fastest, per-worker process, keyed by API key hash + category). `_manager_cache`: `LRUCache(100)`, `_dataframe_cache`: `TTLCache(50, ttl=4h)`, `_file_cache`: `LRUCache(20)`.
  2. Disk **value store** via `CrafdDiskCacheManager` (`managers/disk_cache.py`) — the dataset's columnar value (arrow/npz frame arrays + geo table + index + manifest, via `ForecastDataset.to_value`/`from_value`; **no pickle**, C-149), cross-worker, `FileLock`-serialized, TTL-validated at 3.5 weeks. The schema version is derived from the value format + meta layout, **decoupled from the class signature** (C-138). Partition directories are labelled by a salted HMAC of the API-key-hash (ADR-031/#323), not the raw key hash.
  3. Remote Appwrite download via `PredictionStoreManager` (slowest, authoritative source).
  Cache hits at any tier populate all higher tiers.
- **Per-API-key isolation:** Manager instances, DataFrames, and dataset objects are cached per API-key-hash. Different API keys never share cached data (except the global file-ID-to-bytes cache, which is content-addressed).
- **Endpoint routing:** Registers approximately 30 endpoints across five geographic levels (pg, country, gaul0, gaul1, gaul2) times two categories (historical, forecast) for both subset and HDI-MAP analysis, plus file management, cache management, provenance (`GET /provenance/{category}`), and health check endpoints.
- **Provenance/lineage (C-86 / ADR-023):** When an artifact is brought into service, `_get_latest_dataframe` resolves and logs a `PredictionProvenance` record (file ID, declared upstream `source`/`pipeline`, faoapi `methodology_version`, hash, timestamp) and stores it on the dataframe cache. `GET /provenance/{category}` (category ∈ {forecast, historical}) exposes this lineage without fetching the dataframe; a `source` of `"unknown"` means the producer did not stamp a provenance field. Returns 422 for an invalid category, 404 when no artifact matches.
- **`/provenance/forecast` full served decision (S7/#252, ADR-033 §5 observability):** for forecasts the response reports the run **actually being served** — `{artifact_id, mode ("wire"/"legacy"), status (producer-declared maturity), source, freshness (verdict vs the SLA), serving_state, refusal_reason?}` — sourced from `DatasetService.served_forecast_provenance()` + `forecast_serving_state()`, which are authoritative over the store's newest record (they may differ). `refusal_reason` appears only while a bounded grace fallback is active. Before any forecast has been served, it falls back to the store's newest provenance (`mode: null`).
- **Value/metadata plausibility (C-72):** Before caching/serving, `_get_latest_dataframe` calls `dataset.validate_value_plausibility()` (prediction samples finite + non-negative) and `dataset.validate_metadata_plausibility()` (coordinate ranges, ISO3 shape, non-negative GAUL codes). A violation is surfaced as HTTP 500 — implausible data fails loud rather than reaching FAO.
- **Error translation:** All exceptions within endpoint handlers are caught and translated to appropriate HTTP status codes (401 for authentication failures, 404 for missing files, 422 for invalid query parameters, 500 for unhandled errors, 503 for health check failures).
- **Format auto-detection:** When downloading prediction files, attempts to parse as parquet, CSV (utf-8 only), JSON, and feather in sequence. **Pickle is intentionally excluded** (register C-59): `pd.read_pickle` runs `pickle.load`, which executes arbitrary code on deserialization — a remote-code-execution path on untrusted Appwrite bytes. If a file has a parquet magic header (`PAR1`) but fails to parse, the cascade is short-circuited immediately to prevent silent format misparse (C-52). Raises HTTP 500 with details of all attempted formats if none succeed.
- **Prediction staleness detection:** After a cache hit, calls `_check_staleness()` to compare the cached entry's timestamp against a configurable threshold (default 24 hours). Staleness is purely advisory — stale data is always served, and staleness check failures are caught and logged at DEBUG level (never block the response). Returns a `StalenessResult` dataclass with `is_stale`, `age_hours`, and `threshold_hours` fields.
- **Lifecycle methods:** Implements four abstract methods from `APIManager`: `_startup()` configures and launches uvicorn; `_shutdown()` clears the in-memory caches and **preserves** the durable on-disk dataset cache (C-66); `_health_check()` returns server and cache status; `_maintenance()` conditionally clears caches based on config flags.

---

## 4. Inputs and Assumptions

- **`model_path: APIPathManager`** (required at construction): Provides paths for `.env` file, cache directory, and configuration. Must be a valid `APIPathManager` instance with resolvable `dotenv`, `cache`, and config attributes.
- **`wandb_notifications: bool`** (optional, default `False`): Passed through to the parent `APIManager` constructor. Enables Weights & Biases alerting if True.
- **Environment variables** (loaded from `.env` at construction time):
  - `APPWRITE_ENDPOINT` -- Appwrite server URL.
  - `APPWRITE_DATASTORE_PROJECT_ID` -- Appwrite project ID.
  - `APPWRITE_CRAFD_BUCKET_ID` -- Storage bucket for prediction files.
  - `APPWRITE_CRAFD_BUCKET_NAME`, `APPWRITE_CRAFD_COLLECTION_ID`, `APPWRITE_CRAFD_COLLECTION_NAME`, `APPWRITE_METADATA_DATABASE_ID`, `APPWRITE_METADATA_DATABASE_NAME` -- Additional Appwrite configuration.
  - All 8 environment variables are validated at startup by `_validate_appwrite_env()` (module-level function at `api.py:37-55` -- `_REQUIRED_APPWRITE_ENV_VARS` at lines 37-46, `_validate_appwrite_env()` at lines 49-55). Missing variables cause an immediate `ValueError` listing all missing names. Called in `__init__()` after `load_dotenv()`.
- **API key per request:** Every endpoint requires an `X-API-Key` header containing a valid Appwrite API key. The key is validated on each request by attempting an Appwrite SDK operation.
- **Appwrite reachability:** The Appwrite server must be reachable at runtime. There is no offline fallback beyond cached data.
- **Parent class chain:** Inherits `CrafdApiManager` -> `APIManager` -> `ModelManager`. Path management uses a separate hierarchy (`APIPathManager` -> `ModelPathManager`) via composition: `ModelManager` stores a `ModelPathManager` instance as `self._model_path`. The parent `ModelManager` provides `self.configs` as a computed `@property` that merges deployment, hyperparameters, and meta configuration dicts on each access. Parent constructors also initialize `self._model_path` and `self._is_running`.

---

## 5. Outputs and Side Effects

- **HTTP responses:** All endpoints return JSON dicts. Data endpoints include `success`, `data` (containing `dataframe`, `shape`, `columns`, and request metadata). File endpoints return `StreamingResponse` or `FileResponse` for binary content.
- **FastAPI application object:** `self.app` is the configured FastAPI instance. A module-level `create_app()` factory function (`api.py:1302`) constructs the singleton lazily when called by uvicorn. Module-level state (`app` and `_fao_manager` at `api.py:1298-1299`) is initialized to `None` — no side effects occur at import time.
- **`from_config()` classmethod** (`api.py:266`): Test seam that constructs a `CrafdApiManager` from a pre-built config dict, bypassing filesystem config discovery and `.env` loading. Uses `_init_caches()` internally.
- **Side effects:**
  - **In-memory state:** Populates `_manager_cache`, `_dataframe_cache`, and `_file_cache` as requests arrive. Bounded by LRU eviction (`_manager_cache`, `_file_cache`) and TTL expiry (`_dataframe_cache`, 4-hour TTL). Explicit clearing available via DELETE `/cache` endpoint and lifespan shutdown hook.
  - **Disk state:** Delegated to `CrafdDiskCacheManager` (`managers/disk_cache.py`), which creates and manages the `{cache}/datasets/` directory. Writes a **value directory** (`_value/`, columnar arrow/npz — no pickle, C-149), a metadata sidecar (`_meta.json`), and a lock file (`.lock`) per partition. Partitions are labelled by a salted HMAC of the API-key-hash + category (ADR-031/#323).
  - **Remote reads:** Downloads files from Appwrite storage. The API is read-only with respect to Appwrite storage. The DELETE `/cache` endpoint only clears local caches.
  - **Signal handlers:** Registers SIGINT and SIGTERM handlers at construction time. These invoke `_shutdown()` and call `sys.exit(0)`.
  - **Shutdown preserves the durable cache (C-66):** `_shutdown()` clears only the ephemeral in-memory caches; the durable on-disk dataset cache (`CrafdDiskCacheManager`, 3.5-week TTL per ADR-011) **survives** a signal-triggered shutdown, so it is reused across restarts/redeploys instead of being cold-rebuilt. Purging the durable cache is a deliberate, opt-in operation: `_maintenance()` removes it only when `clear_cache` is set (and clears the in-memory caches only when `clear_manager_cache` is set).
  - **Logging:** Logs at INFO level for cache hits/misses, downloads, startup/shutdown. Logs at WARNING for disk cache failures and stale predictions. Logs at DEBUG for non-blocking staleness check failures. Logs at ERROR for unhandled exceptions.

---

## 6. Failure Modes and Loudness

- **HTTP 401 (Unauthorized):** Raised when `_validate_api_key()` fails. The API key is tested against Appwrite's `list_buckets(limit=1)`. Invalid keys, expired keys, or unreachable Appwrite all produce 401.
- **HTTP 404 (Not Found):** Raised when no prediction files match the requested category in the Appwrite bucket, or when a specific file ID does not exist.
- **HTTP 422 (Unprocessable Entity):** Raised by `parse_list_param()` and `parse_string_list_param()` when query parameters cannot be parsed as comma-separated integers or strings.
- **HTTP 500 (Internal Server Error):** Catch-all for unhandled exceptions within endpoint handlers. Includes file download failures, DataFrame parsing failures, `ForecastDataset` construction failures, and `PredictionStoreManager` creation failures.
- **HTTP 503 (Service Unavailable):** Raised by the `/health` endpoint when the Appwrite connection check fails.
- **`/health` forecast freshness (S3/#246, C-50):** `/health` returns HTTP 200 with `status: "degraded"` and a `forecast_freshness` block when the served forecast exceeds the freshness SLA (default 45 days, env `CRAFDAPI_FORECAST_FRESHNESS_SLA_DAYS`); the HTTP status stays about *service* health (503 iff Appwrite is unreachable). The verdict is also surfaced at `GET /provenance/forecast`.
- **`/health` + `/provenance` grace-fallback state (S4/#249, ADR-033 §6, D-24):** when a bounded last-good fallback is active (the newest manifested run was refused and `DatasetService` is serving the last-good run within the SLA), `/health` sets `status: "degraded"` and a `forecast_serving_state` block, and `GET /provenance/forecast` includes the same `serving_state` — even when the served forecast is itself fresh. Read from `DatasetService.forecast_serving_state()`; `None` (no forecast served yet) is omitted.
- **`/version` served-contract capability (S5/#250, ADR-033 §7, C-171):** `GET /version` (unauth) returns `served_contract_version` — the wire-contract dialect this build renders (`forecast/contract.SERVED_CONTRACT_VERSION`) — alongside `version` and `deployed_tag`, so deploy↔delivered capability skew is remotely diagnosable. A run whose manifest declares a version this build cannot render is `Refused` at ingest (see `DatasetService` CIC), never degraded-served.
- **Silent cache fallback:** Disk cache read failures (`json.JSONDecodeError`, `filelock.Timeout`, a torn/partial value dir) are caught, logged at WARNING, and silently fall through to remote download. A corrupted or version-mismatched cache entry does not cause a user-visible error, but the failure is not propagated either. Schema versioning (derived from the on-disk value format + the metadata sidecar field layout — **not** the class signature, C-138) mitigates version-mismatch risk. There is **no `pickle.load` on the read path** (C-149).
- **Advisory staleness warnings:** When a cached prediction exceeds the staleness threshold, a WARNING-level log is emitted but the data is still served. If the staleness check itself fails (e.g., timestamp missing), the failure is logged at DEBUG level and the response proceeds unaffected. Staleness is never a gate — it is observability-only.
- **Must never fail silently:** Authentication failures, missing prediction files, and all data retrieval errors must always produce an HTTP error response. A request must never return stale or empty data without indicating the failure.

---

## 7. Boundaries and Interactions

- **Layer:** Infrastructure (`src/views_crafdapi/managers/api.py`). This is the top-level orchestration class in the infrastructure layer.
- **Inheritance chain:** `CrafdApiManager` -> `APIManager` -> `ModelManager`. `ModelPathManager` is composed (not inherited) via `self._model_path`. The parent classes provide `self.configs` (computed property), `self._model_path`, `self._is_running`, and abstract lifecycle methods (`_startup`, `_shutdown`, `_health_check`, `_maintenance`).
- **Calls (downstream):**
  - `ForecastDataset` (Domain layer, `data/handlers/forecast_dataset.py`) -- constructs datasets from DataFrames, delegates `get_subset_dataframe()` and `calculate_hdi_map()`.
  - `CrafdDiskCacheManager` (Infrastructure, `managers/disk_cache.py`) -- owns disk-based dataset caching with file-locking, TTL, and auto-derived schema versioning. Composed as `self._disk_cache`.
  - `PredictionStoreManager` (Infrastructure, `managers/prediction/manager.py`) -- retrieves latest file IDs and downloads prediction files from Appwrite.
  - `AppWriteFileManager` (Infrastructure, `managers/appwrite/`) -- file listing, download, and cache operations for the file management endpoints.
  - `AppwriteConfig` (Infrastructure, `managers/appwrite/`) -- constructed per API key from environment variables.
- **Called by (upstream):**
  - FastAPI/uvicorn (external) -- invokes the registered endpoint handlers.
  - `create_app()` (module-level factory function) -- constructs the singleton instance.
- **Module-level helpers** (not methods, but co-located):
  - `parse_list_param(param)` -> `List[int]`: Parses comma-separated integer strings.
  - `parse_string_list_param(param)` -> `List[str]`: Parses comma-separated strings.
  - `convert_numpy_types(obj)` -> native Python types: Recursive conversion for JSON serialization.
  - `flatten_numeric_list_columns(df)` -> DataFrame: Flattens single-element arrays to scalars.
  - `dataframe_to_dict(df)` -> `List[Dict]`: Converts DataFrame to JSON-ready records with numpy type conversion.
- **Accepted topology deviation:** The parent class `APIManager.run()` imports `wandb_alert` from the `wandb/` observability layer via a lazy import inside the function body (`model.py:792-793`). This is permitted per ADR-012 Decision 4 — the import creates no load-time dependency and the module remains importable without wandb installed.
- **Must not depend on:** Test infrastructure, CLI entry points, or external services other than Appwrite.

---

## 8. Examples of Correct Usage

**Starting the API server (production entry point):**

```python
from views_crafdapi.managers.model import APIPathManager
from views_crafdapi.managers.api import CrafdApiManager

model_path = APIPathManager("un_crafd")
manager = CrafdApiManager(model_path=model_path)
manager._startup()
```

**Using the factory function with uvicorn (multi-worker mode):**

```bash
uvicorn views_crafdapi.managers.api:create_app --factory --host 0.0.0.0 --port 80 --workers 4
```

Each worker calls `create_app()` once at startup. No side effects occur at import time — construction is deferred to the factory call.

**Using `from_config()` for testing:**

```python
from views_crafdapi.managers.api import CrafdApiManager

config = {"deployment": {...}, "hyperparameters": {...}, "meta": {...}}
manager = CrafdApiManager.from_config(config, cache_dir=Path("/tmp/test_cache"))
```

**Calling an endpoint (client-side):**

```python
import requests

response = requests.get(
    "http://localhost:80/pg/data/forecast/subset",
    headers={"X-API-Key": "my-appwrite-api-key"},
    params={"time_ids": "500,501,502", "features": "main_mean", "with_metadata": True}
)
data = response.json()
```

---

## 9. Examples of Incorrect Usage

**Constructing multiple CrafdApiManager instances in the same process:**

```python
# WRONG: Each instance registers signal handlers, creates a new FastAPI app,
# and competes for the same disk cache directory. Use the module-level
# singleton via create_app() instead.
manager_a = CrafdApiManager(model_path=model_path)
manager_b = CrafdApiManager(model_path=model_path)
```

**Accessing internal caches directly to read or modify data:**

```python
# WRONG: Bypasses cache validation, TTL checks, and thread safety.
# Internal caches are implementation details, not a public interface.
manager._dataframe_cache["abc123"]["historical"]["data"] = my_dataframe
manager._file_cache["file_id_xyz"] = {"data": some_bytes, "timestamp": 0}
```

**Bypassing `from_config()` for test setup:**

```python
# WRONG: Manual attribute setup is fragile and will break when internal
# cache structure changes. Use from_config() instead.
manager = object.__new__(CrafdApiManager)
manager._manager_cache = {}
manager._dataframe_cache = {}
```

**Relying on in-memory cache persistence across worker processes:**

```python
# WRONG: Each uvicorn worker has its own CrafdApiManager instance with
# separate in-memory caches. Data cached in worker 1 is not visible to
# worker 2. Only the disk cache is shared across workers.
```

---

## 10. Test Alignment

- **Dedicated test files:**
  - `tests/test_api_endpoints.py` — endpoint routing, HTTP status codes, response format for all dynamic routes; `TestProvenanceEndpoint` covers `GET /provenance/{category}` (C-86).
  - `tests/test_api_utilities.py` — `parse_list_param()`, `parse_string_list_param()`, `convert_numpy_types()`, `dataframe_to_dict()` unit tests.
  - `tests/test_cache_bounds.py` — LRU/TTL eviction behavior, cache size limits.
  - `tests/test_disk_cache_versioning.py` — schema version embed and mismatch detection.
  - `tests/test_format_cascade.py` — format auto-detection pipeline including parquet magic-bytes guard (C-52).
  - `tests/test_cache_isolation.py` — per-API-key cache isolation, hash contract, eviction safety (C-58).
  - `tests/test_staleness_detection.py` — prediction staleness detection, advisory semantics, non-blocking guarantee (C-50).
  - `tests/test_integration_http.py` — ASGI integration tests via `httpx.AsyncClient`.
- **Integration coverage via downstream tests:** `tests/test_datastore_manager.py` exercises `PredictionStoreManager`, which is a key dependency.
- **Green tests (must pass):**
  - `parse_list_param()` correctly parses valid input and raises HTTP 422 for invalid input.
  - `parse_string_list_param()` correctly parses valid input and returns None for null/empty.
  - `convert_numpy_types()` recursively converts numpy scalars to Python natives.
  - `dataframe_to_dict()` produces JSON-serializable list-of-dicts output.
  - Bounded caches evict entries at capacity (LRU) and after TTL expiry.
  - Schema version mismatch triggers cache miss, not corrupted deserialization.
- **Beige tests (behavioral expectations):**
  - Three-tier cache hierarchy: in-memory hit skips disk and remote; disk hit skips remote; remote hit populates both disk and in-memory.
  - Disk cache TTL expiry triggers re-download on next request.
  - Disk cache round-trip preserves ForecastDataset state.
  - Lifespan shutdown hook clears all three caches.
- **Red tests (regression guards):**
  - A corrupted cache entry does not crash the service (fallback to remote download).
  - Concurrent requests with different API keys do not cross-contaminate cached data.

---

## 11. Evolution Notes

- **Stable:** The FastAPI app structure, endpoint URL patterns, and the three-tier cache hierarchy are the core architectural commitment. Changing endpoint URLs is a breaking API change for FAO consumers.
- **Stable:** The per-API-key isolation model (cache keyed by API key hash) is fundamental to the multi-tenant design.
- **Stable:** Bounded in-memory caches via `cachetools` (`LRUCache`, `TTLCache`). Cache sizes and TTLs are tunable but the bounded-cache design is settled.
- **Stable:** Lazy `create_app()` factory pattern with `from_config()` test seam.
- **Stable:** Fail-fast environment variable validation at startup via `_validate_appwrite_env()`.
- **Resolved:** C-02 (module-level init), C-03 (env validation), C-05 (cache schema versioning), C-07 (unbounded caches), C-08 (dead code) — all resolved as of 2026-06-01.
- **Candidate for change (D-03):** Further decomposition per D-03 recommendation — extract `DatasetService` or route registration logic from the remaining ~1,300 LOC. See also C-37 (`appwrite.py` mixed concerns).
- **Would require contract revision:** Adding WebSocket support, switching to a different HTTP framework, changing the authentication model (e.g., JWT tokens instead of Appwrite API keys), or splitting into separate microservices.

---

## End of Contract

This document defines the **intended meaning** of `CrafdApiManager`.

Changes to behavior that violate this intent are bugs.  
Changes to intent must update this contract.
