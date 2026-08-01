# FAO Forecast API — Reference

Consolidated reference for the `views-faoapi` HTTP service: data scopes, authentication, the endpoint catalogue, query parameters, the response envelope, and usage examples. This document is the human-readable companion to the auto-generated OpenAPI schema and to the governing ADRs.

- **Resource model & surface:** [ADR-026](../ADRs/active/026_api_surface_and_resource_model.md)
- **Authentication:** [ADR-027](../ADRs/active/027_authentication_and_per_key_isolation.md)
- **Where the data comes from:** [ADR-028](../ADRs/active/028_terminal_consumer_boundary.md)
- **Numerical scale (raw counts):** [ADR-024](../ADRs/active/024_raw_count_serving_contract.md)
- **Output schema & column naming:** [ADR-025](../ADRs/active/025_fao_output_schema_and_naming.md)
- **Column definitions (analyst-facing):** [data_dictionary.md](data_dictionary.md) — what every served column means + the three naming conventions
- **Live OpenAPI:** `GET /openapi.json` · interactive docs at `GET /docs` (Swagger UI) and `GET /redoc`

> The interactive `/docs` page is generated from the running service and is always authoritative for exact parameter types. This file gives the conceptual map and stable examples.

---

## 1. Overview

faoapi serves VIEWS conflict data to UN FAO over HTTP. It is the **terminal consumer** of the VIEWS pipeline (ADR-028): it reads the latest artifacts produced upstream by the `un_crafd` post-processor from an Appwrite bucket, computes summaries, and serves them as JSON. It runs no models and applies no transforms — all values are **raw fatality counts** (ADR-024).

**Two data scopes (`category`):**

| Category | Meaning |
|----------|---------|
| `historical` | Observed conflict actuals (UCDP/GED-derived fatality counts). |
| `forecast` | VIEWS probabilistic forecasts, as posterior sample distributions. |

Every quantity is reported per **violence type**: `sb` (state-based), `ns` (non-state), `os` (one-sided).

**Base URL (production):** `https://faoapi.viewsforecasting.org`
**Local development:** `http://localhost:8000` (see the repository README for `uvicorn` startup).

---

## 2. Authentication

All data, analysis, file, provenance, and cache endpoints require an **`X-API-Key`** header whose value is your **Appwrite API key** (ADR-027). faoapi delegates authentication and authorization to Appwrite: what you can read is exactly what your key is scoped to. The key must be sent over HTTPS. faoapi never stores the raw key — it partitions per-caller caches by a truncated SHA-256 hash only.

```
X-API-Key: <your-appwrite-api-key>
```

A missing key returns `422`. An invalid/unscoped key surfaces as an upstream Appwrite error.

---

## 3. Response envelope

Every JSON endpoint returns:

```json
{ "success": true, "data": { ... } }
```

Tabular payloads (`data.dataframe` or `data.hdi_map`) also carry `shape`, `columns`, and an echo of the resolved `parameters`. Errors are returned as HTTP error status codes with a JSON `{"detail": "..."}` body:

| Status | Meaning |
|--------|---------|
| `422` | Missing/invalid parameter (e.g. unknown `category`, missing `X-API-Key`). |
| `404` | No artifact of the requested category exists in the bucket. |
| `500` | Unhandled server/upstream error (`detail` carries the message). |

---

## 4. Endpoint catalogue

### 4.1 Data & analysis (the resource grammar)

The levelled surface follows `/{level}/{kind}/{category}/{operation}` (ADR-026):

- **`level`** ∈ `pg` · `country` · `gaul0` · `gaul1` · `gaul2`
- **`kind`** ∈ `data` (raw rows) · `analysis` (computed summaries)
- **`category`** ∈ `historical` · `forecast`
- **`operation`** ∈ `subset` (for `data`) · `hdi-map` (for `analysis`)

| Method & Path | Purpose |
|---------------|---------|
| `GET /{level}/data/{category}/subset` | Rows of the latest dataframe at `level`, filtered by query params. |
| `GET /{level}/analysis/{category}/hdi-map` | MAP + HDI summaries at `level`. |
| `GET /data/{category}/latest` | The full latest dataframe of a category (non-levelled, PRIO-GRID grain). |

`pg` is the native PRIO-GRID grain; `country`/`gaul0`/`gaul1`/`gaul2` are aggregation levels (pass `aggregate=true` to roll up). Forecast aggregation uses the conservation-correct joint-sum (`HDI(Σ) ≠ ΣHDI`).

### 4.2 Provenance

| Method & Path | Purpose |
|---------------|---------|
| `GET /provenance/{category}` | Lineage record of the currently-served artifact (file id, hash, created-at, declared upstream source/pipeline, methodology version) — without fetching the dataframe. `category` ∈ `forecast`/`historical`. |

### 4.3 File access (raw Appwrite passthrough)

| Method & Path | Purpose |
|---------------|---------|
| `GET /files/{bucket_id}` | List files in a bucket (`limit` 1–1000, `offset`, `search`). |
| `GET /files/{bucket_id}/{file_id}/info` | File metadata. |
| `GET /files/{bucket_id}/{file_id}/download` | Download the raw artifact (native media type, e.g. parquet). |
| `GET /files/{bucket_id}/{file_id}/cached` | Serve the file from the local cache if present. |

### 4.4 Cache & service

| Method & Path | Purpose |
|---------------|---------|
| `GET /cache/stats` | Per-key cache statistics. |
| `DELETE /cache` | Clear the caller's cache partition. |
| `GET /ping` | **Unauthenticated liveness probe** — returns `200 {"status":"ok"}` iff the process is serving HTTP. No API key, no Appwrite dependency; the target for external uptime monitors. |
| `GET /version` | **Unauthenticated deployed-version probe** — `{"version": …, "deployed_tag": …}`: the installed package version and the pinned deploy tag (when the S4 deploy gate sets one). For verifying remotely which version is live. |
| `GET /health` | **Readiness check** — verifies the Appwrite connection and returns cache stats. **Requires an `X-API-Key` header.** |
| `GET /` | Self-describing index of endpoints. |

---

## 5. Query parameters

### `data/.../subset`

| Param | Type | Default | Meaning |
|-------|------|---------|---------|
| `time_ids` | comma-separated ints | all | Months (VIEWS `month_id`) to include. |
| `entity_ids` | comma-separated | all | Entity ids to include (strings for `country`, ints otherwise). |
| `features` | comma-separated strings | all | Feature/column names to include. |
| `sample_idx` | comma-separated ints | all | Posterior sample indices to include. |
| `with_metadata` | bool | `true` | Join the GAUL geographic metadata. |
| `aggregate` | bool | `false` | Roll up to `level` (joint-sum for forecasts). |
| `force_refresh` | bool | `false` | Bypass the cache and re-fetch the latest artifact. |

### `analysis/.../hdi-map`

All of the above **plus**:

| Param | Type | Default | Meaning |
|-------|------|---------|---------|
| `alpha` | float | `0.9` | Credible mass for the HDI (e.g. `0.9` = 90% HDI). |
| `enforce_non_negative` | bool | `false` | Clip MAP estimates at zero (counts cannot be negative). |

---

## 6. Examples

Latest forecast (full, PRIO-GRID grain):

```bash
curl -H "X-API-Key: $APPWRITE_DATASTORE_API_KEY" \
  https://faoapi.viewsforecasting.org/data/forecast/latest
```

Forecast HDI + MAP aggregated to GAUL admin-1, 90% HDI, for two months:

```bash
curl -H "X-API-Key: $APPWRITE_DATASTORE_API_KEY" \
  "https://faoapi.viewsforecasting.org/gaul1/analysis/forecast/hdi-map?aggregate=true&alpha=0.9&time_ids=541,542"
```

Country-level historical subset for one country (string entity id):

```bash
curl -H "X-API-Key: $APPWRITE_DATASTORE_API_KEY" \
  "https://faoapi.viewsforecasting.org/country/data/historical/subset?aggregate=true&entity_ids=SDN"
```

Provenance of the live forecast artifact:

```bash
curl -H "X-API-Key: $APPWRITE_DATASTORE_API_KEY" \
  https://faoapi.viewsforecasting.org/provenance/forecast
```

---

## 7. Versioning & change policy

The API surface, the column schema, and the numerical scale are FAO-facing contracts. Changes that alter published columns, names, or values are coordinated with FSFC and governed by [ADR-023](../ADRs/active/023_rebaselining_published_forecasts_governance.md) (re-baselining) and the FAO Release Notes. The current schema/naming convention and its amendment of Release Note 01 are described in [ADR-025](../ADRs/active/025_fao_output_schema_and_naming.md). No `/v1` URL prefix is applied yet (see ADR-026, Open Questions).

---

## 8. See also

- Repository [README](../../README.md) — installation, configuration, local startup.
- [ADRs index](../ADRs/active/) — all architecture decisions.
- Quickstart notebooks under `notebooks/` — worked retrieval/plotting examples.
