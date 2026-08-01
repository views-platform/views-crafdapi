# ADR-024: faoapi Serves Forecasts in Raw Count Space

**Status:** Accepted
**Date:** 2026-06-26
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** views-pipeline-core ADR-055 (Raw-Space Model I/O Contract), views-models ADR-012 (Target Scale and Prefix Convention), views-hydranet ADR-063 (Regression-head output activation), ADR-003 (Authority of Declarations over Inference), faoapi ADR-023 (re-baselining governance), register C-72 / C-81
**Informed:** UN CRAF'd API consumers

---

## Context

faoapi is the **terminal consumer** in the VIEWS forecasting chain: it reads a forecast artifact from Appwrite (produced upstream by views-postprocessing `un_crafd`), computes MAP / HDI / point + interval summaries, and serves them to the UN FAO — as JSON via the analysis endpoints and (planned) as a bulk parquet. The numbers FAO consumes are humanitarian-planning inputs: they must be in **raw fatality counts**, not a log-compressed or otherwise transformed scale.

The platform has now **ratified the numerical-scale contract upstream**, and faoapi must declare its position as the consuming end of it:

- **views-pipeline-core ADR-055 — Raw-Space Model I/O Contract** (`views-pipeline-core/documentation/ADRs/055_raw_space_model_io_contract.md`): models consume and return predictions in **raw target space** (actual counts). Any training-time transform (log1p, asinh, …) is **model-internal, config-declared, and inverted before predictions leave the model** (Clauses 1–2). **Pipeline-core and downstream consumers do not undo model transforms** (Clause 4). The `ln_`/`lx_`/`lr_` column-name prefix scheme is **deprecated as a scale signal** — *"a column named `ln_ged_sb` is not evidence that the values are in log-space"* (Clause 5); scale is determined by config declaration, never inferred from a name (ADR-003).
- **views-models ADR-012 — Target Scale and Prefix Convention** (`views-models/docs/ADRs/012_target_scale_and_prefix_convention.md`): `lr_` means **linear / original measurement scale** (an *identity* convention, not "a transform was applied"); `ln_`/`lx_` are deprecated; **every prediction leaves views-models on its measurement scale**, per target (`sb`/`ns`/`os`).
- **views-hydranet ADR-063 — Regression-head output activation** (`views-hydranet/docs/ADRs/active/063_regression_head_output_activation.md`): the per-target regression heads emit through softplus/ReLU, guaranteeing **non-negative raw-count outputs** at the model boundary.

So by the time a forecast reaches faoapi, it is — by upstream contract — **raw, non-negative counts**, one series per violence type (`sb`/`ns`/`os`). faoapi has never been, and must never become, a place where target transforms are applied or inverted.

A decision is needed now because: (a) faoapi's own code and docs still carry the legacy `pred_ln_*` names (e.g. `pred_ln_sb_best` in `data/README.md`), which ADR-055/012 explicitly say are **not** a scale signal; (b) the views-frames adoption (epic #87) and the planned FAO bulk-parquet make the served-value scale an explicit, FAO-facing contract that must be pinned; and (c) the estimator path must collapse posterior samples in the **same space the data arrives in** — raw — and must not wrap a log/exp around the collapse.

---

## Decision

### 1. faoapi serves raw counts and neither applies nor inverts target transforms

Every served value — the tower MAP, the HDI bounds, the low/high estimates, and the historical actuals — is in **raw count space** (events), the same scale in which the upstream artifact's posterior samples arrive. faoapi is a **downstream consumer** under ADR-055 Clause 4: it does **not** apply `log`/`log1p`, does **not** apply `exp`/`expm1`, and does **not** otherwise transform target values. Inversion, if any was ever applied, is the upstream model library's sole responsibility and has already happened before the artifact is written.

### 2. Scale is never inferred from a column-name prefix

faoapi must not read `pred_ln_*`, `pred_lr_*`, or any prefix as evidence of numerical scale (ADR-055 Clause 5; ADR-003). A `pred_ln_sb_best` column is **not** evidence the values are log-space. The prefix is part of the column's *identity* only. faoapi already keys on the `pred_` prefix for identity (detecting prediction columns) — that is unaffected; what is prohibited is treating `ln_`/`lr_` as a scale signal or a trigger to transform.

### 3. The estimator collapses in raw space

The tower estimator (`forecast/summarize/estimator.py`) operates directly on the raw-count samples. MAP (`tower_point`), HDI (`hdi_tower`), and any quantile/low/high are **monotonic order statistics**, so they are computed in raw space and stay in raw space — there is **no** `log → collapse → exp` round-trip (which would change the published numbers and is forbidden). Cross-level aggregation (the joint-sum, #90) likewise sums raw counts.

### 4. faoapi guards the contract at ingestion (trust-but-verify)

faoapi's correctness depends on the producer (`un_crafd`) actually delivering raw counts. Because the prefix is not authoritative and the upstream migration to raw is in flight, faoapi treats raw-scale as a **checked precondition**, not an assumption. The existing value-plausibility gate (register **C-72**, `forecast/ingestion/plausibility.py`) already enforces **finite + non-negative** — consistent with ADR-063's non-negative guarantee. This ADR additionally authorizes a **raw-scale sanity check** (a fail-loud guard that the served distribution looks like counts, e.g. its magnitude is not compressed into a log-like `[0, ~7]` band on active cells), mirroring the views-models transform-undo verification (views-models issue #72). A violation fails loud (HTTP 500 on ingestion), never silently serves a log-scale number to FAO.

### 5. Scope

This ADR governs the **target/prediction values** served to FAO. It does not govern feature handling, the wire format of the artifact (parquet vs views-frames arrow — a separate ADR), or the choice of which summaries to emit (HDI masses, low/high definitions — a separate schema decision).

---

## Rationale

- **The contract is already raw upstream; faoapi must not re-introduce a transform boundary.** ADR-055 eliminated cross-boundary scale ambiguity by making the model own its transforms. A consumer that logs/unlogs would re-create exactly the ambiguity (and the silent-corruption modes) ADR-055 exists to remove.
- **Names lie; declarations don't.** faoapi's `pred_ln_*` names are legacy identity, not truth (ADR-055 Clause 5, ADR-012). Depending on them for scale is the ADR-003 anti-pattern.
- **Order statistics are scale-faithful — so don't move the scale.** Collapsing in raw space and serving raw is both correct and simplest; a log/exp wrapper around the tower would shift the published MAP/HDI for no benefit.
- **Trust-but-verify protects FAO.** The upstream guarantee is real but the migration is in flight and the prefix is unreliable; a cheap fail-loud guard converts "silently served log values" into a loud ingestion error.

---

## Considered Alternatives

### Alternative A: faoapi inverts a log transform before serving
- **Pros:** would produce raw counts even if the artifact were log-scale.
- **Cons:** **violates ADR-055 Clause 4** (consumers do not invert); needs to *know* the transform, which is exactly what the deprecated prefix cannot reliably tell it; re-introduces silent-corruption modes (wrong offset, double-undo). Re-creates the boundary the platform just removed.
- **Rejected.**

### Alternative B: assume raw, no guard
- **Pros:** simplest; matches the upstream contract literally.
- **Cons:** the producer migration to raw is in flight and the prefix is not authoritative, so a regression upstream would silently ship log-scale numbers to a UN agency with no signal.
- **Rejected** in favour of trust-but-verify (Decision 4).

### Alternative C: infer scale from value ranges at serve time
- **Pros:** no upstream dependency.
- **Cons:** inference-from-values is precisely what ADR-003 / ADR-055 Clause 3 forbid as a source of truth; brittle (a genuinely low-violence month looks "log-like").
- **Rejected** as an *authority*; permitted only as a fail-loud *sanity guard* (Decision 4), not as a transform trigger.

---

## Consequences

### Positive
- The FAO-facing scale is now an explicit, ratified contract aligned with the platform (ADR-055/012/063).
- No transform logic enters faoapi — the consumer boundary stays clean and the silent-corruption modes ADR-055 names cannot appear here.
- The raw-scale guard makes an upstream scale regression a loud ingestion failure, not a silent UN-facing error.

### Negative / trade-offs
- faoapi's correctness is explicitly coupled to the producer honouring ADR-055 (un_crafd must emit raw). This coupling is real and now documented (a checked precondition rather than a hidden assumption).
- The legacy `pred_ln_*` names remain in data/examples until upstream renames to `lr_` (ADR-012); this ADR makes clear they are identity-only, but the cosmetic mismatch persists until the producer migrates.

---

## Implementation Notes

- No transform code is added or removed by this ADR — its primary effect is **prohibitive** (no log/exp in faoapi) and **documentary**.
- The raw-scale sanity guard (Decision 4) extends `forecast/ingestion/plausibility.py` (the C-72 home) — a fail-loud check that active-cell magnitudes are not log-compressed; wire it alongside `assert_prediction_samples_plausible` at the ingestion boundary (`managers/api.py _get_latest_dataframe`). Tune the threshold against real cached posteriors before enabling as hard-fail.
- CIC note: `forecast/summarize/estimator.py` and the (future) `forecast/serialize/json_contract.py` should state explicitly that inputs and outputs are raw counts.

---

## Validation & Monitoring

- The provenance/methodology surface (ADR-023, C-86) already records the served methodology; this ADR's scale contract is checked at ingestion by the plausibility gate.
- A characterization/golden test should include at least one active (non-zero) cell whose served MAP is in a count-plausible range (not a `[0, ~7]` log band), mirroring views-models `test_pfe_production_readiness.py::TestTransformUndoScale` (issue #72).

---

## Open Questions

- The exact magnitude threshold / rule for the raw-scale guard (Decision 4) — to be calibrated on real cached forecasts before it is made hard-fail.
- Whether the served column names should be renamed `pred_ln_* → pred_lr_*` to match ADR-012 once the producer migrates (cosmetic; tracked with the wire-format ADR).

---

## References

- views-pipeline-core **ADR-055** — Raw-Space Model I/O Contract (`views-pipeline-core/documentation/ADRs/055_raw_space_model_io_contract.md`)
- views-models **ADR-012** — Target Scale and Prefix Convention (`views-models/docs/ADRs/012_target_scale_and_prefix_convention.md`)
- views-hydranet **ADR-063** — Regression-head output activation (softplus/ReLU → non-negative raw outputs) (`views-hydranet/docs/ADRs/active/063_regression_head_output_activation.md`)
- faoapi **ADR-003** — Authority of Declarations over Inference
- faoapi **ADR-023** — Governance Gate for Re-baselining Published Forecasts
- Risk register: **C-72** (value/metadata plausibility gate), **C-81** (views-frames adoption), **C-86** (provenance/methodology surface)
- Upstream verification pattern: views-models issue **#72** (`TestTransformUndoScale` — predictions in original scale, not log-compressed)
