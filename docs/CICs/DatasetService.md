# Class Intent Contract: DatasetService

**Status:** Active
**Owner:** Project maintainers
**Last reviewed:** 2026-06-27
**Related ADRs:** ADR-001 (Infrastructure category), ADR-008 (logging and observability), ADR-009 (boundary contracts and configuration validation)
**Source:** epic #144 / C-36, S2 — extracted from `CrafdApiManager._get_latest_dataframe`

---

## 1. Purpose

> **What is this class for?**

`DatasetService` owns the **data-fetch pipeline** for the views-faoapi service: given a `PredictionStoreManager`, an API key, and a category (`historical`/`forecast`), it returns the latest `ForecastDataset` / its DataFrame, served through a three-tier cache (in-memory → disk → remote Appwrite download), with format-cascade parsing, value/metadata plausibility validation (C-72), disk-cache persistence, and provenance/lineage recording (C-86). It was extracted from `CrafdApiManager` so the fetch/cache strategy has one reason to change, separate from the HTTP surface and lifecycle.

---

## 2. Non-Goals (Explicit Exclusions)

- Does **not** own the caches it uses. `dataframe_cache`, `file_cache`, and `disk_cache` are owned by `CrafdApiManager` and injected by reference, so routes, lifecycle clears, and cache-stats observe the same objects.
- Does **not** do HTTP routing, authentication, or FastAPI dependency wiring. It receives an already-authenticated `PredictionStoreManager`; the `Depends`/`X-API-Key` seam stays in `CrafdApiManager`.
- Does **not** compute statistics or own data semantics. It constructs a `ForecastDataset` and validates plausibility; MAP/HDI and column meaning live in the domain layer.
- Does **not** manage the Appwrite SDK or perform uploads. It only *reads* (downloads) via the injected manager and *writes the disk cache* via `CrafdDiskCacheManager`.

---

## 3. Responsibilities and Guarantees

- **Three-tier fetch:** (1) in-memory `dataframe_cache[api_key_hash][category]` (TTL-bounded; advisory staleness warning on hit), (2) disk cache via `CrafdDiskCacheManager`, (3) remote Appwrite download. A hit at any tier populates the higher tiers. `force_refresh=True` bypasses tiers 1–2.
- **Provenance (C-86):** the latest artifact is resolved via `manager.get_latest_provenance(filters={"category": …})`; the lineage (file id, declared upstream `source`, `methodology_version`, hash, created-at, filename) is logged and stored on the cache entry. No forecast/historical artifact → `HTTPException(404)`.
- **Format auto-detection:** parquet → CSV (utf-8 only) → JSON → feather. **Pickle is excluded** (C-59, RCE on untrusted bytes); a `PAR1` magic header that fails to parse short-circuits to 500 (C-52).
- **Plausibility (C-72):** before caching/serving, `validate_value_plausibility()` + `validate_metadata_plausibility()` run; a violation surfaces as 500 — implausible data fails loud rather than reaching FAO.
- **Cache key parity:** the API-key hash is computed by the injected `api_key_hash_fn` (the same function `CrafdApiManager` uses for routes), so service-written and route-read cache entries always share a key.
- **Return contract:** `get_latest_dataframe` returns a **copy** of the cached DataFrame; `get_latest_dataset` returns a **copy** of the cached `ForecastDataset`.

---

## 4. Inputs and Assumptions

- Constructor (keyword-only): `dataframe_cache`, `file_cache`, `disk_cache` (a `CrafdDiskCacheManager` with `ttl_seconds`/`read`/`write`), `prediction_bucket_id`, `configs_getter` (`() -> dict`, read lazily at request time for `historical_targets`), `api_key_hash_fn` (`str -> str`), `check_staleness_fn` (`float -> StalenessResult`-like).
- `manager` (per call) is an already-authenticated `PredictionStoreManager`.
- Assumes the injected caches are the same objects `CrafdApiManager` exposes elsewhere (shared by reference).

---

## 5. Outputs and Side Effects

- Returns a pandas `DataFrame` / `ForecastDataset` (copies).
- **Side effects:** mutates the injected in-memory caches; writes the disk cache; logs provenance + staleness + format-detection at INFO/DEBUG.

---

## 6. Failure Modes and Loudness

- No artifact for the category → `HTTPException(404)`.
- Download failure / empty bytes / unparseable format / implausible values → `HTTPException(500)` (fail loud; never serve corrupt data).
- Forecast only: a manifested wire run whose declared `contract_version` this build cannot render (`forecast/contract.can_render_contract` — different major, newer minor, or unparseable) is `Refused("schema_capability_mismatch")` **before any shard is fetched** (S5 / ADR-033 §7, C-171) — never served in a degraded/old schema. The build's capability is `SERVED_CONTRACT_VERSION`, surfaced at `/version`.
- Forecast only: a manifested wire run **present but unservable** (failed integrity / capacity / parse, or an unidentifiable source) **or absent entirely** (`NoRun` — no manifest / a manifest quarantined-to-nothing) → the **bounded grace fallback** (`_serve_last_good_within_sla`, S4 + S1/#264 / ADR-033 §2/§6): serve the last-good **manifested** run persisted on disk **iff** its `created_at` is positively within the freshness SLA (S3) — otherwise `HTTPException(503)`. Stale *or* unknown-age ⇒ 503. It **never** serves a loose legacy artifact for a forecast (S1 retired that fallback; the C-170/C-71 fix). The guard holds on the cache tiers too — a warm/disk *forecast* entry is served only when it is a WIRE entry (`_forecast_entry_servable`), never a legacy one. The fallback is loud, not silent: it does not repopulate the warm cache (so every request re-evaluates the newest run), logs a WARNING, and sets a degraded `forecast_serving_state()` (`{degraded, reason, fallback_available, file_id, age_days, sla_days}`; `reason` is the refusal reason or `"no_manifest"`) surfaced on `/health` + `/provenance`; the flag clears on the next normal serve. `/historical` is unaffected — it still serves loose legacy files (until the producer co-delivers historical on the wire path, epic #263 S5 / C-169). The FAO-facing HTTP contract for the degraded state (D3) is finalised in S7 with Pre-Release Note 07.
- **Served-decision surface (S7/#252):** `forecast_serving_state()` returns the last forecast-serve outcome (`None`, `{degraded: False}`, or the degraded/fallback dict above); `served_forecast_provenance()` returns the provenance of the forecast **actually being served** (`{file_id, mode, status, source, created_at, …}`, `mode ∈ {"wire","legacy"}`). Both are single-value (last-serve) surfaces the API layer reads for `/health` and `/provenance` — authoritative over the store's newest record, which may differ from the served run.
- Staleness-check failure → caught, logged at DEBUG, **never blocks** the response (staleness is advisory).

---

## 7. Boundaries and Interactions

- **Composed by** `CrafdApiManager` (constructed in `__init__` and `from_config`); route handlers call it via the thin `_get_latest_dataframe`/`_get_latest_dataset` delegators.
- **Depends on** `PredictionStoreManager` (injected per call), `CrafdDiskCacheManager`, `ForecastDataset` (domain).
- **Does not** depend on FastAPI routing or `CrafdApiManager` internals beyond the injected collaborators (DIP).

---

## 8. Examples of Correct Usage

```python
service = DatasetService(
    dataframe_cache=self._dataframe_cache,
    file_cache=self._file_cache,
    disk_cache=self._disk_cache,
    prediction_bucket_id=self._prediction_bucket_id,
    configs_getter=lambda: self.configs,
    api_key_hash_fn=self._get_api_key_hash,
    check_staleness_fn=self._check_staleness,
)
df = service.get_latest_dataframe(prediction_manager, x_api_key, "forecast")
```

## 9. Examples of Incorrect Usage

```python
# WRONG: constructing private caches inside the service — they must be the
# SAME objects CrafdApiManager owns, or routes/lifecycle will see stale state.
DatasetService(dataframe_cache={}, ...)  # only acceptable in isolated unit tests
```

---

## 10. Test Alignment

- `tests/test_dataset_service.py` — isolated unit tests (cache tiers, force_refresh, download+provenance, 404/500).
- Parity (behaviour-preserving extraction): `tests/test_api_endpoints.py`, `tests/test_cache_*.py`, `tests/test_disk_cache_versioning.py`, `tests/test_staleness_detection.py`, `tests/forecast/test_served_output_golden.py`.
