# ADR-028: faoapi's Position in the Platform — the Terminal-Consumer Boundary

**Status:** Accepted
**Date:** 2026-06-26
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** ADR-024 (raw-count serving), ADR-026 (API surface), ADR-023 (re-baselining governance), views-postprocessing `un_crafd` (producer), views-pipeline-core ADR-055, the live config in `un_crafd/config_queryset.py`; risk register C-86, C-71, C-72
**Informed:** UN FAO / FSFC; platform maintainers

---

## Context

Issue #6 asks for an ADR on "the orchestration flow between models and post-processing": how model/ensemble prediction connects to reconciliation, calibration, aggregation, and scheduling, and how outputs are registered and passed downstream. Most of that flow is **upstream of faoapi** and is governed by other repositories' decisions (views-models, views-pipeline-core, views-postprocessing). What is genuinely a *faoapi* decision — and what has never been written down — is **where faoapi's boundary sits in that flow**: what it consumes, what it is forbidden from doing, and the single integration point it depends on.

Recording this matters now because: (a) the views-frames adoption (epic #87) and the ADR-024/025 schema work make the producer→consumer contract explicit and FAO-facing; (b) a stale internal README once described faoapi as reading from viewser when the live `un_crafd` queryset in fact pulls from views-datafactory (a correction made during the session that produced C-86) — exactly the kind of mis-documented boundary an ADR prevents; and (c) without a stated boundary, faoapi risks accreting upstream responsibilities (a "helpful" reconciliation or transform) that belong to the pipeline, re-creating the coupling the platform is removing.

This ADR documents the boundary as faoapi sees it. It does **not** restate or own the upstream orchestration — that is the upstream repos' to govern.

---

## Decision

### 1. faoapi is the terminal consumer; the orchestration is upstream

The model→post-processing→delivery flow runs **entirely upstream** of faoapi and is owned by upstream ADRs:

```
views-models / ensembles  ──▶  views-pipeline-core / views-postprocessing
   (prediction)                 (reconciliation, calibration, aggregation,
                                 GAUL enrichment, scheduling)
                                              │  writes artifact
                                              ▼
                                 Appwrite prediction bucket
                                              │  reads latest artifact
                                              ▼
                                 faoapi  (summarise + serve)  ──▶  UN FAO
```

faoapi's responsibility begins at **reading the latest artifact from the Appwrite prediction bucket** and ends at **serving summaries over HTTP** (ADR-026). Everything to the left of the bucket is not faoapi's decision to make.

### 2. The producer is the `un_crafd` post-processor; the integration point is the Appwrite bucket

The artifacts faoapi serves are produced by the **views-postprocessing `un_crafd`** post-processor, which pulls upstream predictions (per its live `config_queryset.py`, from **views-datafactory** — *not* viewser), performs the GAUL metadata enrichment (the 9-column contract), and writes `category="historical"` / `category="forecast"` artifacts into the Appwrite bucket faoapi reads. **The Appwrite bucket is the sole integration point.** faoapi and the producer share the bucket/credential configuration; there is no direct call between them.

**Upstream sources of truth (linked, not restated).** Two facts about the artifact are owned upstream; faoapi points to them so each is maintained in exactly one place:

- **Geographic scope (which cells/countries) is an upstream config**, never a faoapi setting: `views-models/postprocessors/un_crafd/configs/config_queryset.py` (`REGION`), resolved against `views-datafactory` `datafactory_query/regions.py` — e.g. `africa_me_legacy` (≈ 13,110 Africa+Middle-East cells) vs `land_gaul` (64,742 global-land cells, = the rusty_bucket forecast grid). faoapi serves whatever scope the delivered artifact carries; it does not choose it.
- **What is delivered depends on the producer's *mode*** — source of truth `views-postprocessing/views_postprocessing/unfao/managers/unfao.py` (`_save` / `_save_contract`), wire format **ADR-013 "The Sampled-Forecast Wire Contract"** (views-postprocessing): the **legacy** path uploads *both* `category="historical"` **and** `category="forecast"`; the **wire-contract** path (`_save_contract`) delivers the **forecast run only** — the historical frame is coverage-validated (`_check_coverage`) but **not uploaded**.

**Consumer-visible consequence (faoapi's to track).** The `/historical/*` endpoints and the bulk `s_actual` column are fed by the delivered *historical* artifact. A pure wire-contract cutover therefore stops that artifact refreshing — historical goes silently stale, and `s_actual` goes all-`NaN` — unless historical delivery is added to the contract path. Confirm at cutover (**risk register C-169**).

### 3. faoapi does not run models, reconcile, calibrate, or transform

faoapi is **explicitly forbidden** from performing upstream operations: it runs no model or ensemble, performs no reconciliation or calibration, applies and inverts **no** target transform (ADR-024), and does not re-derive the cross-model aggregation that produced the artifact. The only computation faoapi performs is **summarisation of the delivered posterior samples** (MAP/HDI via views-frames, ADR-026) and the **conservation-correct spatial roll-up** of those samples to administrative levels (the joint-sum, C-70) — both operate on the artifact as received, in the space it arrives in.

### 4. The boundary is trust-but-verify, with provenance

Because faoapi trusts an upstream producer it does not call, it treats the artifact as a **checked input, not an assumed-correct one**: value/metadata plausibility gates at ingestion (C-72), a raw-scale sanity guard (ADR-024 Decision 4), and a **provenance/lineage record** (`GET /provenance/{category}`, C-86) that makes "which file, from which upstream source/pipeline, is live" auditable — so a silent producer or source switch is detectable rather than invisible.

### 5. Scope

This ADR fixes **faoapi's boundary and integration point**. It does *not* define or own the upstream orchestration (scheduling, ensemble composition, reconciliation/calibration logic, model registration) — those are governed by views-models, views-pipeline-core, and views-postprocessing ADRs, referenced here. It does not change the API surface (ADR-026) or the schema (ADR-025).

---

## Rationale

- **A boundary you can name is a boundary you can defend.** The single most valuable thing faoapi can record about the orchestration is *where it stops* — so contributors do not migrate upstream logic into the consumer "for convenience," re-creating the coupling epic #87 removes.
- **One integration point (the bucket) is the simplest correct seam.** A bucket handoff decouples producer and consumer schedules, needs no service-to-service auth between them, and lets either side evolve as long as the artifact contract (ADR-024/025) and the GAUL metadata contract hold.
- **The viewser→datafactory correction proves the hazard.** A boundary documented only in a stale README produced a wrong claim about faoapi's own data source; an ADR with the live producer config as its reference is the fix.
- **Trust-but-verify is the honest posture for a delegated boundary.** faoapi cannot guarantee what it does not produce; plausibility gates plus provenance convert that exposure from a hidden assumption into a checked, auditable fact.

---

## Considered Alternatives

### A: faoapi pulls directly from the upstream store (viewser/datafactory), bypassing the bucket
- **Pros:** one fewer hop; fresher data.
- **Cons:** couples faoapi to the upstream store's schema and availability, duplicates the `un_crafd` enrichment (GAUL metadata) the post-processor already does, and ties the API's uptime to the pipeline's. **Rejected** — the bucket handoff is the decoupling.

### B: faoapi performs its own reconciliation/calibration as a "value-add"
- **Pros:** could correct or enrich the artifact at serve time.
- **Cons:** re-implements upstream science in the wrong layer, diverges from the pipeline's ratified outputs, and violates ADR-024's "no transform at the consumer." **Rejected.**

### C: A direct producer→faoapi API call (push)
- **Pros:** immediate delivery on new artifacts.
- **Cons:** introduces service-to-service auth and coupling, and a failure mode where a producer push outage stalls delivery; the bucket's "latest" semantics already give faoapi what it needs. **Rejected** (revisit only if push-freshness becomes a requirement).

---

## Consequences

### Positive
- The consumer boundary is explicit; upstream logic has a documented reason not to leak into faoapi.
- Producer and consumer evolve independently behind the bucket + artifact/metadata contracts.
- Provenance + plausibility make the trusted boundary auditable rather than blind.

### Negative / trade-offs
- faoapi's correctness and freshness are **coupled to the `un_crafd` producer** doing its job (writing a current, correct artifact). This dependency is real and now documented; faoapi surfaces `source="unknown"` and stale-artifact signals rather than hiding it (C-86, `staleness_threshold_hours`).
- Full *pipeline identity* (viewser-era vs datafactory) in the provenance record still requires the producer to stamp a `source`/`pipeline` field — an upstream-only residual (C-86); faoapi reports `"unknown"` until then.

---

## Implementation Notes

- Producer reference: views-postprocessing `un_crafd/config_queryset.py` (the live source = views-datafactory), `un_crafd` GAUL enrichment (9-column metadata contract). faoapi must **not** be modified to call upstream stores directly.
- Consumer entry: `PredictionStoreManager.get_latest_*` reads the latest artifact of a category from the Appwrite bucket; `_get_latest_dataframe` logs the provenance record on entry (C-86).
- The shared bucket/credential configuration is environment-driven on both sides (`APPWRITE_CRAFD_BUCKET` and the project env vars; ADR-013).

---

## Open Questions

- **Producer-side source stamping** — when `un_crafd` stamps a `source`/`pipeline` field, faoapi's provenance ceases to report `"unknown"`; coordinate the field name with the producer (C-86 residual).
- **Push vs pull freshness** — the latest-artifact pull is adequate today; revisit if FAO requires near-real-time delivery on new artifacts.
- **Wire format** — the bucket currently carries nested-array parquet; the native views-frames wire format is a separate, upstream-gated decision (#100, blocked on views-postprocessing#45).

---

## References

- faoapi **ADR-024** (raw-count serving — the consumer-end scale contract), **ADR-026** (API surface), **ADR-023** (re-baselining governance), **ADR-013** (env-var validation)
- Upstream: views-postprocessing `un_crafd` (`config_queryset.py`); views-pipeline-core **ADR-055** (raw-space I/O contract)
- Risk register: **C-86** (provenance/lineage), **C-71** (artifact promotion), **C-72** (value/metadata plausibility), cluster **A** (upstream-data trust boundary)
- Issue **#6** (orchestration flow between models and post-processing)
