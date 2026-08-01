# ADR-011: Caching Strategy and Eviction Policy

**Status:** Implemented  
**Date:** 2026-05-27  
**Implemented:** 2026-05-28  
**Deciders:** Project maintainers  

**Addresses risk register:** C-05 (pickle without version checks), C-07 (unbounded in-memory caches), C-09 (unused cachetools dependency)

> **Superseded in part (2026-06, S5/#154 → ADR-030, register C-149/C-138):** the disk tier no
> longer pickles. It persists the dataset's **columnar VALUE** (arrow/npz frame arrays + geo table
> + index + manifest, via `ForecastDataset.to_value`/`from_value`) — there is **no `pickle.load`
> on the read path** (C-149) — and the cache schema version is derived from the value format + meta
> layout, **decoupled from the class signature** (C-138). Partition directories are labelled by a
> salted HMAC of the API-key-hash (ADR-031/#323), not the raw hash. The bounded-cache / TTL /
> eviction / schema-version *decisions* below stand; every reference to *pickle serialization* is
> **historical** — do not rebuild a pickled cache from this document (register C-160).

---

## Context

**Implementation Status (2026-05-28):** Bounded caches adopted -- `LRUCache` and `TTLCache` from `cachetools` replace plain dicts (`api.py:292-295` in `_init_caches()`). Disk cache extracted to `managers/disk_cache.py` (`FAODiskCacheManager` class). Schema versioning implemented via `_derive_cache_schema_version()` at `disk_cache.py:25-35`, with `CACHE_SCHEMA_VERSION` at `disk_cache.py:38`. C-05, C-07, C-09 resolved.

The system maintains a three-tier cache architecture with escalating persistence:

1. **In-memory dicts** (`_manager_cache`, `_dataframe_cache`, `_file_cache` -- formerly at `managers/api.py:190-196`, now bounded caches at `api.py:292-295`) --- plain Python dicts keyed by API key hash and category. None had eviction policies, size limits, or LRU behavior. They grew monotonically with distinct API keys and file IDs, cleared only on explicit `DELETE /cache` or server shutdown.

2. **Disk pickle with FileLock** (formerly inline in `api.py`, now extracted to `managers/disk_cache.py` as `FAODiskCacheManager`) --- entire `FAO_PGMDataset` objects serialized via `pickle.dump()` with `HIGHEST_PROTOCOL`. A `.json` sidecar stores `file_id`, `timestamp`, `rows`, `columns`, `ttl_days`, and now `schema_version` (`disk_cache.py:145`). TTL is 3.5 weeks (`disk_cache.py:40`), reasonable for monthly forecast data.

3. **Remote Appwrite** --- the authoritative source of prediction files.

Three concrete problems exist:

- **Unbounded memory growth (C-07).** In long-running production deployments with many distinct API keys, `_file_cache` (raw file bytes) and `_dataframe_cache` (full DataFrames plus `FAO_PGMDataset` objects) grow without bound. There is no mechanism to shed entries under memory pressure.

- **Pickle without version compatibility (C-05).** If `FAO_PGMDataset` class attributes, inheritance chain, or `_ViewsDataset` fields change, existing pickle files become incompatible. The broad `except` clause (formerly at `api.py:306`) caught `UnpicklingError` and treated it as a cache miss, causing silent re-downloads that masked deployment issues where old caches should be explicitly invalidated.

- **Unused cachetools dependency (C-09).** `cachetools==6.2.1` is declared in `pyproject.toml:24` but never imported anywhere in the source. It appears to be a remnant of an earlier caching approach that was replaced by the current dict-based caches.

---

## Decision

Adopt bounded, TTL-aware, versioned caching across all three tiers:

1. **In-memory caches must have max-size limits and LRU eviction.** `_file_cache` (raw bytes, largest objects) gets the lowest limit. `_manager_cache` (lightweight SDK clients) gets the highest.

2. **Disk cache must embed a `schema_version` in the JSON sidecar.** On version mismatch, explicitly invalidate (delete pickle + metadata files and log at WARNING) rather than silently falling through the broad `except` clause.

3. **In-memory caches should have a configurable TTL** (default: 4 hours for `_dataframe_cache`, no TTL for `_manager_cache` since client objects are stateless).

4. **The existing `cachetools` dependency should be adopted** for in-memory cache management, resolving C-09 naturally.

---

## Rationale

- Unbounded caches in a long-running ASGI server are a well-known production failure mode. The FAO API serves multiple users with distinct API keys; each key creates separate cache entries across all three dicts.
- Silent pickle deserialization failures mask the difference between "cache expired normally" and "deployment changed class definitions." These two cases require different operational responses.
- `cachetools` is already in the dependency tree (`pyproject.toml:24`). Using it eliminates manual eviction logic and resolves the unused-dependency concern simultaneously.
- The 3.5-week disk TTL is appropriate for monthly forecast data and does not need to change. The new schema version mechanism is orthogonal to TTL.

---

## Considered Alternatives

### Alternative A: `cachetools.LRUCache` / `cachetools.TTLCache`

- **Pros:** Already declared as a dependency. Drop-in replacement for plain dicts. Configurable `maxsize` and `ttl`.
- **Cons:** None significant --- this is the chosen approach.
- **Reason for rejection:** Not rejected; this is the selected implementation.

### Alternative B: Redis or Memcached

- **Pros:** Shared cache across workers; built-in eviction; production-grade.
- **Cons:** Adds operational complexity (separate process, network dependency). Overkill for a single-server deployment with a small user base.
- **Reason for rejection:** Disproportionate infrastructure cost for current deployment model. May be revisited if the API scales to multiple nodes.

### Alternative C: Manual `OrderedDict` + max-size enforcement

- **Pros:** No new imports; full control over eviction behavior.
- **Cons:** Reinvents the wheel when `cachetools` is already available. More code to maintain. No built-in TTL support.
- **Reason for rejection:** Unnecessary complexity when a suitable library is already declared.

---

## Consequences

### Positive

- Memory usage becomes bounded and predictable under sustained load (resolves C-07)
- Class definition changes produce explicit WARNING log entries instead of silent fallthrough (resolves C-05)
- The `cachetools` dependency becomes justified by actual usage (resolves C-09)
- Disk cache invalidation becomes a deliberate, logged operation

### Negative

- LRU eviction means some requests will experience cache misses that would not have occurred with unbounded caches. This is acceptable: a cache miss triggers a re-download from Appwrite, which is the correct behavior.
- Adding `schema_version` to the sidecar requires a one-time migration: existing cache files without a version field should be treated as version 0 and invalidated on first read.
- Max-size tuning requires production observation. Initial values are estimates.

---

## Implementation Notes

1. **Cache schema version** implemented via auto-derived `_derive_cache_schema_version()` at `disk_cache.py:25-35`, with `CACHE_SCHEMA_VERSION` constant at `disk_cache.py:38`. The version is based on a hash of the `FAO_PGMDataset.__init__` signature and sidecar meta fields, so class changes automatically invalidate old caches.

2. **Disk cache extracted** to `managers/disk_cache.py` as `FAODiskCacheManager` (class at `disk_cache.py:43`). The `write()` method includes `schema_version` in the JSON sidecar (`disk_cache.py:145`). The `read()` method checks `schema_version` on load (`disk_cache.py:92`) and invalidates on mismatch with WARNING log (`disk_cache.py:97`).

3. **Plain dict caches replaced** with `cachetools` equivalents at `api.py:292-295` inside `_init_caches()`:
   ```python
   self._manager_cache: LRUCache = LRUCache(maxsize=100)
   self._dataframe_cache: TTLCache = TTLCache(maxsize=50, ttl=4 * 3600)
   self._file_cache: LRUCache = LRUCache(maxsize=20)
   ```

4. **`from cachetools import LRUCache, TTLCache`** added at `api.py:25`, resolving C-09.

---

## Validation & Monitoring

- **Memory bounds:** Log cache sizes (len) at INFO level on each eviction event. Monitor for unexpected eviction rates that indicate max-size is too low.
- **Schema version misses:** Log at WARNING when a schema version mismatch triggers invalidation. A burst of these after deployment confirms the mechanism is working. Sustained warnings indicate a problem.
- **Cache hit rates:** Disk cache hit/miss logging is now handled by `FAODiskCacheManager` in `managers/disk_cache.py`. In-memory hit/miss logging can supplement these for performance tuning.
- **Failure signal:** If eviction rates exceed 50% of requests over a 1-hour window, the max-size limits need upward adjustment.

---

## Open Questions

- What are realistic upper bounds for concurrent distinct API keys in production? The proposed `_manager_cache` limit of 100 is an estimate.
- Should the disk cache schema version be tied to a specific `FAO_PGMDataset` class attribute (e.g., `__version__`) or remain a manually incremented constant?
- Should cache statistics (hit rate, eviction count, memory estimate) be exposed via a `/cache/stats` endpoint for operational monitoring?

---

## References

- C-05, C-07, C-09 in the technical risk register (`reports/technical_risk_register.md`)
- ADR-008 (Observability and Explicit Failure) --- logging requirements for cache invalidation events
- ADR-009 (Boundary Contracts and Configuration Validation) --- cache tier boundary contracts
- `managers/api.py:292-295` (bounded cache declarations in `_init_caches()`), `managers/disk_cache.py` (extracted `FAODiskCacheManager`), `pyproject.toml` (cachetools dependency)
