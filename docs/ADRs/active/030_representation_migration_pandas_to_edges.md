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
| country | 29.8 s | 33.0 s | 10.7 GB | 4.5 GB |
| gaul0 | 27.9 s | 25.4 s | 10.7 GB | 4.5 GB |
| gaul1 | 59.4 s | 30.8 s | 10.9 GB | 4.7 GB |
| gaul2 | 313.7 s | 65.7 s | 13.7 GB | 6.7 GB |
| **total** | **430.8 s** | **154.9 s** | | |

(An earlier run of the same change, before the review fixes and with 12 GB free rather than
7 GB, totalled 124.3 s with gaul2 at 49.2 s. The table reports the final run — the code as
merged — rather than the better number.)

All four levels **byte-identical** over every value, index entry and column, at a
10-decimal encoding; gaul1 was re-compared at **exact float bits** (`float.hex()`, 45,488,383
bytes) to confirm the encoding was not hiding a sub-ulp difference. C-146 (cells with no code for
the level are excluded, not summed into a phantom unit) survives as a named predicate,
`has_level_code`, rather than `pd.factorize`'s `-1` sentinel.

**Verified in production, 2026-08-18 (v0.4.0).** `/data/forecast/bulk` on the deployed service:
**HTTP 200, 461,991 bytes, 25.8 s, peak 6.0 G** — measured after a restart so the peak covers
that request alone. It was 501 s, 504 at the proxy, on a service reporting `peak: 14.8G`.
`smoke.py --expect-tag v0.4.0`: ALL PASS. The byte count matches the local build (461,664), i.e.
the same table.

Worth separating, because the first reading looked like a regression: the peak immediately after
restart was **16.8 G**, which covered a **cold** load of both datasets — the historical leg's
`pd.read_parquet` of 28.4M rows (**C-169**) is a ~13 GB transient. Isolated, the bulk build is
6.0 G. The aggregate path is no longer what threatens this box; the cold historical load is.

**The endpoint (#79).** `/data/forecast/bulk` builds the full 45-column table in **31.2 s at
10.5 GB peak** with historical included (23.4 s / 4.5 GB without) — against the 300 s nginx
`proxy_read_timeout` that made it return 504. It was 501 s before C-235 and 109 s after.

**What is still on the historical leg.** Loading historical on a *cold* disk cache costs a
13.1 GB transient (`pd.read_parquet` of 28.4M rows), measured before this change. On a warm
cache — how the box actually serves — both datasets together sit at 4.4 GB, and the historical
leg's contribution to the build is the difference between the two rows above, ~6 GB. That leg is
**C-169**, §8 out-of-scope here, and it is now the larger remaining term. It did not have to be
fixed for the endpoint to come inside budget, so per the plan's own stop condition it was not.

**The historical leg stays on the pandas path.** ADR-030 §1 excludes the historical/scalar leg
from this migration and requires it to stay float64: the frame leaf accumulates in float32 and
compounds error across cells. S7's first draft called the array path unconditionally and moved a
served value on `/{level}/analysis/historical/hdi-map?aggregate=true` by 96.0 — with the full
suite green, because the guard that states this invariant is pinned to
`get_subset_dataframe(aggregate=True)`, the sibling method. The leg now dispatches to
`_aggregate_hdi_map_pandas` (the pre-S7 body, verbatim) and the invariant is pinned to both entry
points. Register **C-258**; input validation lost the same way is **C-259**.

**Ratchet (§7).** `forecast/` still imports pandas only in `ingestion/`, `serialize/`,
`geography/` — `aggregate/reduction.py` is pandas-free (`np.unique`, not `pd.factorize`).
`data/handlers/grid_dataset.py` is unchanged at 55 references; `forecast_dataset.py` went
**24 → 28**. The count went the wrong way, and that is recorded rather than explained away: the
index/assembly pandas that `_aggregate_distributions` used to own is now written out in
`calculate_hdi_map`. What changed is what pandas is *used for* — it no longer touches a single
sample. `_aggregate_distributions` and `_frame_native_joint_sum` remain for
`get_subset_dataframe(aggregate=True)`, which has a live caller (`managers/api.py:668`) that
genuinely wants a DataFrame; the two paths are deliberately allowed to duplicate.

---

## Addendum — the historical cold-start load (2026-08-18, C-263 / #98)

The S7 addendum above closed with a measurement and an open question: *"The aggregate path is no
longer what threatens this box; the cold historical load is."* This addendum answers it.

**What the cold start actually cost, and why.** The historical leg decoded the whole artifact
into one pandas frame and handed that frame to `ForecastDataset`, which copied out of it. The two
are resident **at the same time**, so the peak is the sum — and no amount of freeing afterwards
lowers a peak that has already happened. Measured on the real artifact
(`historical_dataset_20260814_203554.parquet`, 28,421,738 rows):

| rows | in-memory path, peak above baseline | streamed |
|---|---|---|
| 3,884,520 (60 months) | 1.307 GB | 0.354 GB |
| 7,769,040 (120 months) | 2.575 GB | 0.274 GB |
| 28,421,738 (all 439 months) | **12.205 GB** | **3.940 GB** |

Both full-scale figures are measured, on the same host in the same state.

**Correction, 2026-08-21.** The first version of this table gave the full-scale in-memory figure
as "~9.4 GB *(extrapolated)*", linearly from the 120-month row, because the development host had
only 9.6 GB free at the time and could not run it. With 19 GB free it was measured: **12.205 GB**.
The linear extrapolation understated the real peak by 30% — the in-memory path grows *super*
linearly, so the saving is 8.27 GB and the ratio 3.1x, not the 2.4x first claimed. Recorded as a
correction rather than silently improved, because the error was in the direction that flattered
the change, and a reader who checked the arithmetic against the 120-month row would not reproduce
it.

The streamed column is near-flat in row count because the value-dir is file-backed and read back
memory-mapped; the in-memory column is not.

**What streaming costs.** It is *slower*: 36.5 s against 26.5 s for the whole ingest at full
scale, ~38% more wall time, because the loader makes two passes — one to build the category
vocabulary, one for the data. That is the trade this change makes deliberately: ~10 s once per
restart, against 8.27 GB of peak on a box that has already been OOM-killed three times. At
smaller scales streaming measured *faster*, which is why the trade is stated at full scale rather
than from the 120-month row.

**Three hypotheses the measurement killed.** Recorded because each looked obvious and each was
wrong, and #98 listed two of them as candidate directions:

1. *"The four object-dtype geography columns cost ~6-7 GB."* They do not. That figure came from
   `memory_usage(deep=True)`, which sums `sys.getsizeof` per element and therefore counts each
   **shared** string once per row; Arrow's `to_pandas` interns repeated values. Reading them
   dictionary-encoded instead moves peak RSS by ~0.7 GB, not ~6 GB. The claim had been sitting in
   a load-bearing comment in `dataset_service.py`; it is now corrected there.
2. *"Release the source frame and the retained file bytes."* Both were real defects — `del
   file_bytes` was a no-op because `_download_file_bytes` had stored the same object in
   `_file_cache`, and the decoded frame stayed alive through `to_value()`. Fixing both frees ~0.25 GB
   at 60-month scale and **moves the peak by nothing**, because the peak occurs during construction,
   before either line runs.
3. *"Elide the redundant copies."* `to_array_columns` copied unconditionally for an artifact with
   no list columns, `sort_index()` copied an already-sorted index, and `geo_metadata.reindex()`
   copied against an identical index. All three are genuine waste and all three are now elided.
   They too leave the peak unchanged.

**Corrected 2026-08-21: the wall-time figure first published for (3) was wrong.** It was given as
"~15-20%", blended from end-to-end runs that also contained the (2) changes and had visible
run-to-run variance. Measured properly — 5 repetitions each, 3,884,520 real rows, median:

| | before | after | |
|---|---|---|---|
| `fill_dense_grid` (isolated, already-dense input) | 0.332 s | 0.234 s | 1.42x |
| `ForecastDataset(...)` construction | 0.971 s | 0.884 s | **1.10x** |
| `validate_metadata_plausibility` | **2.384 s** | **0.018 s** | **135x** |

So the copy elisions are worth ~9%, not 15-20%. What the blended figure was actually measuring,
without knowing it, was the fourth change: `assert_geo_metadata_plausible` called `.astype(str)`
on a **categorical** column, expanding 3.9M rows into Python strings to validate a set of ~200
distinct codes. Checking `.cat.categories` instead is 135x faster on that call and saves ~17 s at
full scale — which makes it the largest wall-time win in this change, and it had been recorded as
an afterthought. `fill_dense_grid`'s fast path is real but small, and now applies only to the
fallback and wire paths, since the streamed loader never calls it.

**What was done.** `forecast/ingestion/historical_stream.py` assembles the value-dir a parquet row
group at a time: each target's `(N,1)` **float64** block is written into a preallocated
`np.lib.format.open_memmap`, geography is appended to an open `ParquetWriter`, and the result is
adopted with the same `write_value_dir` the wire path uses and read back mmap'd. It is structurally
the `WireRunAssembler` pattern (S6b-2 / #208) applied to the leg that did not get it.

**Why this is not the §1 float32 hazard.** §1 keeps this leg float64 because the frame leaf
accumulates in float32 and compounds error across cells (C-258, caught after a green suite). The
streamed path performs **no arithmetic at all** — it moves bytes from arrow to a float64 memmap. It
changes where assembly happens, not what is assembled.

**The precondition, and why it is checked rather than assumed.** Streaming in file order is only
equivalent if file order already *is* `sort_index()` order and the grid is already dense —
otherwise the constructor's sort and dense-fill are real work. The producer's artifact satisfies
both (439 months x 64,742 cells, month-ascending, cell-ascending within each month, the identical
cell vector every month), but `stream_to_value` verifies it per row group and raises
`NotStreamable` on any violation, and the service falls back to the unchanged in-memory path. A
wrong row order here would move served numbers with no error anywhere, so it is the one thing not
taken on trust.

**Byte-identity, verified the way S7 was.** Against the in-memory path on the real artifact:
manifest, index, and every float64 feature block identical **byte for byte**; the geo table
identical including categorical dtype and **category order** (which fixes served group order under
`groupby(observed=True)`). On 1,553,808 real rows the served outputs match exactly —
`calculate_hdi_map(aggregate=True, level="country")` (the C-258 path, 4,848 x 36, all float64),
`get_subset_dataframe(aggregate=True, level="gaul1")`, the bulk `s_actual` column
(`_actual_by_admin1`), and a cell-level subset with metadata (129,484 x 12).

**What this leaves unproven.** The production cold-start figure. The before/after in #98 requires
`systemctl restart` on the shared box, which was out of scope for the session that made the change;
the local measurement predicts roughly 16.8 G → ~11 G, and that prediction is what needs checking,
not assuming. Until it is checked, **#99's memory ceilings remain blocked** — on a measurement now,
not on a code change.

**§8 scope, flagged not resolved.** §8 places the historical/scalar leg outside this migration.
Nothing here changes its representation, so the scoping is not violated in substance. But §8 was
written before the cold-start measurement existed, and the leg has now acquired a streaming
assembler that looks a great deal like the one §5/§6 built for the forecast leg. Whether §8 should
be amended is a governance question for the operator, not something this change decides.
