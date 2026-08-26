# ADR-036: Bulk Delivery Is a Built Artifact, Not a Rendered Response

**Status:** Proposed — awaiting maintainer ratification
**Date:** 2026-08-26
**Deciders:** Simon (maintainer)
**Consulted:** expert-code-review panel (2026-08-26); views-faoapi and views-datafactory registers and post-mortems
**Informed:** CRAF'd (not yet a consumer — see Context)

---

## Context

**CRAF'd does not call this API.** There is no live consumer, no release note, and no signed
delivery agreement. The FAO-facing release notes and the LoA referenced throughout this repository
are inherited from views-faoapi and bind nothing here. This is the single fact that makes the
decision available: **retirement breaks nobody.**

The agreed specification is small: a global forecast, 36 months ahead, carrying uncertainty as 128
posterior draws, from PGM and CM ensembles that are reconciled, in an agreed 45-column schema.
**Delivery mechanism is entirely open.** ADR-026, which defines the current 20-route query grid,
lists its Informed party as *UN FAO / FSFC API consumers*; its Context records that the route table
"grew organically… and has never been described in a decision record"; and its registration loop is
character-for-character identical to views-faoapi's. The surface was inherited by forking, not
chosen for this consumer (**C-307**).

### The measurements that decide this

| | |
|---|---|
| Full production posterior — every draw, every target, globally | **1.81 MB** of value bytes |
| The same, as currently packaged on the wire | 94 MB (98% is `unit` and `sample` columns derivable from the header) |
| The same, expanded as `(N,S)` float32 in the serving cache | **3.58 GB** |
| Historical artifact | 164 MB, of which **518 KB is data** — the rest is `priogrid_id` and geography repeated 439 times |
| Full 45-column consumer schema, precomputed at **all four** aggregation levels | **~16 MB**, ~2.5 min per delivery |
| An unparameterised `subset` request, rendered as JSON | **34.5 GB, 13.5 minutes** (C-284) |

The data is small. **The transport is what is expensive.** Every memory ceiling, OOM, 504 and
62-second cold start in this repository's history is an artifact of rendering parquet as JSON per
request, not a property of the data.

### What the siblings paid

**views-faoapi** runs this same architecture over this same data. Their register records three OOM
kills at ~23.3 GB each on the box we share with them; eight weeks of deliveries returning 2.3M rows
with **zero value columns** under HTTP 200; and six client timeouts caused by their own shipped
default. Their post-mortem states:

> Bulk delivery renders JSON per request rather than serving the artefact. That choice is why the
> machinery exists… **A service that served the file would have had no slot to be wrong about.**

and, measured on their own codebase: **"87% of this service is machinery for moving bytes; 13% is
the thing it exists to do."** They costed the change: **40 of 218 register entries vanish outright**
under file delivery. They attempted the deletion and abandoned it for exactly one reason — *"FAO run
the endpoints the step would delete."* **That blocker does not exist here.**

**views-datafactory** delivers comparable data as static files with no application server, and
ADR-021 explicitly considered and rejected FastAPI. Their register shows the bill: no versioning
(the previous export is `rmtree`'d, so a consumer cannot pin and there is no rollback), no
deprecation mechanism at all, no server-side filtering, and **no consumer observability** — their
three worst data defects were found by a human doing unrelated work, by a script written afterwards,
and by another team filing a bug in their repository.

### The statistical constraint, and what it actually proves

The HDI of a sum is not the sum of the HDIs. Correct aggregate uncertainty requires summing aligned
draws **before** computing the interval, so the aggregation must happen where the draws are.
views-faoapi's `DELIVERY_SPECIFICATION.md` §3 states this and adds that *"any proposal to 'just ship
files and delete the query surface' is wrong for this reason, and it is the single easiest thing on
this page to forget."*

It is correct, and it **proves server-side aggregation, not a query API.** The aggregation
vocabulary is *closed* — five levels × two categories — so precomputing each combination at ingest
satisfies the statistics identically, at ~2.5 minutes per delivery against 3–70 seconds per request.
`alpha` is already inert; the credible masses are fixed at 50/90/95, so there is no caller-selected
dimension a file cannot precompute. This repository already does exactly this for one route:
`/data/forecast/bulk` is built and served with `FileResponse`.

---

## Decision

**Bulk delivery is a built artifact. The query grammar is not the bulk channel.**

Concretely, and in this order:

1. **Build at ingest, serve as files.** Once per delivered run, compute the 45-column consumer
   schema at each aggregation level and write it as parquet. Serve those files with `FileResponse`,
   which brings `Content-Length`, `ETag`, `Accept-Ranges` and resumability.

2. **Verify against the existing characterization test.** `/data/forecast/bulk` returns **461,991
   bytes, byte-stable across v0.4.0, v0.5.1 and v0.6.1**. The admin-1 artifact produced by the new
   path must match it byte for byte before anything else proceeds.

3. **Add the new path alongside the existing routes. Delete nothing yet.** The query grid is retired
   only after the built artifacts have served a full monthly cycle, and then only what is provably
   unreferenced.

**Explicitly in scope:** the forecast and historical consumer schema at pg, country/gaul0, admin-1
and admin-2; the build step; file serving; and the ADR-026 amendment recording that the levelled
grammar is for slices, not bulk.

**Explicitly out of scope, and deferred with named triggers:**

- **Publishing the raw posterior draws.** Nobody has asked for them. Once published they are a
  contract, and every future repacking decision would be constrained by consumers we never confirmed
  exist. *Trigger to revisit:* CRAF'd asks for a quantity we do not compute, or asks to aggregate to
  a geography outside the closed vocabulary.
- **Deleting the cache tiers.** views-faoapi attempted this and found three of four targets
  load-bearing for reasons unrelated to rendering — per-key partitioning is a **security** property
  (C-287), and the disk tier *implements* the ADR-033 last-good fallback. *Trigger:* a
  characterization test exists for each tier naming what it guarantees.
- **The calibration/validation archive.** Blocked upstream in three places; see ADR-037.

---

## Rationale

**It is a subtraction, not a rewrite.** The proposal generalises a route that already works
(`/data/forecast/bulk`), against a byte-stable characterization test that already exists, at the
existing ingest chokepoint (`_get_latest_dataset`). The operator's preference for local improvement
over rewrites is honoured by staging: build alongside, verify, then remove.

**It converts a shallow module into a deep one.** Thirty-two routes over a job whose entire
information content is 1.81 MB is the textbook definition of a large interface over a small
substance. Moving the complexity to build time pays it once, offline, where it can be debugged
without a request attached.

**It makes correctness testable.** Today, proving a served number correct requires an Appwrite mock,
a cache fixture and an HTTP client — and that fixture has actively lied: seeding a `pred_*` dataset
into the historical cache slot is how a Tier 1 defect survived eight weeks of green tests next door.
With built artifacts the test is bytes against golden bytes.

**It keeps what file-only delivery never gets.** We already run the box, systemd and nginx, so a
thin service is near-zero marginal cost — and it retains per-key authentication, revocation, request
logs, versioned addressing and a deprecation path. Those are precisely the four things
views-datafactory's register shows were never built and cannot now be retrofitted. For a UN-facing
partner, being able to answer *"is the consumer broken?"* is worth one process.

**Minimum machinery.** No new abstraction, layer, registry or framework is introduced. The build is
one function taking a run and a level. Calibration, validation and forecasting differ in month range
and sequence count, not in kind, so they need parameters, not strategies.

---

## Consequences

**Positive**

- Removes the failure mode that killed the neighbour three times: per-request expansion to 3.58 GB
  on a 22 GiB shared box.
- A stale artifact is still a valid, readable, correctly-labelled file. A stale cache slot is a
  wrong answer wearing a 200.
- The aggregation cost moves from *per request* to *once per delivery*.
- Materially shrinks the surface on which twelve open concerns live (**C-307**).

**Negative, and accepted**

- **A build step is a batch job with a deadline**, and that is new machinery even though it removes
  more than it adds. It needs idempotency, partial-failure semantics, and a failure surface distinct
  from the serving health check — the pipeline and the file server fail independently, which
  views-datafactory needed three monitors to learn.
- **Arbitrary filtering is given up.** ADR-026 records that per-field subsetting *"was added as a
  developer convenience for experimentation; it is not the recommended workflow and never was."*
  No CRAF'd requirement names it.
- Until the grid is retired, both paths exist and must agree. That is the cost of not doing a
  big-bang, and it is the right cost to pay.

**Neutral**

- Staleness is untouched. views-faoapi *had* an API and still served a 139-day-old artifact behind
  green health (ADR-033). **Staleness is a pipeline problem, not a delivery-mechanism problem**, and
  neither architecture fixes it.

---

## Alternatives Considered

**A — Keep the query grid and fix its defects.** Rejected on arithmetic. Even setting aside the 12
open concerns on that surface, the partition requirement in ADR-037 needs 96.5 GB expanded on a
22 GiB box. Render-on-demand does not become risky; it becomes impossible.

**D — Static files with no application server, views-datafactory style.** Rejected on the four gaps
their own register documents: no versioning, no deprecation, no server-side filtering, no consumer
observability. Acceptable for a handful of internal Python processes; not for a UN partner we cannot
otherwise observe.

**E — Ship a reader library only.** Rejected: we have no evidence CRAF'd is a Python shop, and it
answers neither discovery nor access control.

**Full B+C in one change** — build, publish draws, delete the grid, and become an archive
simultaneously. Rejected by the review panel as four separable decisions in one bundle that cannot
be reverted selectively, three of which lack evidence or are blocked upstream.

---

## Open Questions

1. **Which of `country` and `gaul0` survives.** They are the same geography under two key columns
   (`forecast/serialize/schema.py:70-74`), with different missing-code handling and no test
   comparing them. `ROADMAP.md:147-149` already names the divergence as undetectable. Precomputation
   forces the question, because it means writing the same numbers to two files.
2. **Retention.** How many built artifacts are kept, evicted by what rule. There is no size-based
   eviction anywhere in this codebase (**C-236**). A number written now costs nothing.
3. **Whether historical needs a computed product at all**, or whether making the stored parquet
   followable is sufficient (**C-304** is the current blocker either way).

---

## Related

**Register:** C-307 (the inherited surface), C-284 (the bound this supersedes for bulk), C-286 (the
download defect on the file channel), C-304 (the unfollowable escape hatch), C-287 and C-236 (the
cache properties that are *not* about rendering).
**ADRs:** ADR-026 (the surface this amends), ADR-033 (fail-visible selection, unaffected), ADR-034
(the CRAF'd data contract — content-only, names no endpoint), ADR-037 (run identity).
**Cross-repo:** views-faoapi ADR-026 amendment 2026-08-22 and `DELIVERY_SPECIFICATION.md` §3;
views-datafactory ADR-021 and ADR-050.
