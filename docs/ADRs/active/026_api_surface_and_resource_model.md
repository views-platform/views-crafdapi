# ADR-026: API Surface and Resource Model

**Status:** Accepted
**Date:** 2026-06-26
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** ADR-024 (raw-count serving), ADR-025 (output schema & naming), ADR-022 (deployment strategy), ADR-008 (observability & explicit failure), ADR-018 (SDK response normalization), the live route table in `src/views_crafdapi/managers/api.py`
**Informed:** UN FAO / FSFC API consumers

---

## Context

faoapi exposes VIEWS conflict data to UN FAO over HTTP. The route table grew organically in `managers/api.py` (`CrafdApiManager._register_routes`) and has never been described in a decision record. Issue #4 ("add_api_adrs") asks for the API's design to be made explicit before it is treated as a stable, FAO-facing contract; issue #22 ("api_docs") asks for end-user documentation built on top of that record. Both need a single authoritative statement of **what the API surface is and why it is shaped that way** — the data it serves, the resource model, the URL grammar, and the response envelope — so the documentation, the tests, and any future endpoint can be checked against one source of truth rather than re-derived from the code each time.

A decision is needed now because: (a) FAO has live access and will build tooling against these paths, so the surface is becoming a contract (cf. ADR-025's amendment of Release Note 01); (b) the views-frames adoption (epic #87) is reshaping the internals behind these endpoints, and the *external* surface must be pinned independently of that churn; and (c) without a documented grammar, every new level or operation risks an ad-hoc path that breaks the pattern.

---

## Decision

### 1. Two data scopes are served: historical actuals and probabilistic forecasts

faoapi serves two **categories** of data, both originating upstream and read from the Appwrite prediction bucket (the producer boundary is ADR-028):

- **`historical`** — observed conflict actuals (UCDP/GED-derived fatality counts) underlying the forecasts.
- **`forecast`** — VIEWS probabilistic forecasts as posterior sample distributions.

Both are raw fatality counts (ADR-024), reported per violence type (`sb`/`ns`/`os`). faoapi serves the **latest** artifact of each category present in the bucket; it is not a time-versioned archive (a single "current" artifact per category, refreshed on upload — staleness governed by `staleness_threshold_hours`).

### 2. The resource model is `/{level}/{kind}/{category}/{operation}`

The data/analysis surface follows one URL grammar:

```
/{level}/{kind}/{category}/{operation}
   level     ∈ { pg, country, gaul0, gaul1, gaul2 }   (spatial aggregation level)
   kind      ∈ { data, analysis }                      (raw rows vs computed summaries)
   category  ∈ { historical, forecast }                (the two data scopes above)
   operation ∈ { subset, hdi-map }                     (per kind)
```

- **`data/.../subset`** returns the rows of the latest dataframe at `level`, filtered by the query parameters.
- **`analysis/.../hdi-map`** returns computed MAP + HDI summaries at `level`.
- The **`pg`** prefix is the native PRIO-GRID grain; `country`/`gaul0`/`gaul1`/`gaul2` are aggregation levels. Aggregation is performed by the conservation-correct joint-sum (the `aggregate=true` parameter; cf. C-70, the forecast `forecast/aggregate` path).

~~Two **non-levelled** convenience endpoints return the full latest dataframe of a category: `/data/historical/latest` and `/data/forecast/latest`.~~

> **Amended 2026-08-24 — both retired (register C-232).** They answered HTTP 200 with rows carrying no values: ADR-030 §5 moved samples and scalars out of `.dataframe`, and the handlers served that index-only frame directly. Measured live before removal: 88,357,820 bytes in 9.04 s for `/data/forecast/latest`, every row carrying only `month_id` and `priogrid_id`. Retired rather than fixed because nothing this repo ships called them, and any caller that did was already receiving a successful-looking empty answer — a 404 tells a caller to stop, a valueless 200 does not. The levelled `subset` routes serve the same need with a filter.

### 3. Auxiliary surfaces: provenance, file access, cache, health

Outside the data grammar, the API also exposes:

- **`GET /provenance/{category}`** — the lineage record of the currently-served artifact (which file, from which upstream source/pipeline) without fetching the heavy dataframe (C-86, ADR-028).
- **File access** — `GET /files/{bucket_id}`, `/files/{bucket_id}/{file_id}/info`, `/files/{bucket_id}/{file_id}/download`, `/files/{bucket_id}/{file_id}/cached` — direct, authenticated passthrough to the underlying Appwrite bucket (raw parquet retrieval).
- **Cache** — `GET /cache/stats`, `DELETE /cache` (per-key; ADR-011, ADR-027).
- **`GET /health`** and **`GET /`** (root: a self-describing endpoint index).

### 4. Uniform JSON response envelope

Every JSON endpoint returns `{"success": true, "data": {...}}` on success. Data payloads carrying a table include `dataframe` (or `hdi_map`), `shape`, `columns`, and an echo of the resolved `parameters`. Errors are raised as `HTTPException` with a status code and a `detail` string (explicit failure, ADR-008): `422` for an invalid category/parameter, `404` when no artifact exists, `500` for an unhandled error. The default media type is JSON; the file-download endpoint streams the raw artifact with its native media type.

### 5. Scope

This ADR governs the **HTTP surface and resource grammar**. It does *not* govern: the column schema inside the payload (ADR-025), the numerical scale (ADR-024), authentication (ADR-027), the upstream producer boundary (ADR-028), deployment/hosting (ADR-022), or the caching/eviction policy (ADR-011). New endpoints must either fit the `/{level}/{kind}/{category}/{operation}` grammar or be justified as an auxiliary surface here.

---

## Rationale

- **One grammar, not N ad-hoc paths.** Levels and operations are registered from a single loop over `levels × {subset, hdi-map} × {historical, forecast}` (`_register_routes`), so the surface is regular by construction: a consumer who learns one path knows them all, and a new level is data, not a new branch (OCP). This is the URL-level expression of the screaming-architecture goal.
- **Separating `data` from `analysis` keeps raw retrieval and computed summaries distinct.** A consumer who wants the posterior rows (`subset`) and one who wants decision-ready intervals (`hdi-map`) ask different questions; the path makes the difference explicit rather than overloading one endpoint with a "mode" flag.
- **Latest-artifact, not versioned-archive, matches the operational reality.** FAO consumes the current forecast each cycle; faoapi mirrors the bucket's "latest" semantics rather than inventing a versioning scheme the producer does not provide. Provenance (Decision 3) makes "which latest" auditable.
- **A uniform envelope makes success/failure machine-checkable** and lets the documentation (#22) describe one response shape instead of one per endpoint.

---

## Considered Alternatives

### A: A single parameterised endpoint (`/data?level=…&kind=…&category=…`)
- **Pros:** one route; fewer registrations.
- **Cons:** collapses the resource model into opaque query flags; harder to document, cache, and reason about; loses the self-describing path grammar. **Rejected.**

### B: Per-level hand-written endpoints
- **Pros:** explicit per-level signatures.
- **Cons:** five-fold duplication of identical handler logic; a new level means new copy-pasted routes (the inheritance-style trap epic #87 is removing). **Rejected** in favour of the registration loop.

### C: GraphQL surface
- **Pros:** flexible client-driven queries.
- **Cons:** large dependency and operational surface for a small, well-bounded resource model; the `subset` parameters already give the needed filtering; out of proportion for an MVP FAO delivery. **Rejected** (revisit only if consumers need joins faoapi cannot express).

---

## Consequences

### Positive
- The API surface is now an explicit contract that documentation (#22) and tests can be checked against.
- New levels/operations have an obvious, pattern-conforming home; deviations are visible as deviations.
- The uniform envelope and explicit error codes give consumers a stable integration target.

### Negative / trade-offs
- The path grammar pins `level`/`category`/`operation` vocabularies; renaming any of them is a consumer-visible change (coordinate as with ADR-025 / Release Note 01).
- The "latest artifact" model means a consumer cannot request a historical *vintage* of a forecast through the data endpoints; only the current artifact and its provenance are exposed. (A versioned-delivery need would be a new decision.)

---

## Implementation Notes

- The surface lives in `src/views_crafdapi/managers/api.py` (`CrafdApiManager._register_routes`); the level loop is the canonical list of levelled routes. FastAPI auto-generates OpenAPI at `/docs` and `/openapi.json`, which the #22 documentation should reference rather than restate.
- Query-parameter parsing helpers (`parse_list_param`, `parse_string_list_param`) are shared by `subset` and `hdi-map`; `country` entity IDs parse as strings, other levels as integers.
- Any new endpoint should return the Decision-4 envelope and raise `HTTPException` with an explicit status, not return an error object inside a `200`.

---

## Open Questions

- Whether the bulk-parquet channel of ADR-025 is exposed as a new auxiliary endpoint (e.g. `/{level}/bulk`) or delivered out-of-band; to be settled when the bulk writer is built.
- Whether `hdi-map` adopts the richer ADR-025 column set (3 HDIs + `severe_scenario` + `actual`) on the same path or under a new operation name during the transition (cf. C-144, the credible-level reconciliation).
- API versioning (a `/v1` prefix) is not currently applied; revisit before the surface is frozen for external SLAs.

---

## References

- faoapi **ADR-024** (raw-count serving), **ADR-025** (output schema & naming), **ADR-027** (authentication & per-key isolation), **ADR-028** (terminal-consumer boundary), **ADR-022** (deployment), **ADR-011** (caching), **ADR-008** (observability & explicit failure)
- Live surface: `src/views_crafdapi/managers/api.py` (`CrafdApiManager._register_routes`)
- Risk register: **C-86** (provenance), **C-70** (aggregation joint-sum), **C-144** (credible-level reconciliation)
- Issues **#4** (API ADRs), **#22** (API documentation)
