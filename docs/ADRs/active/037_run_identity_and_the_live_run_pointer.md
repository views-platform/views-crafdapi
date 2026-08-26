# ADR-037: Run Identity and the Live-Run Pointer

**Status:** Proposed — awaiting maintainer ratification
**Date:** 2026-08-26
**Deciders:** Simon (maintainer)
**Consulted:** expert-code-review panel (2026-08-26), Kleppmann/Hickey/Feathers seats in particular
**Informed:** views-postprocessing (the producer), views-pipeline-core (the CLI prohibition), views-models

---

## Context

**This service has no concept of run identity.** `get_latest_manifest()`
(`managers/prediction/manager.py:298-311`) takes no arguments and returns `docs[0]` after sorting
every matching document by `$createdAt` descending, in Python. That is a **last-write-wins
register** — the weakest available conflict resolution — and it is the sole mechanism deciding what
a UN partner is served.

There is no queryable run identifier at all. `run_id` exists only *inside* the manifest JSON
(`forecast/ingestion/wire_reader.py:58,87`), so learning which run a stored document belongs to
requires downloading it. Shard resolution compounds this: `resolve_artifact_file_ids`
(`manager.py:313-332`) queries every shard document of every run ever uploaded, unscoped, and
matches by filename in Python (**C-305**).

**The concrete hazard exists today, before any new work.** Upload a second forecast artifact — a
calibration experiment, a re-run, a test — and it becomes the served CRAF'd forecast, unconditionally,
because it is newest (**C-303**). The only control is a comma-separated list of file ids in an
environment variable.

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
| **total** | **972** (2,916 shards: 2.5 GB as packaged, ~50 MB repacked, **96.5 GB expanded**) |

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

1. **Artifacts are addressed by run.** Every built artifact (ADR-036) is written under an address
   that names the run, and that address is stable and immutable. An artifact, once published, is
   never rewritten in place.

2. **Lead time is part of the address where it exists.** For rolling-origin partitions the address
   carries the sequence, not merely the month. **This goes in the path, not only in a catalogue** —
   because a catalogue that carries identity the bytes do not is a second source of truth that can
   drift from the thing it describes.

3. **One explicit live-run pointer.** A single, small, named, mutable record states which run is
   current for CRAF'd. It is the *only* mutable thing in the delivery. Everything else is an
   immutable value. Selection stops being an emergent property of upload order.

4. **The pointer is introduced now, with the one partition that exists.** This is the only item here
   that is expensive to retrofit and nearly free to get right at the start, and it does not depend
   on any upstream change.

5. **The archive is not built yet.** Partition storage waits on the upstream work below. What is
   built now is the *shape* that accommodates it: run-addressed artifacts and an explicit pointer,
   exercised against production forecasting runs.

**Out of scope, and filed upstream rather than compensated for here:**

- `run_type` on the wire contract — views-postprocessing / views-pipeline-core.
- The CLI prohibition on storing calibration and validation runs — views-pipeline-core.
- The three conflicting partition-calendar definitions (real configs 457/504 and 505/552;
  pipeline-core's fallback 397/444 and 445/492; the scaffolding template 445/492 and 493/540).
- MetricFrame paths carrying no timestamp or run id, so a second calibration run silently destroys
  the first run's evaluation of record.

---

## Rationale

**Last-write-wins is not a selection policy; it is the absence of one.** Making the live run an
explicit declaration replaces an accident of upload ordering with a statement someone made on
purpose. That is worth doing even if the archive is never built.

**It untangles two guarantees that currently share one mechanism.** `_identity_ok`
(`dataset_service.py:737-753`) asserts that the cached copy equals the newest manifest, and that
single comparison serves as both cache invalidation *and* the ADR-033 promise that a forecast is
never served from anything but the current manifested run (**C-306**). Neither can be changed alone
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

**Positive**

- Removes **C-303** — the live hazard that any second upload silently becomes production.
- A consumer can pin a run and re-fetch it, verified against the per-shard sha256 already in the
  manifest.
- Makes a deprecation path possible: a run can be marked superseded or withdrawn, which is the thing
  views-datafactory's model cannot express and cannot retrofit.
- Scopes shard resolution to a run, removing the unbounded bucket scan (**C-305**).

**Negative, and accepted**

- **A second mutable thing exists in the system** — the pointer — and it must be updated as part of
  delivery. If it is forgotten, a fresh run is stored but not served. That failure is *visible*
  (provenance names the old run) where the current failure is silent, which is the trade being made.
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

**Keep newest-wins.** Rejected: it is already a live hazard (**C-303**), and it cannot express an
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

1. **The exact address format**, which should be settled with views-postprocessing rather than
   declared here, since they write the artifacts.
2. **How the pointer is stored** — a document in the existing collection, a file in the bucket, or
   configuration. The requirement is only that it is explicit, small, and readable without
   downloading a run.
3. **Retention**, shared with ADR-036: an archive with no eviction rule is an unbounded queue, and
   this codebase has no size-based eviction anywhere.

---

## Related

**Register:** C-303 (no run address — the entry this ADR primarily answers), C-305 (unscoped shard
resolution), C-306 (the two guarantees in one mechanism), C-308 (pgm-only, unreconciled — the spec
gap that shares the upstream blockers), C-233, C-254, C-287, C-236.
**ADRs:** ADR-036 (the artifacts being addressed), ADR-033 (the fail-visible guarantee the pointer
must preserve), ADR-013 upstream (the wire contract that must carry `run_type`).
**Upstream issues:** views-models #419/#421/#422/#423, views-pipeline-core #490.
