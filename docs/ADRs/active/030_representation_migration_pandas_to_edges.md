# ADR-030: Representation Migration — pandas to the Edges

**Status:** Accepted
**Date:** 2026-06-27
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** views-frames ADR-018 (frozen frame API), faoapi ADR-023 (re-baselining governance), ADR-008/009 (fail-loud / boundary validation); register C-136, C-146–C-149, D-12, D-14, D-15, D-16; epic #154, issue #112
**Informed:** maintainers

---

## Context

faoapi's **canonical internal store** is an object-dtype `pandas.DataFrame` whose `pred_*` cells each hold an `(S,)` array of draws (`data/handlers.py:91` → `_convert_to_arrays`). At grid scale (≈10.5M cells × ~1024 draws) this does not scale — per-cell Python objects, no vectorization, large memory — and it inverts the design: the least-stable, most-concrete thing is the spine everything depends on (SDP/SAP/DIP backwards). The views-frames epic built the destination (`forecast/` package, frozen `views_frames.PredictionFrame (N,S)`, ADR-018) but did not flip the store.

The expert review (epic #154) raised that a naive flip could **silently re-baseline** FAO-facing numbers (C-136/C-A) if float64→float32, and that the parity net was synthetic (C-146). This ADR is the S1 tracer outcome: it pins, with evidence, **what is safe and what is gated**, and governs the migration.

---

## Decision

### 1. The canonical store for the forecast/sample path becomes `PredictionFrame (N,S)`; pandas lives only at the two seams

The `(N,S)` `float32` frame is the canonical in-memory representation. Object-dtype pandas exists only transiently in `forecast/ingestion` (decode the wire), and a scalar DataFrame only in `forecast/serialize` (emit JSON). The **historical/scalar path stays pandas** for now (point data, far less dangerous) — out of scope (WET before DRY; CRP).

### 2. Representation flip and collapse-precision are SEPARATE concerns (the central scope boundary)

Investigated 2026-06-27 and locked as a permanent audit (`tests/forecast/test_representation_parity.py`):
- The stored cells are **already `float32`**; the served collapse upcasts to **`float64`** (`to_tensor` → `tower_collapse`).
- **Making the frame the canonical store while keeping the legacy float64 collapse is BYTE-IDENTICAL** — proven across an even/odd-S synthetic corpus. The store flip (S4) is therefore a behaviour-preserving refactor, **not** an ADR-023 re-baseline.
- The **float32-native collapse** is the *only* numeric change, and it is the existing **#112 / Phase 4b**, ADR-023-gated — **out of this epic**. Measured deltas:
  - **Per-cell MAP/HDI: 0** (the tower is *order-statistic* based — HDI bounds are actual sample values; MAP is a median).
  - **Aggregation (cross-cell joint-sum): ≈1e-5** — arithmetic, real, **sub-count**, but **invisible** to the `approx(1e-4)` golden (register C-136). This is the #112 surface; its ADR-023 diff is now evidence-based, not guessed.

### 3. Ragged-S is rejected, fail-loud

A `PredictionFrame` is rectangular `(N,S)`; cells with differing draw counts cannot fit. The pipeline **rejects** them at the ingestion boundary (today `np.stack` already raises; an explicit guard message is added in S4). Uniform per-artifact `S` is required; ragged draws are a producer-side concern (pad upstream), not a faoapi accommodation (ADR-009 fail-loud). Test: `test_ragged_S_is_rejected_fail_loud`.

### 4. The disk cache persists the frame VALUE, not a pickled object

Per D-14/C-149: the cache stores arrow/npz of the frame arrays + geo table + index + `file_id` + provenance (S5), removing the `CACHE_SCHEMA_VERSION`-from-class-signature churn (C-138) and the `pickle.load` surface (C-149) in one move, and pre-staging the #100 mmap path.

### 5. `.dataframe` is sealed (loud/forbidden for sample columns)

Per D-12/C-148: `.dataframe` no longer materializes object-dtype sample cells; reconstructing them is forbidden, not loud-and-bounded (a reconstruct-on-demand property would double memory at peak — C-148).

**Realized by the S4d hard drop (2026-06-28), not by a sealing property (S6 #160).** The expert review chose the hard drop over a "derived view" (D-19; derived-view = the rejected path-b, D-20). So the sample columns are simply **absent** from `.dataframe`: reaching `dataset.dataframe["pred_*"]` raises a `KeyError` (the seal) and there is no reconstruction path to resurrect them. The canonical, named access is `ForecastDataset.samples(var)` / `_sample_array` (C-153). D-12 is resolved toward sealing. A property that intercepts `df["pred"]` with a friendlier message was considered and **declined** (`.dataframe` is a plain attribute; intercepting item-access would require a `DataFrame` subclass — disproportionate); the breadcrumb lives in the `samples()` docstring instead. Pinned by `tests/forecast/test_dataframe_seal.py`.

### 6. The slice plan, resting states, and the declared stop-point

Tracer-first (D-15). Slices: **S1** tracer+this ADR · **S2** parity corpus + ratchet CI gate + memory/pickle tests · **S3** read-chokepoint · **S4** store flip (byte-identical) · **S5** value-cache · **S6** seal `.dataframe` · **S7** tensor-native subset · **S8** retire the `_ViewsDataset → _PGDataset` chain (terminal). Every slice leaves a coherent, shippable-and-stoppable resting state; the **declared stop-point is after S4** (forecast store flipped, still byte-identical) — a safe place to pause if upstream gating (#100) or other work intervenes (D-16). The **terminal milestone** is S8 (chain deleted, ratchet green) so adoption is not left without retirement (C-147/C-83).

### 7. The `grep pandas` ratchet is the acceptance signal

`grep -rl 'import pandas' src/views_crafdapi/forecast/` may match only `ingestion/`, `serialize/`, `geography/`; `summarize/`, `aggregate/`, `frames/`, `conformance` must stay pandas-free. `data/handlers.py`'s pandas footprint is on a shrinking budget to its target. Enforced as a CI gate in S2.

### 8. Scope

Governs the **forecast/sample internal representation**. Out of scope: the float32-native collapse (#112), the historical/scalar path, the wire format (#100, upstream-gated), and the FAO-facing schema (ADR-024/025). The boundary contract (DataFrame-in, JSON-out, per case) is unchanged.

---

## Rationale

- **Same arrow fixes both goals.** The scalability hazard *is* the SDP/SAP/DIP inversion; making the frozen frame the canonical store fixes both, and the destination abstraction is already designed and frozen (ADR-018) — no new, unstable abstraction is invented.
- **Evidence over assertion.** The byte-identical claim is proven on a corpus, and the only numeric change is measured and isolated to a gated issue — so the migration cannot silently re-baseline FAO numbers (answers C-136/C-A/D-B).
- **Order statistics are the reason per-cell collapse is float-stable.** This is *why* the bulk of the migration is free; the arithmetic surfaces (aggregation, future severe_scenario) are the only places dtype matters, and they are governed separately.

---

## Considered Alternatives

- **Big-bang store + collapse flip.** Rejected — bundles a behaviour-preserving refactor with an ADR-023 re-baseline, defeating bisection (register C-139) and risking silent re-baseline.
- **Pad ragged-S to max-S with NaN.** Rejected — hides a producer defect and inflates memory; fail-loud is the ADR-009 stance.
- **Keep pickled-object cache.** Rejected for the migration — carries C-138 churn + C-149 pickle surface; value-persistence removes both.

---

## Consequences

### Positive
- Scalability hazard removed (no `(N,S)` draws in pandas cells); principle alignment (frozen frame as the stable, abstract spine); unblocks #100 / #112.
- The migration is provably byte-identical at every slice; the #112 delta is recorded evidence.

### Negative / trade-offs
- **Mid-migration is more complex** (two representations transiently) — mitigated by coherent resting states + the stop-point (C-147/D-16).
- The aggregation float32 delta (~1e-5) is real and golden-invisible — it surfaces only when #112 flips, under ADR-023 (not this epic).
- The historical/scalar path stays pandas — accepted (WET); revisit only if a shared shape emerges.

---

## Implementation Notes

- Tracer + audit: `tests/forecast/test_representation_parity.py` (synthetic corpus; permanent).
- Estimator: `forecast/summarize/estimator.tower_collapse`; frame builder: `ForecastDataset.to_frames` (`data/handlers.py:631`).
- The #112 ADR-023 diff should reuse the aggregation measurement here against a real cached posterior when one exists.

---

## Open Questions

- The exact `data/handlers.py` pandas-footprint target for the ratchet (zero vs the serialize edge) — fixed in S2.
- Whether a real production artifact can be captured as a golden once the un_crafd producer delivers one (would strengthen C-146 beyond synthetic).

---

## References

- Epic **#154**; issue **#112** (the gated float32 collapse); views-frames **ADR-018** (frozen frame); faoapi **ADR-023** (re-baselining), **ADR-008/009** (fail-loud / boundary).
- Register: **C-136** (silent re-baseline / golden too loose), **C-146** (parity corpus), **C-147** (swamp), **C-148** (`.dataframe` memory), **C-149** (pickle persistence), **D-12/D-14/D-15/D-16**.
- Audit: `tests/forecast/test_representation_parity.py`.

---

## Closeout (2026-06-28)

Epic #154 is **complete**, byte-identical throughout:

- **S1–S3** tracer + parity corpus + `grep pandas` ratchet + read-chokepoint.
- **S4 (keystone, the declared stop-point)** the canonical store is the `(N,S)` `PredictionFrame`; consumers rerouted per option-(c) (S4a tensor, S4b aggregation, S4c subset), then the object-dtype `pred_*` cells **dropped** (S4d hard-drop, D-19/D-20).
- **S5** disk cache persists the frame **value** (`to_value`/`from_value`, `views_frames.io.npz` + parquet); no `pickle.load` (C-149), version decoupled from class identity (C-138).
- **S6** `.dataframe` **sealed** for sample columns — realized by the S4d hard-drop, not a reconstructing property (D-12).
- **S8 (terminal)** the chain was **rationalized, not deleted** (register **D-21**): `_PGDataset` retired; `_ViewsDataset` renamed **`_GridDataset`** and kept as the generic geo-less base; a full single-class merge was rejected (would complect responsibilities + relocate the geo fail-loud, C-156/C-157). The C-147 swamp — the object-dtype spine — was drained at S4; the "delete the chain" AC was an over-specified *means* and is amended here.

**Gated / not in this epic:** the float32-native collapse (**#112 / 4b**, ADR-023) — this epic kept the legacy float64 collapse so every slice stayed byte-identical, and measured the 4b delta for the ADR-023 diff. **Out of scope (retained pandas):** the historical/scalar feature path (ADR-030 §1); `data/handlers.py` stays on the ratchet allowlist as the DataFrame-returning serving boundary.

---

## Addendum — S7 for the aggregate path (2026-08-18)

The 2026-06-28 closeout lists S1–S6 and S8. **S7 (tensor-native subset) is absent from it**:
the epic closed with the store flipped but the aggregate read path still going through pandas.
That gap is what register **C-235** and issue **#79** turned out to be.

`calculate_hdi_map(aggregate=True)` made a four-step round trip around a store that was already
contiguous — `get_subset_dataframe` exploded `(N, S)` into one ndarray object per row per target
(`pd.Series(list(arr), dtype=object)`), `_stack_cells` put them back for the views-frames leaf,
the group sums were scattered into object cells again, and the reduction stacked them a third
time. The DataFrame carried no information across those steps; it carried only itself.

**S7 as landed.** The reduction is `forecast/aggregate/reduction.py::joint_sum_to_level` — one
pandas-free function, streamed per month by `calculate_hdi_map` exactly as the cell path streams
(S6b-1 / #208). Groups are `(time, unit)` and a month is one time value, so no group spans two
months: per-month reduction sums the same draws in the same order as reducing every month at
once. Pandas keeps two jobs on this path, both at seams §1 already allows — the group index and
metadata (`groupby(observed=True)`, which fixes row order and so the served order) and the final
frame assembled from stacked arrays.

Measured against the delivered run (`rusty_bucket_forecasting_20260727_095355`, 2.33M cells,
S=128, 36 months), full month range, all four aggregate levels:

| level | wall before | wall after | peak RSS before | peak RSS after |
|---|---|---|---|---|
| country | 29.8 s | 23.2 s | 10.7 GB | 4.5 GB |
| gaul0 | 27.9 s | 23.7 s | 10.7 GB | 4.6 GB |
| gaul1 | 59.4 s | 28.2 s | 10.9 GB | 4.7 GB |
| gaul2 | 313.7 s | 49.2 s | 13.7 GB | 6.6 GB |
| **total** | **430.8 s** | **124.3 s** | | |

All four levels **byte-identical** over every value, index entry and column, at a
10-decimal encoding; gaul1 was re-compared at **exact float bits** (`float.hex()`, 45,488,383
bytes) to confirm the encoding was not hiding a sub-ulp difference. C-146 (cells with no code for
the level are excluded, not summed into a phantom unit) survives as a named predicate,
`has_level_code`, rather than `pd.factorize`'s `-1` sentinel.

**The endpoint (#79).** `/data/forecast/bulk` builds the full 45-column table in **31.2 s at
10.5 GB peak** with historical included (23.4 s / 4.5 GB without) — against the 300 s nginx
`proxy_read_timeout` that made it return 504. It was 501 s before C-235 and 109 s after.

**What is still on the historical leg.** Loading historical on a *cold* disk cache costs a
13.1 GB transient (`pd.read_parquet` of 28.4M rows), measured before this change. On a warm
cache — how the box actually serves — both datasets together sit at 4.4 GB, and the historical
leg's contribution to the build is the difference between the two rows above, ~6 GB. That leg is
**C-169**, §8 out-of-scope here, and it is now the larger remaining term. It did not have to be
fixed for the endpoint to come inside budget, so per the plan's own stop condition it was not.

**Ratchet (§7).** `forecast/` still imports pandas only in `ingestion/`, `serialize/`,
`geography/` — `aggregate/reduction.py` is pandas-free (`np.unique`, not `pd.factorize`).
`data/handlers/grid_dataset.py` is unchanged at 55 references; `forecast_dataset.py` went
**24 → 28**. The count went the wrong way, and that is recorded rather than explained away: the
index/assembly pandas that `_aggregate_distributions` used to own is now written out in
`calculate_hdi_map`. What changed is what pandas is *used for* — it no longer touches a single
sample. `_aggregate_distributions` and `_frame_native_joint_sum` remain for
`get_subset_dataframe(aggregate=True)`, which has a live caller (`managers/api.py:668`) that
genuinely wants a DataFrame; the two paths are deliberately allowed to duplicate.
