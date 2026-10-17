# Class Intent Contract: DatasetService

**Status:** Active
**Owner:** Project maintainers
**Last reviewed:** 2026-08-21
**Related ADRs:** ADR-001 (Infrastructure category), ADR-008 (logging and observability), ADR-009 (boundary contracts and configuration validation), ADR-011 (caching; the disk TTL does not bound the fallback), ADR-027 (authentication; §4 revocation), ADR-033 (fail-visible selection — §6a is the composed degraded-serving policy this class implements)
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
- **Streamed historical ingest (C-263 / #98):** on a tier-3 miss for `category="historical"`, the artifact is first offered to `forecast.ingestion.historical_stream.stream_to_value`, which assembles the value-dir a parquet row group at a time instead of decoding the whole 28.4M-row frame — 3.9 GB peak against 12.2 GB, measured. It applies only to an artifact already dense and already in `sort_index()` order, and raises `NotStreamable` otherwise; the in-memory path below then runs **unchanged**. A refusal is not an error: the request succeeds, at the old memory cost. C-72 plausibility runs *while streaming*, before anything is committed, and a violation raises `ImplausibleArtifact` → HTTP 500 rather than falling back (C-277).
- **Provenance (C-86):** the latest artifact is resolved via `manager.get_latest_provenance(filters={"category": …})`; the lineage (file id, declared upstream `source`, `methodology_version`, hash, created-at, filename) is logged and stored on the cache entry. No forecast/historical artifact → `HTTPException(404)`.
- **Format auto-detection:** parquet → CSV (utf-8 only) → JSON → feather. **Pickle is excluded** (C-59, RCE on untrusted bytes); a `PAR1` magic header that fails to parse short-circuits to 500 (C-52).
- **Plausibility (C-72):** before caching/serving, `validate_value_plausibility()` + `validate_metadata_plausibility()` run; a violation surfaces as 500 — implausible data fails loud rather than reaching FAO.
- **Cache key parity:** the API-key hash is computed by the injected `api_key_hash_fn` (the same function `CrafdApiManager` uses for routes), so service-written and route-read cache entries always share a key.
- **Return contract:** `get_latest_dataset` returns a **copy of the dataset this request selected** — warm, disk, a fresh wire ingest, or the last-good fallback — never whatever the warm cache happens to hold afterwards. The fallback leaves the warm cache empty by design (ADR-033 decision (i)), so a caller that re-read the cache got `None` on a cold worker and a different run on a warm one (C-356, #156). `get_latest_dataframe` is `get_latest_dataset(...).dataframe.copy()`; no route uses it, and it is kept as a test seam.

---

## 4. Inputs and Assumptions

- Constructor (keyword-only): `dataframe_cache`, `file_cache`, `disk_cache` (a `CrafdDiskCacheManager` with `ttl_seconds`/`read`/`write`), `prediction_bucket_id`, `configs_getter` (`() -> dict`, read lazily at request time for `historical_targets`), `api_key_hash_fn` (`str -> str`), `check_staleness_fn` (`float -> StalenessResult`-like).
- `manager` (per call) is an already-authenticated `PredictionStoreManager`.
- Assumes the injected caches are the same objects `CrafdApiManager` exposes elsewhere (shared by reference).

---

## 5. Outputs and Side Effects

- Returns a pandas `DataFrame` / `ForecastDataset` (copies).
- **Side effects:** mutates the injected in-memory caches; writes the disk cache; logs provenance + staleness + format-detection at INFO/DEBUG. On the streamed historical path it additionally stages the downloaded artifact to a temp file under the disk cache's staging dir and adopts the assembled value-dir with `write_value_dir` (previously wire-only), discarding the staging dir on any failure.

---

## 6. Failure Modes and Loudness

- No artifact for the category → `HTTPException(404)`.
- Download failure / empty bytes / unparseable format / implausible values → `HTTPException(500)` (fail loud; never serve corrupt data).
- Forecast only: a manifested wire run whose declared `contract_version` this build cannot render (`forecast/contract.can_render_contract` — different major, newer minor, or unparseable) is `Refused("schema_capability_mismatch")` **before any shard is fetched** (S5 / ADR-033 §7, C-171) — never served in a degraded/old schema. The build's capability is `SERVED_CONTRACT_VERSION`, surfaced at `/version`.
- Forecast only: a manifested wire run whose manifest pins no `sha256` for any shard or for the sidecar (`RunManifest.unhashed_artifacts`) is `Refused("manifest_unhashed")` **before any shard is fetched** (ADR-033 §6a rule 8, C-234). A pinned hash that does not match the bytes is `Refused("ingest_failed")`.
- Forecast only: a manifested wire run **present but unservable** (failed integrity / capacity / parse, or an unidentifiable source) **or absent entirely** (`NoRun` — no manifest / a manifest quarantined-to-nothing) → the **bounded grace fallback** (`_serve_last_good_within_sla`, S4 + S1/#264 / ADR-033 §2/§6): serve the last-good **manifested** run persisted on disk **iff** its `created_at` is positively within the freshness SLA (S3) — otherwise `HTTPException(503)`. Stale *or* unknown-age ⇒ 503. It **never** serves a loose legacy artifact for a forecast (S1 retired that fallback; the C-170/C-71 fix). The guard holds on the cache tiers too — a warm/disk *forecast* entry is served only when it is a WIRE entry (`_forecast_entry_servable`), never a legacy one. The fallback is loud, not silent: it does not repopulate the warm cache (so every request re-evaluates the newest run), logs a WARNING, and sets a degraded `forecast_serving_state()` — `{degraded, reason, fallback_available, serving, file_id, created_at, age_days, sla_days}` while a last-good run is served (`serving: "last_good_manifested"`), or `{degraded, reason, fallback_available: false, last_good_file_id?, last_good_age_days?, last_good_withdrawn?, sla_days?}` when nothing could be served; `reason` is the refusal reason or `"no_manifest"`. A third degraded state, `serving: "cached_unverified"` with `reason: "manifest_lookup_error"`, means the cached run was served while the manifest lookup failed (ADR-033 §6a) surfaced on `/health` + `/provenance`; the flag clears on the next normal serve. `/historical` is unaffected — it still serves loose legacy files (until the producer co-delivers historical on the wire path, epic #263 S5 / C-169). The FAO-facing HTTP contract for the degraded state (D3) is finalised in S7 with Pre-Release Note 07.
- **Served-decision surface (S7/#252):** `forecast_serving_state()` returns the last forecast-serve outcome (`None`, `{degraded: False}`, or the degraded/fallback dict above); `served_forecast_provenance()` returns the provenance of the forecast **actually being served** (`{file_id, mode, status, source, created_at, …}`; for a forecast `mode` is always `"wire"` — the legacy forecast path was retired in S1/#264). Both are single-value (last-serve) surfaces the API layer reads for `/health` and `/provenance` — authoritative over the store's newest record, which may differ from the served run.
- Staleness-check failure → caught, logged at DEBUG, **never blocks** the response (staleness is advisory).

---

## 7. Boundaries and Interactions

- **Composed by** `CrafdApiManager` (constructed in `__init__` and `from_config`); route handlers call it via the thin `_get_latest_dataset` delegator; `_get_latest_dataframe` survives only as the test seam (C-356, #156). `/health` and `/provenance/forecast` also read `served_forecast_provenance()` and `forecast_serving_state()` directly.
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
dataset = service.get_latest_dataset(prediction_manager, x_api_key, "forecast")  # what routes use
```

## 9. Examples of Incorrect Usage

```python
# WRONG: constructing private caches inside the service — they must be the
# SAME objects CrafdApiManager owns, or routes/lifecycle will see stale state.
DatasetService(dataframe_cache={}, ...)  # only acceptable in isolated unit tests
```

---

## 10. Test Alignment

- `tests/test_dataset_service.py` — isolated unit tests (cache tiers, force_refresh, download+provenance, 404/500; a refused download is 401, a failed one a 500 without the store's text; a historical lookup outage is 503).
- `tests/test_dataset_service_fallback.py` — the bounded last-good fallback (ADR-033 §6/§6a): within/past the SLA and unknown age, `NoRun`, recovery; on a cold and a restarted worker; the run returned is the run selected (C-356); past the disk TTL but within the SLA (C-360); never a run the operator withdrew; never for a refused key (401); the copy contract.
- `tests/test_dataset_service_wire.py` — the wire path and its identity guards, including a lookup outage served `cached_unverified` and bounded by the operator controls and the SLA.
- `tests/test_api_endpoints.py::TestDegradedFallbackAcrossARestart`, `::TestARevokedKeyIsRefusedOnItsNextRequest` — the same contracts through HTTP.
- Parity (behaviour-preserving extraction): `tests/test_api_endpoints.py`, `tests/test_cache_*.py`, `tests/test_disk_cache_versioning.py`, `tests/test_staleness_detection.py`, `tests/forecast/test_served_output_golden.py`.
