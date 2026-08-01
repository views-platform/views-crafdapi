# ADR-004: Rules for Evolution and Stability

**Status:** Accepted (Deferred)  
**Date:** 2026-06-02  
**Deciders:** Project maintainers  
**Informed:** All contributors  

---

## Context

The preceding ADRs establish:

- **ADR-001:** the ontology of the repository (what exists)
- **ADR-002:** the topology of the repository (how components may relate)
- **ADR-003:** semantic authority (who owns meaning and how it is declared)

Together, these decisions define the structure and semantics of views-faoapi at a point in time.

What they do **not** yet define is how the system is allowed to **change over time**:
- which components are expected to be stable
- which components may evolve freely
- what constitutes a breaking change
- when compatibility guarantees apply
- when a new ADR is required

These questions are architectural, cross-cutting, and costly to reverse once external users or downstream dependencies exist.

---

## Decision

Components in views-faoapi are classified into **stability tiers** that govern how they may change.

### Stability Classification

| Tier | Label | Rule |
|------|-------|------|
| **S1** | Frozen | Changes require a new ADR superseding the existing contract. External consumers depend on this. |
| **S2** | Stable | Changes require CIC update + migration path. No silent behavioral change. |
| **S3** | Internal | Changes require test updates. No external notification needed. |
| **S4** | Experimental | May change freely. No guarantees. |

### Current Classifications

| Component | Tier | Rationale |
|-----------|------|-----------|
| HTTP endpoint URL patterns (`/{level}/data/{type}/subset`, etc.) | S1 | FAO consumers depend on these URLs |
| Response JSON schema (`success`, `data.dataframe`, `data.shape`) | S1 | FAO consumers parse these fields |
| `X-API-Key` header authentication contract | S1 | FAO deployment uses this |
| `ForecastDataset` public API (CIC-governed) | S2 | Internal but CIC-bound |
| `AppWriteFileManager` public API (CIC-governed) | S2 | CIC-bound |
| `PosteriorDistributionAnalyzer` public API (CIC-governed) | S2 | CIC-bound |
| `_ViewsDataset` public API (CIC-governed) | S2 | CIC-bound, parent of domain hierarchy |
| Three-tier cache hierarchy (in-memory → disk → Appwrite) | S2 | Architectural commitment per CrafdApiManager CIC |
| Per-API-key isolation model | S2 | Multi-tenant design commitment |
| `_as_dict()` / `_get()` normalization boundary | S2 | ADR-018 governed |
| Internal cache sizes, TTLs, eviction parameters | S3 | Tunable without contract revision |
| Module-level helper functions (`parse_list_param`, etc.) | S3 | Internal utilities |
| `CrafdDiskCacheManager` internals | S3 | Implementation detail of cache tier |
| `client.py`, `plotting.py`, `time.py` (notebook utilities) | S4 | Recently extracted, API not yet committed |
| `wandb/utils.py` | S4 | Observability, no external consumers |

### When a New ADR Is Required

A new ADR is required when:
- An S1 component needs to change behavior (not just implementation)
- A new S1 commitment is being made (new external-facing contract)
- An existing ADR's decision is being reversed or significantly amended

### What Constitutes a Breaking Change

A breaking change is any modification that would cause an existing, correctly-behaving consumer to fail or produce different results. For S1 components, breaking changes require advance coordination with FAO stakeholders. For S2 components, the CIC must be updated before or alongside the change.

---

## Rationale

### Why now (trigger conditions met)

As of 2026-06-02, the following trigger conditions from the original deferral are satisfied:

- **External dependency exists:** The CRAF'd API is deployed in shadow mode at `faoapi.viewsforecasting.org` (Hetzner CPX52), serving the Complex Risk Analytics Fund (CRAF'd). Endpoint URLs and response schemas are committed external contracts.
- **Breaking changes have cost:** The CrafdApiManager CIC identifies endpoint URL patterns and per-API-key isolation as "core architectural commitments" (Section 11). Multiple CICs declare stable vs. candidate-for-change components.
- **Contributors need clarity:** 11 disagreements (D-01 through D-11) in the risk register reflect uncertainty about what is safe to change.

### Historical note (original deferral)

This ADR was originally deferred (2026-05-27) because core abstractions were still being refined and premature guarantees would constrain necessary exploration. The stability classifications above are derived from the CICs and ADRs that emerged during that exploration period.

---

## Trigger Conditions for Supersession

This ADR should be superseded by a new ADR when:

- Versioning schemes or release processes are needed
- Migration tooling becomes necessary for S1 or S2 changes
- Multiple concurrent versions of the same component must be supported
- The stability classifications above no longer reflect the system's actual commitments

---

## Non-Decisions (Explicitly Out of Scope)

This ADR does **not** define:
- Versioning schemes (e.g., semver for the API)
- Release processes or cadence
- Migration tooling for S1/S2 changes
- Deprecation mechanics or timelines
- How to communicate breaking changes to FAO stakeholders

Those topics require operational decisions that are premature until the stability classifications here are accepted and tested in practice.

---

## Consequences

### Positive
- Contributors know which components are safe to change and which require coordination
- S1/S2 classifications protect external consumers (FAO) from silent breaking changes
- S3/S4 classifications preserve internal flexibility where it is needed
- The stability tiers are derived from existing CIC declarations, not invented speculatively

### Negative
- S1 classification constrains endpoint URL and response schema evolution — any change requires a new ADR
- S2 classification adds overhead to CIC-governed class changes — CIC must be updated alongside code
- Classifications may need recalibration as the system evolves — initial assignments are based on current understanding

These consequences are accepted intentionally.

---

## Notes

The stability classifications in this ADR are derived from existing governance artifacts (CICs, ADR-002 topology, FAO deployment contracts), not invented independently. They codify commitments that were already implicit.

This ADR is proposed, not yet accepted. Once accepted, the classifications become binding: changes to S1 components require a new ADR, and changes to S2 components require CIC updates. Review the current classifications carefully before acceptance.
