# ADR-037: Run Identity and the Live-Run Pointer

**Status:** Proposed — awaiting maintainer ratification
**Date:** 2026-08-26
**Deciders:** Simon (maintainer)
**Consulted:** expert-code-review panel (2026-08-26), Kleppmann/Hickey/Feathers seats in particular
**Informed:** views-postprocessing (the producer), views-pipeline-core (the CLI prohibition), views-models

---

## Context

**What a UN partner is served is decided by whichever artifact was uploaded most recently, and by
nothing else.** Upload a second forecast to `crafd_bucket` — a calibration experiment, a re-run, a
test — and it becomes the live CRAF'd forecast, unconditionally, behind a valid `200` with
correct-looking provenance. There is no confirmation step and no way to express an intended run. The
only existing control is a comma-separated list of file ids in an environment variable.

That is the harm this ADR answers. It is present today, and it does not depend on anything new being
asked for.

**The mechanism.** `get_latest_manifest()` (`managers/prediction/manager.py:298-311`) takes no
arguments and returns `docs[0]` after sorting every matching document by `$createdAt` descending, in
Python. In other words: whichever artifact arrived most recently wins, and nothing anywhere records that a
choice was made. That is standing in for a selection policy nobody ever wrote.

It cannot be scoped, because there is nothing to scope it by: no queryable run identifier exists.
`run_id` lives only *inside* the manifest — the small JSON file the producer uploads last to mark a
run complete — so learning which run a stored document belongs to means downloading it first. Shard resolution inherits
the same gap — `resolve_artifact_file_ids` (`manager.py:313-332`) scans every shard document of
every run ever uploaded, unscoped, and matches by filename in Python.

### Why this is now urgent

The specification has gained a requirement: **store and retrieve forecasts from the calibration and
validation partitions**, not only production. That changes the product from *"the current forecast"*
into *"an addressable archive of runs"*, and an archive whose entries cannot be addressed by
identity is not an archive.

The volume is not the problem. The identity is.

| | predicted month-slices |
|---|---|
| forecasting | 36 |
| calibration (months 457–504) | **468** — 13 rolling-origin sequences × 36 steps |
| validation (months 505–552) | **468** |
| **total** | **972** |

Those 972 slices are 2,916 files as the producer writes them today (972 × 3 targets). What that
costs depends entirely on how it is held:

| | bytes | derivation |
|---|---|---|
| as the producer writes them now | **2,667,756,249** | 2,916 × 914,868, the measured mean shard size |
| repacked, keeping only the value column | **49,002,867** | 2,916 × 16,805, the measured mean value bytes per shard |
| **loaded into memory as `(N,S)` float32** | **96,659,288,064** | 3 × 972 × 64,742 × 128 × 4 |

The last row is the one that decides the architecture, and the box has 22 GiB. All three were
computed on 2026-08-26 from the measured shard sizes of the delivered run — the mean shard is
914,868 bytes over 108 shards, and one shard holds 64,742 × 128 = 8,286,976 rows, which was checked
against the parquet metadata rather than assumed.

**One assumption is load-bearing and is not measured:** that calibration and validation runs cover
the same 64,742-cell grid at the same 128 draws as the forecasting run. That is what the partition
definitions imply, but no calibration artifact exists to check it against. If either differs, all
three figures scale with it.

**Rolling origin is the sharp edge.** The same `(month, cell)` appears up to **13 times** at
different lead times. Any address phrased as *"the forecast for month M"* is ambiguous unless lead
time is part of it — and comparing forecasts made at different horizons is precisely the error that
validation exists to detect.

### What blocks this, and it is not here

- **The wire contract carries no `run_type`** — not in the manifest, not in the shard header, not in
  the store schema. It is smuggled inside the `run_id` string, and views-pipeline-core already
  resorts to `if "forecasting" in fc` string-matching.
- **`category` is a closed enum** `{forecast, historical}`, enforced by a raise
  (`prediction/metadata.py:74-75`) **and** a golden test. A partition label cannot be a category.
- **crafdapi does not upload.** views-postprocessing does. Any stamp is a cross-repo amendment.
- **views-pipeline-core's CLI actively forbids what is being asked for:** `--prediction_store`
  requires `--forecast`, which requires `--run_type forecasting`; and `--evaluate` is refused for
  forecasting. *"The two halves never meet in a store."* Calibration and validation predictions land
  in a gitignored directory on whichever laptop ran the model.
- **Nothing in the platform loads a stored prediction and scores it against actuals.** Evaluation
  exists only as an in-run side effect.

---

## Decision

**Run identity becomes explicit, and "which run is live" becomes a separate, named thing.**

1. **Artifacts are addressed by run, and the address is ours to set.** ADR-036's built artifacts
   are produced by this repository, not by the producer, so their layout needs no cross-repo
   agreement. They are written under `runs/{run_id}/{category}_{level}.parquet`, where `run_id` is
   the value already carried in the run manifest. Once written, an artifact is never rewritten in
   place; a corrected run is a new `run_id`.

   This is separate from the *wire* shard naming, which views-postprocessing owns and which this
   ADR does not touch.

2. **Lead time goes in the path when partitions arrive.** Production forecasting runs have one
   sequence, so today's address needs no sequence component. Calibration and validation have
   thirteen, and for those the address becomes
   `runs/{run_id}/seq{NN}/{category}_{level}.parquet`, `NN` zero-padded, matching the producer's
   existing `predictions_{run_type}_{timestamp}_{NN}.parquet` convention.

   **It goes in the path, not only in a catalogue**, because a catalogue carrying identity the bytes
   do not is a second source of truth that can drift from what it describes.

   This item is **not buildable yet** — it needs `run_type` on the wire, which is upstream. It is
   stated now so the production layout does not have to change when partitions land.

3. **One explicit live-run pointer**, replacing "whichever arrived last". It is the *only* mutable
   thing in the delivery; everything else is an immutable value.

   **What it must satisfy** — these are testable and are the decided part:
   - it names exactly one `run_id`, and serving reads it rather than sorting by upload time;
   - it is readable without downloading a run;
   - it is shared by every worker, so two workers cannot serve different runs;
   - it survives a restart and a redeploy;
   - changing it is a deliberate act with a recorded actor and timestamp, not a side effect of an
     upload;
   - if it is absent or names a run that is not present, serving **refuses** rather than falling
     back to newest — which is the ADR-033 guarantee, now carried by the pointer instead of by the
     cache-identity comparison.

   **Where it lives, and who writes it.** Ratified by the maintainer 2026-08-26.

   The pointer is a document in the **CRAF'd bucket's own metadata collection** — the collection
   this service already queries for file records. No new configuration in this repository, and none
   in views-models or views-postprocessing. Chosen over a file in the bucket (needs its own read
   path) and over a deployment config value (invisible at runtime, lost on redeploy). The cost is
   real and accepted: that collection's attribute set is pinned by a golden test, so adding the
   field is a deliberate contract change rather than an edit.

   **views-postprocessing writes it at delivery**, as part of the same upload that publishes the
   run. The producer declares what it published — which is where that knowledge already exists, and
   which removes the "someone forgot to move the pointer" failure rather than documenting it.
   Filed there as an issue; this repository reads the pointer and does not write it.

   Until that lands, this repository still selects by upload order, so the hazard in Context stands.
   Reading the pointer when present is the change here; writing it is upstream.

4. **The pointer is introduced before the archive, not after.** It is the only item here that is
   expensive to retrofit — once artifacts are addressable and something has pinned one, changing how
   "live" is expressed breaks the pins — and it depends on no upstream change. It does depend on
   item 3's storage choice, which is why that is marked rather than assumed.

5. **The archive is not built.** Partition storage waits on the upstream work below. What is built
   now is items 1 and 3 only, exercised against production forecasting runs — item 2's sequence
   component is specified but not implemented, because no artifact exists that needs it.

**Out of scope, and filed upstream rather than compensated for here:**

- `run_type` on the wire contract — views-postprocessing / views-pipeline-core.
- The CLI prohibition on storing calibration and validation runs — views-pipeline-core.
- The three conflicting partition-calendar definitions (real configs 457/504 and 505/552;
  pipeline-core's fallback 397/444 and 445/492; the scaffolding template 445/492 and 493/540).
- MetricFrame paths carrying no timestamp or run id, so a second calibration run silently destroys
  the first run's evaluation of record. Confirmed by views-pipeline-core on 2026-08-26 at
  `evaluation/stage.py:395`, whose own docstring calls that path *"the evaluation-of-record (#226)"*.

**One upstream fact that changes a downstream instruction.** views-pipeline-core reports that on the
`PredictionFrame` path, `pgm_cm_point` is **not honoured as point reconciliation at all**:
`prediction_frame_ensemble.py:646` gates on truthiness rather than equality, so a sampled ensemble
declaring `pgm_cm_point` receives aligned-draws regardless. The name does not describe the behaviour
on the path CRAF'd would be on. **If a reconciliation type is adopted for this delivery, adopt
`pgm_cm`.** This is a concrete instruction for views-models#423, not a preference.

Note the timing precisely, because an earlier draft of this ADR overstated it: pipeline-core PR #492
(deprecate `pgm_cm_point`, warn rather than refuse) is **open against `development` and not merged**,
so `pgm_cm_point` warns in **no released version**. #490 stays open until step 3. **The instruction
does not depend on #492**: the truthiness gate at `prediction_frame_ensemble.py:646` is in shipped
code today, so a sampled ensemble declaring `pgm_cm_point` already receives aligned-draws regardless
of when the deprecation lands.

---

## Rationale

**"Whatever arrived last" is not a selection policy; it is the absence of one.** Making the live run an
explicit declaration replaces an accident of upload ordering with a statement someone made on
purpose. That is worth doing even if the archive is never built.

**It untangles two guarantees that currently share one mechanism.** `_identity_ok`
(`dataset_service.py:737-753`) asserts that the cached copy equals the newest manifest, and that
single comparison serves as both cache invalidation *and* the ADR-033 promise that a forecast is
never served from anything but the current manifested run. Neither can be changed alone
today, and neither has a test naming it. An explicit pointer separates them: the pointer carries the
guarantee, the cache key carries the invalidation.

**Identity belongs in the value, not in the index.** Four of the nine review seats reached this
independently. A file whose name does not say which run, partition and lead time it represents is a
file that can be mis-filed, and no catalogue can repair that after the fact.

**It is cheap now and expensive later.** Retrofitting an identity dimension onto published artifacts
means re-publishing the archive and breaking every pin. One path segment now costs nothing.

**Minimum machinery.** The pointer is a record, not a service. No registry, no resolver, no
strategy. The manifest file id already functions as a run address throughout the cache layer
(`dataset_service.py:207,976,1014`); what is missing is a way to *choose* which one and a namespace
with room for more than one at a time.

---

## Consequences

**Positive — stated as what each item delivers, and when**

- **On item 3 shipping:** the live hazard is removed. Until then it stands, and this ADR does not
  reduce it. Recorded that way because an ADR that says "removes" while the fix is unbuilt is how a
  known hazard stops being tracked.
- **On item 1 shipping:** the service finds a run's files by asking for that run, instead of
  scanning every file in the bucket and matching on filename.
- **On items 1 and 3 together:** a consumer can pin a run and re-fetch it, verified against the
  per-shard sha256 already in the manifest — and a run can be marked superseded or withdrawn, which
  is the deprecation path views-datafactory's model cannot express and cannot retrofit.

**Negative, and accepted**

- **A second mutable thing exists.** It is written by the delivery itself rather than by a person,
  so there is no new manual step and no "someone forgot" failure. If the write fails, the delivery
  fails loudly at the point of upload instead of leaving a fresh run stored but unserved. That is
  the reason for putting the write at the producer rather than in the runbook — a step in a runbook
  is a step someone can skip.
- **The address format is a commitment.** Once a consumer pins, the path shape is a contract.
- Partition support remains unbuildable here until upstream moves. Building the shape against one
  partition is a deliberate bet that the shape generalises; if the wire contract lands with a
  different vocabulary, the paths change before anyone has pinned them.

**Deliberately unresolved**

- Whether the archive eventually belongs in **this service at all**. CCP and CRP both argue it may
  not: a live monthly delivery and a research evaluation archive have different consumers, cadences,
  retention needs and failure consequences. The review declined to pre-decide this while no archive
  exists. Keeping artifacts run-addressed from the start means that later split is a *retention and
  routing* decision rather than a rewrite.

---

## Alternatives Considered

**Keep newest-wins.** Rejected: it is already a live hazard, and it cannot express an
archive at all.

**Catalogue-only identity** — files named by convention, identity carried in an index. Rejected by
Kleppmann, Hickey, GoF and Ousterhout seats independently: the index becomes a second source of
truth, and a 468-entry-per-partition enumeration is an index doing a path's job.

**Content addressing** (name artifacts by digest). Rejected as insufficient alone: a digest is a
good *integrity* check and a poor *human* address. It answers "is this the bytes I expected" and not
"which run, which partition, which lead time". The per-shard sha256 already in the manifest gives us
the integrity half for free.

**Extend `category` to carry the partition.** Rejected: it is a closed enum enforced by a raise and
a golden test, and overloading it would conflate "what kind of data" with "which experiment".

---

## Open Questions

1. ~~**Where the live-run pointer is stored**~~ **Ratified 2026-08-26** — see Decision item 3. What
   remains is upstream: views-postprocessing has to write it. Reading it is buildable here now.

2. **Retention for archived runs**, when the archive exists. ADR-036 decides retention for *built
   artifacts* — keep two runs — but an archive of calibration and validation runs is a different
   thing with a different purpose, and two would be the wrong number for it. Not urgent, because the
   archive is not built.

3. **Whether the archive belongs in this service at all.** Recorded in Consequences as deliberately
   unresolved. Keeping artifacts run-addressed from the start means a later split is a routing and
   retention decision rather than a rewrite, so this does not need answering now.

---

## Related

**Findings behind this ADR** are recorded in the maintainer's local risk register, which is
not part of this repository. Every measurement this ADR relies on is stated inline above so the
decision can be read without it.

**ADRs in this repository:** ADR-036 (the artifacts being addressed), ADR-033 (the guarantee that a
forecast is never served from anything but the current run — which the pointer must preserve).

**Cross-repo.** ADR numbers are per-repository and collide: `views-postprocessing/ADR-013` is not
this repository's ADR-013, which is about environment-variable validation. Foreign references are
written `repo/ADR-nnn`; a bare `ADR-nnn` always means this repository.

- `views-postprocessing/ADR-013` — the wire contract, which would have to carry `run_type`
**Upstream issues:** views-models #419/#421/#422/#423, views-pipeline-core #490.
