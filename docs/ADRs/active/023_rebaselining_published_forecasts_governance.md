# ADR-023: Governance Gate for Re-baselining Published Forecasts

**Status:** Accepted  
**Date:** 2026-06-24  
**Deciders:** Simon (PRIO), Claude Code  
**Consulted:** ADR-021 (dense-grid fill semantics), risk register C-84, C-81, C-86, C-71  
**Informed:** FAO API consumers  

---

## Context

faoapi serves VIEWS conflict forecasts to the UN FAO. The *published numbers* — the MAP point estimate, the HDI interval bounds, and the aggregated level values — are the product. They are consumed as authoritative inputs to humanitarian planning.

Those numbers are a function not only of the upstream model output but of **faoapi's own methodology**: the point/interval estimator (`_tower_collapse` in `data/handlers.py`, with `data/statistics.py` as the parity reference), the aggregation rule (`HDI(Σ) ≠ Σ HDI` — collapse-after-sum), and the dense-grid fill semantics (ADR-021). A change to any of these can move the published values **without any change in the upstream data**.

This already happened. The M1 work (the views-frames tower-estimator swap, register C-81) deliberately re-baselined the published MAP/HDI on active cells. That change was validated and intentional — the evidence lives in `reports/views_frames_integration/`. But it exposed a governance gap (register **C-84**): there was **no documented process** for changing published numbers. No approval gate, no recorded before/after diff communicated to FAO, no methodology version, no rule about how such a change reaches production.

The risk is not the M1 change itself. It is the **absence of a gate**: a future estimator or aggregation change could reach FAO with no sign-off and no notice — eroding trust if noticed, or silently shifting a humanitarian input if not. This is a process artifact, not a code defect; nothing in the code is wrong, but nothing governs the change either.

A decision is needed now because the M1 cutover to production (`development` → `main`, PR #93) is the first re-baseline that would travel through this gap. The gate must exist before that merge.

---

## Decision

### 1. Define a "re-baselining change"

A change is **re-baselining** if it can alter the *published* forecast values (MAP, HDI bounds, `_min`/`_max`, or aggregated level outputs) for inputs that are otherwise unchanged. This includes, non-exhaustively:

- The estimator: `_tower_collapse`, `data/statistics.py`, or the views-frames-summarize version they call.
- The aggregation rule or grouping (`_aggregate_distributions` and the `ForecastDataset` override).
- Dense-grid fill semantics or `fill_value` defaults (ADR-021).
- `enforce_non_negative` / clipping behavior, or the value-plausibility gate (C-72) in a way that changes served values.
- Adopting a different upstream **source/pipeline** whose values differ (the viewser→datafactory switch; see C-86).

A change that cannot move published values for unchanged inputs (refactors, logging, caching, new endpoints, test-only changes) is **not** re-baselining and is not gated by this ADR.

### 2. The gate: re-baselining reaches `main` only with all four artifacts

Re-baselining changes may land on `development` freely (that is where they are built and validated). **Merging `development` → `main` (the production cutover) is the gate.** A PR that includes a re-baselining change MUST carry, before merge:

1. **Explicit sign-off** from the maintainer (Simon/PRIO) in the PR, naming the change as a re-baseline.
2. **A recorded before/after diff** of the published numbers — what changed for FAO, at what magnitude — committed under `reports/` (as the M1 evidence under `reports/views_frames_integration/` already models).
3. **A methodology version bump** (see §3) so the change is identifiable in served output and logs.
4. **FAO-facing change communication** — a dated note (in the diff report and/or the PR) describing the change in consumer terms, sufficient to forward to FAO.

If any of the four is absent, the production merge does not proceed.

### 3. Methodology version

faoapi declares a **methodology version** string (e.g. `faoapi-methodology/<n>`) bumped whenever a re-baselining change ships to `main`. It is surfaced alongside the served-artifact provenance (the C-86 `PredictionProvenance` lineage record / `GET /provenance/{category}`), so "which methodology produced these numbers" is auditable next to "which source/file produced them." The version is a single monotonic identifier; this ADR does not mandate semantic-version granularity.

### 4. Scope boundary

This ADR governs **changing the published numbers**. It does not govern *which artifact* is promoted (that is C-71, the quarantine/rollback gate) or *whether the input values are plausible* (C-72) or *which upstream source* produced them (C-86). Those are complementary trust-boundary controls; this one is the methodology-change control.

---

## Rationale

- **The numbers are the product.** A humanitarian consumer cannot distinguish "the world changed" from "we changed how we compute" unless we tell them. The gate forces that distinction to be explicit.
- **`development` → `main` is the natural choke point.** Production cutover is already a deliberate, reviewed step; attaching the gate there adds governance without slowing day-to-day development.
- **The diff is the honest artifact.** A before/after of published values (already produced for M1) is the most direct evidence of consumer impact — more meaningful than a code review, which sees the mechanism, not the outcome.
- **Versioning + provenance close the loop.** C-86 made *source* auditable; a methodology version makes *method* auditable. Together they answer "why are these numbers what they are" from served output alone.
- **Lightweight by design.** Four artifacts, applied only at production cutover, only for changes that actually move numbers. Refactors are untouched.

---

## Considered Alternatives

### Alternative A: No gate — rely on code review
- **Pros:** Zero process overhead.
- **Cons:** Code review sees the mechanism, not the consumer-facing magnitude; nothing communicates to FAO; nothing is versioned. This is the status quo that produced C-84.
- **Reason for rejection:** The gap this ADR exists to close.

### Alternative B: Gate every change to `data/` regardless of value impact
- **Pros:** Simplest rule (no judgment about what is "re-baselining").
- **Cons:** Taxes refactors, logging, and test changes that cannot move numbers; the friction trains people to rubber-stamp, hollowing out the gate.
- **Reason for rejection:** Over-broad; the value is in gating the changes that actually reach FAO's numbers.

### Alternative C: Full semantic methodology-versioning scheme with per-change classification
- **Pros:** Maximally precise lineage.
- **Cons:** Heavy to define and maintain for a single-maintainer service; premature.
- **Reason for rejection:** A single monotonic version + diff report captures the needed auditability now; can be elaborated if the service grows.

---

## Consequences

### Positive
- Changing published numbers is now a documented, gated, communicated act rather than an implicit side effect of a merge.
- FAO can be told what changed and when; methodology version + provenance make served numbers self-describing.
- The M1 cutover (#93) has an explicit checklist to satisfy before reaching `main`.

### Negative
- Production cutovers that re-baseline now carry a non-trivial obligation (diff + sign-off + version + comms). This is intended friction at the right boundary.
- Requires judgment to classify a change as re-baselining; §1 gives criteria but edge cases will need a call (default to gating when unsure).

---

## Implementation Notes

- **No code is mandated by this ADR to land on `development`.** The methodology-version surface (§3) should be wired to the C-86 provenance record when the M1 cutover is prepared; until then the version is carried in the diff report.
- The M1 cutover PR (#93) is the first application: it must attach the before/after diff (exists under `reports/views_frames_integration/`), a sign-off, a methodology version, and an FAO-facing note.
- This ADR is the process artifact that register C-84 asks to exist "before the M1 cutover (#93) reaches `main`."

---

## Validation & Monitoring

- The gate is enforced at PR review for `development` → `main`. A re-baselining PR without the four artifacts is not merged.
- When the methodology version is wired into `PredictionProvenance`, `GET /provenance/{category}` exposes it; a mismatch between the deployed version and the expected one is observable.

---

## Open Questions

- Where should the canonical methodology version live in code (a module constant vs. derived from a git tag)? Deferred to the #93 cutover implementation.
- Should FAO communication be a structured changelog file in-repo (e.g. `reports/methodology_changelog.md`) rather than per-PR notes? Recommended but not mandated here.

---

## References

- Risk register: **C-84** (this gap), C-81 (the M1 integration that exposed it), C-86 (provenance/source lineage — the surface a methodology version attaches to), C-72 (value plausibility), C-71 ("latest" promotion / rollback gate).
- ADR-021: Dense Grid Fill Value Semantics (a value-affecting decision this gate would cover).
- `reports/views_frames_integration/` — the M1 re-baseline before/after evidence (the diff-report pattern this ADR generalizes).
- PR #93 — the `development` → `main` M1 cutover this gate governs.
