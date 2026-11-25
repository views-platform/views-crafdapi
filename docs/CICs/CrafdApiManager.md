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

- **API lifecycle management:** Constructs a fully configured FastAPI application with all routes registered during `__init__`. Provides lifespan hooks that start the loader at start-up, and at shutdown stop it and clear the in-memory caches (kept instead when the load does not stop within `_STOP_WAIT_SECONDS`, since its thread may still write them) (the durable on-disk cache persists, C-66). Installs no signal handlers of its own (#236): uvicorn handles SIGINT and SIGTERM and runs the lifespan shutdown. A stop normally takes at most 90 s: up to 30 s for open requests (`--timeout-graceful-shutdown 30`), then up to 60 s waiting for a running load; on SIGTERM the process then ends at once (uvicorn re-raises the signal), and a load still running dies in the middle of its step. (On SIGINT, Ctrl-C by hand, Python instead waits for the load's thread.) These limits are the unit's; `_startup()` (not used in production) sets no graceful limit. A SIGTERM that arrives while the service is still starting, or a second Ctrl-C (SIGINT) during a stop (uvicorn then forces the exit; a second SIGTERM does not), skips the lifespan shutdown; nothing is lost, since the in-memory caches end with the process and files are written under temporary names. The unit allows 120 s (`TimeoutStopSec=120`). This is not a hard bound: a request that blocks the event loop (a bulk file built on request, about 40 s for a grid file, or a large slice) stops both timers while it runs, and a stop that passes 120 s is ended by systemd with SIGKILL (files are written under temporary names, so nothing is left half-written). During the first 30 s the loader keeps checking; a load it starts then is stopped like any other.
- **Three-tier cache:** Every data request is served through a three-tier cache hierarchy:
  1. In-memory bounded caches (fastest, per-worker process, keyed by API key hash + category). `_manager_cache`: `LRUCache(100)`, `_dataframe_cache`: `TTLCache(50, ttl=4h)`, `_file_cache`: `LRUCache(20)`.
  2. Disk **value store** via `CrafdDiskCacheManager` (`managers/disk_cache.py`) — the dataset's columnar value (arrow/npz frame arrays + geo table + index + manifest, via `ForecastDataset.to_value`/`from_value`; **no pickle**, C-149), cross-worker, `FileLock`-serialized, TTL-validated at 3.5 weeks. The schema version is derived from the value format + meta layout, **decoupled from the class signature** (C-138). Partition directories are labelled by a salted HMAC of the API-key-hash (ADR-031/#323), not the raw key hash.
  3. Remote Appwrite download via `PredictionStoreManager` (slowest, authoritative source).
  Cache hits at any tier populate all higher tiers.
- **Loading (ADR-044):** The forecast and the recorded deaths are loaded by `ForecastLoader` (`managers/forecast_loader.py`) with the server's own read key (`APPWRITE_CRAFDAPI_SERVER_API_KEY`), at start and whenever a check every five minutes finds a new delivery. The load runs in a worker thread; it builds the nine bulk files, then hands the routes freshly read datasets in one assignment. Routes answer only from that loaded data (`_served()`), and with `503` before the first load. A failed load leaves the served data in place.
- **Partner keys (ADR-044):** A partner key only decides who gets in: `_check_partner_key` checks it with the store (`_validate_api_key`) and remembers a valid key for 60 seconds. No route reads data with a partner key. All partners are served the same loaded copy; the per-key cache tiers below are now used under the server key only.
- **Endpoint routing:** Registers exactly the routes `tests/test_api_endpoints.py::TestTheRouteList` lists: `/ping`, `/version`, `/health`, one bulk route, `/bulk/{level}/{outcome}`, and one slice route, `/slice/{level}/{outcome}`, for the specification's three levels (`grid`, `admin1`, `country`) and every outcome. The slice takes exactly the specification's options (places, months, columns; the outcome is in the path), returns summary columns and the recorded deaths, and never samples (#204, #206). The subset and HDI-MAP routes it replaced are gone. There is no endpoint index at `/`, no `/provenance`, and no `/files` route (removed for the service specification, #207). The levels admin 0 (`gaul0`) and admin 2 (`gaul2`) were removed (#205). The bulk route serves one Parquet file per level and outcome, the table a slice with no options returns (`forecast/serialize/bulk_file.py`), with the header `X-Forecast-Made-In`; a slice's JSON carries `forecast_made_in`; it is built on the first request and kept on disk under the store's file ids of the forecast and history it came from, shared by every key, until a new delivery replaces it (scratch and one-request files older than `STALE_SECONDS`, an hour, are swept at the next hand-over of a delivery: when a new one is served, or at the hourly retry of an incomplete one) (`managers/bulk_files.py`, #203; the decision and its reasons: ADR-043). A dataset loaded without a store id gets a file built for that request only. Like the slice, it answers 503 without the history. The loader builds the files before a delivery is served; a route builds a file only if that failed, and then on the event loop (register C-285).
- **Provenance/lineage (C-86 / ADR-023):** When an artifact is brought into service, the composed `DatasetService.get_latest_dataset` (reached through `_get_latest_dataset`) resolves and logs a `PredictionProvenance` record (file ID, declared upstream `source`/`pipeline`, hash, timestamp) and stores it on the dataframe cache. `/health` reads the served run's creation time from it for the freshness verdict. No route returns the record itself (`/provenance` was removed, #207).
- **Value/metadata plausibility (C-72):** Before caching/serving, `DatasetService.get_latest_dataset` calls `dataset.validate_value_plausibility()` (prediction samples finite + non-negative) and `dataset.validate_metadata_plausibility()` (coordinate ranges, ISO3 shape, non-negative GAUL codes). A violation is surfaced as HTTP 500 — implausible data fails loud rather than reaching FAO. The disk-cache read re-runs both checks (C-277, #168), so a slot whose bytes no longer pass is never served: it is rebuilt from source, or answered with 503 on the last-good fallback.
- **Error translation:** All exceptions within endpoint handlers are caught and translated to appropriate HTTP status codes (401 when the store refuses the key, 404 for an unknown level or outcome, 413 for a slice too large to send, 422 for invalid query parameters, 500 for unhandled errors, 503 when nothing is loaded, the recorded deaths are missing, or the store cannot be asked about a key — see §6). Most data routes still echo the exception text in a 500 (`detail=str(e)`, the C-264 remainder).
- **Format auto-detection:** When downloading prediction files, attempts to parse as parquet, CSV (utf-8 only), JSON, and feather in sequence. **Pickle is intentionally excluded** (register C-59): `pd.read_pickle` runs `pickle.load`, which executes arbitrary code on deserialization — a remote-code-execution path on untrusted Appwrite bytes. If a file has a parquet magic header (`PAR1`) but fails to parse, the cascade is short-circuited immediately to prevent silent format misparse (C-52). Raises HTTP 500 with details of all attempted formats if none succeed.
- **Prediction staleness detection:** After a cache hit, calls `_check_staleness()` to compare the cached entry's timestamp against a configurable threshold (default 24 hours). Staleness is purely advisory — stale data is always served, and staleness check failures are caught and logged at DEBUG level (never block the response). Returns a `StalenessResult` dataclass with `is_stale`, `age_hours`, and `threshold_hours` fields.
- **Lifecycle methods:** Implements four abstract methods from `APIManager`: `_startup()` configures and launches uvicorn; `_shutdown()` clears the in-memory caches and **preserves** the durable on-disk dataset cache (C-66), but nothing in production calls it since #236 (only tests do); `_health_check()` returns server and cache status; `_maintenance()` conditionally clears caches based on config flags.

---

## 4. Inputs and Assumptions

- **`model_path: APIPathManager`** (required at construction): Provides paths for `.env` file, cache directory, and configuration. Must be a valid `APIPathManager` instance with resolvable `dotenv`, `cache`, and config attributes.
- **`wandb_notifications: bool`** (optional, default `False`): Passed through to the parent `APIManager` constructor. Enables Weights & Biases alerting if True.
- **Environment variables** (loaded from `.env` at construction time):
  - `APPWRITE_ENDPOINT` -- Appwrite server URL.
  - `APPWRITE_CRAFD_PROJECT_ID` -- CRAF'd's own Appwrite project (þing 04 D3), not the shared project that holds the shelf and FAO's data.
  - `APPWRITE_CRAFD_BUCKET_ID` -- Storage bucket for prediction files.
  - `APPWRITE_CRAFD_BUCKET_NAME`, `APPWRITE_CRAFD_COLLECTION_ID`, `APPWRITE_CRAFD_COLLECTION_NAME`, `APPWRITE_METADATA_DATABASE_ID`, `APPWRITE_METADATA_DATABASE_NAME` -- Additional Appwrite configuration.
  - All 8 environment variables are validated at startup by `_validate_appwrite_env()` (module-level `_REQUIRED_APPWRITE_ENV_VARS` and `_validate_appwrite_env()` in `api.py`). Missing variables cause an immediate `ValueError` listing all missing names. Called in `__init__()` after `load_dotenv()`. When `APPWRITE_REGISTRY` points at a readable registry file, startup also raises `ValueError` if any required name has no value in the registry or any set coordinate differs from it, listing every problem in one message; the server's service environment does not set `APPWRITE_REGISTRY`, so there `bootstrap.sh part2` performs the required-name check before installing the env file.
- **API key per request (ADR-044):** Every data endpoint requires an `X-API-Key` header holding a partner key. `_check_partner_key` checks it with the store (`_validate_api_key`, `list_buckets(limit=1)`) and remembers a valid key for 60 seconds, so a key deleted in Appwrite is refused within a minute. The key only decides who gets in: no data route reads the store with it. `/health` still asks the store with the caller's key.
- **The server's own key (ADR-044):** `APPWRITE_CRAFDAPI_SERVER_API_KEY` (its own environment file on the server). Without it the service starts, loads nothing and answers 503 to every data request.
- **Appwrite reachability:** The Appwrite server must be reachable at runtime. There is no offline fallback beyond cached data.
- **Parent class chain:** Inherits `CrafdApiManager` -> `APIManager` -> `ModelManager`. Path management uses a separate hierarchy (`APIPathManager` -> `ModelPathManager`) via composition: `ModelManager` stores a `ModelPathManager` instance as `self._model_path`. The parent `ModelManager` provides `self.configs` as a computed `@property` that merges deployment, hyperparameters, and meta configuration dicts on each access. Parent constructors also initialize `self._model_path` and `self._is_running`.

---

## 5. Outputs and Side Effects

- **HTTP responses:** All endpoints return JSON dicts. Data endpoints include `success`, `data` (containing `dataframe`, `shape`, `columns`, and request metadata). File endpoints return `StreamingResponse` or `FileResponse` for binary content.
- **FastAPI application object:** `self.app` is the configured FastAPI instance. A module-level `create_app()` factory function in `api.py` constructs the singleton lazily when called by uvicorn. Module-level state (`app` and `_fao_manager` in `api.py`) is initialized to `None` — no side effects occur at import time.
- **`from_config()` classmethod** (`CrafdApiManager.from_config`): Test seam that constructs a `CrafdApiManager` from a pre-built config dict, bypassing filesystem config discovery and `.env` loading. Uses `_init_caches()` internally.
- **Side effects:**
  - **In-memory state:** The loader populates `_dataframe_cache` and `_file_cache` under the server key when it loads a delivery (ADR-044); `_manager_cache` and the 60-second valid-key cache fill as partner requests arrive. Bounded by LRU eviction (`_manager_cache`, `_file_cache`) and TTL expiry (`_dataframe_cache`, 4-hour TTL). Cleared by the lifespan shutdown hook, unless the load does not stop within `_STOP_WAIT_SECONDS` (then kept for its thread); there is no clearing endpoint (`DELETE /cache` was removed 2026-11-11, C-269).
  - **Disk state:** Delegated to `CrafdDiskCacheManager` (`managers/disk_cache.py`), which creates and manages the `{cache}/datasets/` directory. Writes a **value directory** (`_value/`, columnar arrow/npz — no pickle, C-149), a metadata sidecar (`_meta.json`), and a lock file (`.lock`) per partition. Partitions are labelled by a salted HMAC of the API-key-hash + category (ADR-031/#323).
  - **Remote reads:** Downloads files from Appwrite storage. The API is read-only with respect to Appwrite storage. No endpoint reads the store's files or the shared file cache directly (`/files/*`, `/cache/stats` and `DELETE /cache` are removed: C-269, #207).
  - **Signal handlers:** none of its own since #236. Before, handlers registered at construction replaced uvicorn's, called `_shutdown()` and `sys.exit(0)`, and so skipped the lifespan shutdown (checked with a real SIGTERM, 2026-11-22). `_shutdown()` is no longer called in production (only by tests); the lifespan shutdown does the work. Nothing sets `_is_running` in production (#238).
  - **Shutdown preserves the durable cache (C-66):** `_shutdown()` and the lifespan shutdown clear only the ephemeral in-memory caches; the durable on-disk dataset cache (`CrafdDiskCacheManager`, 3.5-week TTL per ADR-011) **survives** a signal-triggered shutdown, so it is reused across restarts/redeploys instead of being cold-rebuilt. Purging the durable cache is a deliberate, opt-in operation: `_maintenance()` removes it only when `clear_cache` is set (and clears the in-memory caches only when `clear_manager_cache` is set).
  - **Logging:** Logs at INFO level for cache hits/misses, downloads, startup/shutdown. Logs at WARNING for disk cache failures and stale predictions. Logs at DEBUG for non-blocking staleness check failures. Logs at ERROR for unhandled exceptions.

---

## 6. Failure Modes and Loudness

- **HTTP 401 (Unauthorized):** Raised only when the store refuses the key (Appwrite 401/403): by `_validate_api_key()` (which tests the key with `list_buckets(limit=1)`, on a partner's first request and again after 60 seconds), and by `/health`. Since ADR-044 data routes make no store calls with the partner's key. Nothing is served from cache or the last-good fallback to a refused key (C-268, #159; ADR-033 §6a). The detail is generic (`"API key not authorized"`); the store's wording goes to the log (C-264).
- **HTTP 404 (Not Found):** an unknown level or outcome code (`_refuse_unknown_level_or_outcome`), an outcome the loaded forecast does not hold (`_refuse_outcome_not_forecast`), and any path that is not a route. Since ADR-044 no route looks files up in the store, so a missing delivery is not a 404: it is the 503 below.
- **HTTP 422 (Unprocessable Entity):** Raised by the slice when an option is badly written (a place id that is not a whole number, a month not written `2027-01`, an empty option, an option given twice) or names a place, month or column that is not served. The detail says which.
- **HTTP 413 (Content Too Large):** a slice whose answer or whose work would exceed the response budget (`CRAFDAPI_MAX_RESPONSE_BYTES`, default 512 MiB; C-284). The message says to ask for fewer places or months, or names the bulk file of the same level and outcome.
- **HTTP 500 (Internal Server Error):** a server configuration error found while checking a key (missing APPWRITE_* settings, fixed text), the `PredictionStoreManager` used by `/health` failing to build, and the catch-all on the slice and bulk routes for an unexpected error while building an answer (it still echoes the exception text, the C-264 remainder). Loading errors never reach a request: the loader logs them and keeps serving what it has (ADR-044).
- **A country whose cells carry two different ISO3 codes (#225):** every level serves it with the code most of its cells carry (empty on a tie), logged once at WARNING; other countries are unaffected. Cells without a GAUL country code keep their own ISO3 at grid level; a region or place made of them takes the first ISO3 its cells carry. (A region mixing cells with and without a country code still has two country codes and is refused as a broken geography; that predates #225.) A place id with two different *names* is still refused (500 at that level).
- **HTTP 503, nothing loaded yet (ADR-044):** every data request answers "No forecast is available right now. Try again later." until the loader's first complete load after a start (or forever, without the server key), except two kinds checked before it: an unknown level or outcome code (404) and a badly written option (422). Every other check (an outcome the forecast lacks, an unknown column, place or month) needs the loaded data, so before the first load those requests also get the 503. Slices of past months and bulk files also answer 503 while the recorded deaths are not loaded.
- **`/health` loading fields (ADR-044):** `forecast_loaded` (false until the first load; status `degraded`), `history_loaded` (false while the recorded deaths are not loaded, and false when nothing is loaded at all; status `degraded`), and `forecast_loading` with status `degraded` while the newest delivery cannot be loaded and the previous one is served.
- **HTTP 503 (Service Unavailable):** "Metadata store unavailable" when the store could not be asked while checking a partner key (no response, timeout, rate limit, 5xx, unexpected exception; not a 401/403); "The recorded deaths are unavailable right now" when a bulk file or a slice reaching past months needs history that is not loaded, or that holds no column for the outcome; and "Service unhealthy" when `/health` itself fails. The 503 before the first load is described above.
- **`/health` forecast freshness (S3/#246, C-50):** `/health` returns HTTP 200 with `status: "degraded"` and a `forecast_freshness` block when the served forecast exceeds the freshness SLA (default 45 days, env `CRAFDAPI_FORECAST_FRESHNESS_SLA_DAYS`); the HTTP status stays about *service* health: 401 when the store refuses the key, 503 only when the check itself raises. A store that answers `list_buckets` with a non-auth failure, or cannot be reached while the key's check is remembered, is reported as `appwrite_connected: false` in a 200 response and makes `status` `"degraded"`; so does a forecast whose age cannot be measured (`is_stale` null, or the freshness lookup failing) (#248: the Better Stack monitor reads only `status`). When the key's check is not remembered (just after a restart), an unreachable store answers 503 `Metadata store unavailable` instead.
- **`/health` grace-fallback state (S4/#249, ADR-033 §6, D-24):** when a bounded last-good fallback is active (the newest manifested run was refused and `DatasetService` is serving the last-good run within the SLA), `/health` sets `status: "degraded"` and a `forecast_serving_state` block — even when the served forecast is itself fresh. Taken from the loaded snapshot (`Served.serving_state`, recorded when the loader handed the data over, ADR-044), so it describes what partners are answered from; omitted before the first load.
- **`/health` names the served forecast run (#262):** `served_run`, the loaded delivery's `run_id` (from `Served.provenance`), so a check from outside (the smoke test's `--expect-run`; the notebooks, #264) can confirm which run partners get. Omitted while nothing is loaded, or when the loaded run's record carries no `run_id`.
- **`/health` names the build that wrote the served forecast (C-282, #170):** a `served_entry` block `{written_by, written_at (ISO-8601 UTC), source_kind}` from `DatasetService.served_forecast_entry()`. After a deploy the disk slot can still be the previous build's, so this answers "is the fix in effect?" without reading files on the box. Reported, not judged: an older `written_by` never changes `status`; `null` means a build from before #170 wrote the slot (adding the field did not invalidate existing slots). Omitted before the first forecast serve, and from the moment a request has to go to the store until it serves one, so a 503, 401 or 500 leaves none. Taken from the loaded snapshot (`Served.entry`, ADR-044): one loaded copy for every key.
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
    "http://localhost:80/slice/admin1/sb",
    headers={"X-API-Key": "my-api-key"},
    params={"places": "1301", "months": "2027-01,2027-02", "columns": "sb_map,sb_hdi90_upper"},
)
data = response.json()
```

---

## 9. Examples of Incorrect Usage

**Constructing multiple CrafdApiManager instances in the same process:**

```python
# WRONG: Each instance creates a new FastAPI app and its own loader,
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
  - `tests/test_api_endpoints.py` — endpoint routing, HTTP status codes, response format for all dynamic routes; `TestTheRouteList` pins the exact route list (#207).
  - `tests/test_api_utilities.py` — `convert_numpy_types()`, `dataframe_to_dict()` unit tests.
  - `tests/test_cache_bounds.py` — LRU/TTL eviction behavior, cache size limits.
  - `tests/test_disk_cache_versioning.py` — schema version embed and mismatch detection.
  - `tests/test_format_cascade.py` — format auto-detection pipeline including parquet magic-bytes guard (C-52).
  - `tests/test_cache_isolation.py` — per-API-key cache isolation, hash contract, eviction safety (C-58).
  - `tests/test_staleness_detection.py` — prediction staleness detection, advisory semantics, non-blocking guarantee (C-50).
  - `tests/test_integration_http.py` — ASGI integration tests via `httpx.AsyncClient`.
- **Integration coverage via downstream tests:** `tests/test_datastore_manager.py` exercises `PredictionStoreManager`, which is a key dependency.
- **Green tests (must pass):**
  - The slice refuses a badly written or unknown option with HTTP 422 (`tests/test_api_endpoints.py::TestSliceRoute`).
  - `convert_numpy_types()` recursively converts numpy scalars to Python natives.
  - `dataframe_to_dict()` produces JSON-serializable list-of-dicts output.
  - Bounded caches evict entries at capacity (LRU) and after TTL expiry.
  - Schema version mismatch triggers cache miss, not corrupted deserialization.
- **Beige tests (behavioral expectations):**
  - Three-tier cache hierarchy: in-memory hit skips disk and remote; disk hit skips remote; remote hit populates both disk and in-memory.
  - Disk cache TTL expiry triggers re-download on next request.
  - Disk cache round-trip preserves ForecastDataset state.
  - Lifespan shutdown hook clears all three caches, once the loader has stopped (kept if it does not stop in time).
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
