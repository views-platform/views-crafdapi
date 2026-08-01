# ADR-016: Concurrency Safety for Shared Stateful Objects

**Status:** Accepted  
**Date:** 2026-05-27  
**Deciders:** Project maintainers  

---

## Context

C-01 was a Tier 1 silent data corruption risk where a shared `PosteriorDistributionAnalyzer`
instance was mutated concurrently by async request handlers. It was resolved by per-call
instantiation in `_ViewsDataset`. However, the same pattern — shared mutable state accessed
from async handlers without synchronization — exists in the three in-memory caches in
`FAOApiManager` (`_manager_cache`, `_dataframe_cache`, `_file_cache` at `managers/api.py:190-196`).

These caches use **check-then-set patterns** that are not atomic across `await` points:

- **`_manager_cache`** at `api.py:483-491`: two concurrent requests with the same API key can
  both pass `if key not in dict` and both create separate manager instances.
- **`_dataframe_cache`** at `api.py:546-551` and `api.py:593-617`: two concurrent requests can
  both trigger duplicate Appwrite downloads for the same `(key_hash, category)`.
- **`_file_cache`**: same pattern for raw file bytes.

In CPython, individual dict operations are GIL-protected, but multi-statement check-then-set
sequences are **not atomic across `await` points**. When a coroutine awaits an Appwrite call,
the event loop can switch to another coroutine executing the same check-then-set. The current
consequences are duplicate resource creation (wasteful, not corrupt), but the pattern would
become dangerous if cached objects were mutated after insertion.

---

## Decision

1. **Shared objects must be classified as:** **immutable** (safe by construction), **per-request**
   (created fresh per handler), or **synchronized** (protected by `asyncio.Lock` or equivalent).

2. **Check-then-set on shared dicts** must use `dict.setdefault()` for simple synchronous
   initialization, or `asyncio.Lock` for initialization sequences involving `await`.

3. **New stateful classes must declare their concurrency model** in their CIC (per ADR-006).

4. **Code review of async handlers** must explicitly verify shared mutable state access.

---

## Rationale

Caches cannot be per-request (their purpose is cross-request persistence), so the C-01 fix
pattern does not apply directly. The three-category taxonomy (immutable / per-request /
synchronized) provides a complete model: `setdefault()` handles simple synchronous cases
where duplicate creation is acceptable, while `asyncio.Lock` is necessary when initialization
involves network I/O that should not be duplicated.

---

## Considered Alternatives

### Alternative A: No caching (per-request everything)
- **Reason for rejection:** Every request would re-download from Appwrite. Unacceptable latency.

### Alternative B: Global `asyncio.Lock` on every cache access
- **Reason for rejection:** Too coarse — serializes all data access even for different keys.
  Per-key locks are correct but should target only patterns involving `await`.

### Alternative C: Immutable cache entries (frozen dataclasses)
- **Reason for rejection:** Impractical for large DataFrames and `FAO_PGMDataset` objects.

### Alternative D: Thread-safe dicts (e.g., `concurrent.futures`)
- **Reason for rejection:** Wrong concurrency model. FastAPI uses asyncio; thread-safe
  primitives can block the event loop.

---

## Consequences

### Positive
- Clear mental model for shared state in async handlers
- Prevents future C-01-class bugs via explicit classification
- Eliminates duplicate Appwrite downloads on concurrent cache misses
- Integrates with CIC framework (ADR-006) for explicit concurrency contracts

### Negative
- Adds `asyncio.Lock` complexity to cache initialization paths
- Per-key lock dicts are themselves shared state requiring careful management
- GIL-atomicity assumptions break under free-threaded Python (PEP 703)

---

## Implementation Notes

1. **`_manager_cache` (`api.py:483-491`):** Replace check-then-set with `dict.setdefault()`.
   Manager constructors are synchronous — duplicate creation is harmless, `setdefault()`
   ensures only one is stored.

2. **`_dataframe_cache` (`api.py:546-551`, `593-617`):** Add per-key `asyncio.Lock`:
   ```python
   self._dataframe_locks: dict[tuple[str, str], asyncio.Lock] = {}
   # In cache access:
   cache_key = (api_key_hash, category)
   if cache_key not in self._dataframe_cache:
       lock = self._dataframe_locks.setdefault(cache_key, asyncio.Lock())
       async with lock:
           if cache_key not in self._dataframe_cache:  # double-check
               data = await self._download_from_appwrite(...)
               self._dataframe_cache[cache_key] = FAO_PGMDataset(data)
   ```

3. **`_file_cache`:** Same pattern as `_dataframe_cache` if downloads involve `await`;
   `setdefault()` if synchronous.

4. **CIC update:** Update `docs/CICs/FAOApiManager.md` to declare concurrency model per cache.

5. **Lock lifecycle:** Lock dict grows monotonically but is bounded by (API keys x categories).
   If C-07 eviction is implemented, evict locks alongside cache entries.

---

## Validation & Monitoring

- **Stress test:** Send 10+ concurrent requests for the same uncached key. Verify only one
  Appwrite download occurs (via logging).
- **Lock contention:** Add debug logging on lock acquire/release; monitor wait times.
- **Reconsideration trigger:** If free-threaded Python (PEP 703) is adopted, re-evaluate all
  dict access patterns — GIL-atomicity assumptions would no longer hold.
- **CIC compliance:** Verify new stateful classes declare concurrency models during review.

---

## Implementation Status

**As of 2026-06-03:** This ADR was accepted but the `setdefault()` and `asyncio.Lock` patterns described in Implementation Notes have **not yet been applied**. The caches were migrated from plain dicts to `cachetools.LRUCache`/`TTLCache` (ADR-011), which changes the API surface but does not eliminate the check-then-set race condition across `await` points.

Current risk: duplicate Appwrite downloads on concurrent cache misses for the same key. The consequence is wasted resources, not data corruption, because cached entries are immutable once stored. Implementation is deferred until concurrent load testing confirms the practical impact.

---

## Open Questions

- Should `_dataframe_locks` be bounded with LRU eviction, or is monotonic growth acceptable?
- Should the per-key lock pattern be extracted into a reusable `AsyncKeyedLock` utility?
- How should lock eviction interact with cache eviction when C-07 is resolved?

---

## References

- C-01 (resolved) in the technical risk register (`reports/technical_risk_register.md`)
- C-07 in the risk register (unbounded caches — eviction interacts with lock lifecycle)
- ADR-006: Intent Contracts for Non-Trivial Classes (concurrency model requirement)
- `docs/CICs/FAOApiManager.md` — CIC for `FAOApiManager`
- `managers/api.py:190-196` — cache dict declarations
- `managers/api.py:483-491` — `_manager_cache` check-then-set
- `managers/api.py:546-551`, `593-617` — `_dataframe_cache` check-then-set with async I/O
