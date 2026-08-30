# ADR-036: Bulk Delivery Is a Built Artifact, Not a Rendered Response

**Status:** Proposed — awaiting maintainer ratification
**Date:** 2026-08-26
**Deciders:** Simon (maintainer)
**Consulted:** expert-code-review panel (2026-08-26); views-faoapi and views-datafactory registers and post-mortems
**Informed:** CRAF'd (see the scope note in Context)

---

## Context

**This service renders a 1.81 MB product as tens of gigabytes of JSON, once per request, and that
choice has already been paid for in production — on the machine we share.**

The whole global forecast — every posterior draw, for every target, for every month — is **1.81 MB**
of numbers. Converted to JSON and sent over HTTP, the same numbers come to roughly **18 GB**.

**The worst case takes no arguments at all.** A caller who asks for the historical data without
naming any filter — the bare URL, nothing else — is asking the service to turn 28.4 million rows
into JSON. Measured against the real artifact: **34.5 GB of memory and 13.5 minutes**, on a machine
with 22 GiB of RAM that is shared with a second service.

That second service is views-faoapi, which runs this same architecture over this same data. On
2026-08-14 the kernel killed it three times in 41 minutes, each time while it was holding about
23 GB.

That is the problem this ADR answers, and it is a property of the **transport**, not of the data. We
are not short of memory because the forecast is large. We are short of memory because we convert a
compact numeric format into a verbose text one, per request, at full size.

**A new requirement turns a bad ratio into an impossible one.** The specification has gained the
calibration and validation partitions. Those are rolling-origin, meaning each month is predicted
repeatedly at different lead times, so they hold **972 predicted month-slices against today's 36**.
Held in memory the way the current serving path holds a run, that is **96.5 GB** — on a 22 GiB box
(see ADR-037). Rendering on demand does not become risky at that size. It stops being arithmetically
possible.

### What is agreed, and what is open

The specification itself is small: a global forecast, 36 months ahead, carrying uncertainty as 128
posterior draws, from PGM and CM ensembles that are reconciled, in an agreed 45-column schema.
**It names no delivery mechanism**, and neither does ADR-034, the CRAF'd data contract, which is
content-only and still marked Proposed.

The 20-route query grid was therefore never chosen for this consumer. ADR-026, which defines it,
lists its Informed party as *UN FAO / FSFC API consumers*; its own Context records that the route
table "grew organically… and has never been described in a decision record"; and its registration
loop is character-for-character identical to views-faoapi's. It was inherited by forking.

*Scope note, true as of 2026-08-26 and expected to change:* CRAF'd is not yet calling this service.
That does not motivate the decision — the measurements above hold whether or not a consumer is
connected — but it does widen the options available, and it is why this ADR can propose removing a
surface rather than only bounding one. See Consequences for what changes when that stops being true.

### The measurements that decide this

| | |
|---|---|
| Full production posterior — every draw, every target, globally | **1.81 MB** of value bytes |
| The same, as currently packaged on the wire | 94 MB (98% is `unit` and `sample` columns derivable from the header) |
| The same, expanded as `(N,S)` float32 in the serving cache | **3.58 GB** |
| Historical artifact | 164 MB, of which **518 KB is data** — the rest is `priogrid_id` and geography repeated 439 times |
| Full 45-column consumer schema, precomputed at **all four** aggregation levels | **~16 MB**, ~2.5 min per delivery |
| An unparameterised `subset` request, rendered as JSON | **34.5 GB, 13.5 minutes** |

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
`views-datafactory/ADR-021` explicitly considered and rejected FastAPI. Their register shows the bill: no versioning
(the previous export is `rmtree`'d, so a consumer cannot pin and there is no rollback), no
deprecation mechanism at all, no server-side filtering, and **no consumer observability** — their
three worst data defects were found by a human doing unrelated work, by a script written afterwards,
and by another team filing a bug in their repository.

### The statistical constraint, and what it actually proves

The HDI of a sum is not the sum of the HDIs. Correct aggregate uncertainty requires summing aligned
draws **before** computing the interval, so the aggregation must happen where the draws are.
`views-faoapi/DELIVERY_SPECIFICATION.md` §3 states this and adds that *"any proposal to 'just ship
files and delete the query surface' is wrong for this reason, and it is the single easiest thing on
this page to forget."*

It is correct, and it **proves server-side aggregation, not a query API.** The list of aggregations we offer
is fixed and short — five geographic levels, two categories, so ten combinations — so precomputing
every one of them when a run arrives satisfies the statistics identically, at ~2.5 minutes per delivery against 3–70 seconds per request.
`alpha` is already inert; the credible masses are fixed at 50/90/95, so there is no caller-selected
dimension a file cannot precompute. This repository already does exactly this for one route: `/data/forecast/bulk` builds a file and
hands it back, rather than assembling a response per request.

---

## Decision

**Bulk delivery is a built artifact. The query grammar is not the bulk channel.**

Concretely, and in this order:

1. **Build at ingest, serve as files.** When a run is ingested, compute the 45-column consumer
   schema at each aggregation level and write it as parquet, before any request arrives. Hand those
   files back directly rather than generating a response, so the caller is told the size up front,
   can resume an interrupted download, and can skip re-downloading a file that has not changed.

   **What is built, precisely.** Eight artifacts per run: two categories (`forecast`, `historical`)
   × four levels (`pg`, `country`, `gaul1`, `gaul2`). `gaul0` is **not** built — see the decision
   below.

   **Where.** Alongside the existing per-run cache, under the run's own directory, named
   `{category}_{level}.parquet`. The layout is this repository's own and needs no agreement with the
   producer; the *addressing* of runs is ADR-037's subject, not this one's.

   **When.** Triggered by the same ingest that already assembles a run — not by a request, and not
   by a separate schedule.

   **`DECISION NEEDED` — `country` vs `gaul0`.** These are the same geography under two key columns,
   and precomputation forces the question because it would otherwise mean writing the same numbers
   to two files. This ADR builds `country` and not `gaul0`, on the grounds that ISO3 is the key a
   partner will use — **but that is a partner-facing choice and the operator should overrule it if
   ISO3 is wrong.** Until ratified, the `gaul0` route continues to serve from the query path.

2. **Verify against what the service already produces.** The current `/data/forecast/bulk` route
   returns a parquet file of **461,991 bytes**, and it has returned exactly that size in three
   consecutive releases — so any change to it is visible. The file the new build step produces must be identical, byte for byte, before anything else
   proceeds.

   **That check covers one of the eight artifacts.** `/data/forecast/bulk` is admin-1 forecast only;
   the other seven have no existing output to compare against. For those, the acceptance criterion
   is weaker and stated so it is not mistaken for the byte check: each must load, carry all 45
   columns, have row count equal to (months × units at that level), and contain no all-null value
   column. Both criteria are recorded because they are not the same strength, and the difference
   should be visible to whoever reads the results.

3. **Add the new path alongside the existing routes. Delete nothing yet.** The query grid is retired
   only after the built artifacts have served a full monthly cycle. "Unreferenced" then means:
   absent from `CrafdApiClient`, from `smoke.py`, from every notebook under `notebooks/`, and from
   both READMEs — which is the same test used to retire `/data/{category}/latest`, and which is
   checkable by grep rather than by judgement. It does **not** mean "no consumer calls it": that
   would need an access-log read, and the log is not available to this repository.

4. **Retention: keep two runs.** The artifacts for the live run and for the one it replaced are
   kept; older sets are deleted when a new run is adopted. Two rather than one so a bad adoption can
   be reversed without a rebuild, and two rather than many because nothing in this codebase evicts
   by size and an unbounded set on a 22 GiB shared box is how the neighbouring service failed. At
   ~16 MB per run that is ~32 MB, so the number is chosen for reversibility, not for space.

**Explicitly in scope:** the forecast and historical consumer schema at `pg`, `country`, `gaul1` and
`gaul2`; the build step; retention; file serving; and the ADR-026 amendment recording that the
levelled grammar is for slices, not bulk.

**Explicitly out of scope, and deferred with named triggers:**

- **Publishing the raw posterior draws.** Nobody has asked for them. Once published they are a
  contract, and every future repacking decision would be constrained by consumers we never confirmed
  exist. *Trigger to revisit:* CRAF'd asks for a quantity we do not compute, or asks to aggregate to
  a geography outside the fixed list of levels we precompute.
- **Deleting the cache tiers.** views-faoapi attempted this and found three of four targets
  load-bearing for reasons unrelated to rendering — per-key partitioning is a **security** property, and the disk tier *implements* the ADR-033 last-good fallback. *Trigger:* each cache tier has a test that states, in words, what that tier guarantees — so
  removing it fails loudly rather than quietly.
- **The calibration/validation archive.** Blocked upstream in three places; see ADR-037.

---

## Rationale

**It is a subtraction, not a rewrite.** The proposal generalises a route that already works
(`/data/forecast/bulk`), against a file whose exact size has not changed in three releases, at the
single place a run is already loaded (`_get_latest_dataset`). The operator's preference for local improvement
over rewrites is honoured by staging: build alongside, verify, then remove.

**It shrinks the surface without shrinking the capability.** Thirty-two routes over a job whose
entire information content is 1.81 MB is a very large way in to a very small thing. Moving the work
to build time pays it once, offline, where it can be debugged without a request attached. Moving the complexity to build time pays it once, offline, where it can be debugged
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

- Removes the failure mode that killed the neighbouring service three times: loading a whole run
  into memory, 3.58 GB at a time, on every request, on a 22 GiB shared box.
- A stale artifact is still a valid, readable, correctly-labelled file. A stale cache slot is a
  wrong answer wearing a 200.
- The aggregation cost moves from *per request* to *once per delivery*.
- Materially shrinks the surface on which twelve open concerns live.

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

**What changes when CRAF'd connects**

The scope note in Context is dated because it will expire. Once there is a live consumer, the
staging in the Decision stops being a courtesy and becomes the mechanism: the built path must be
serving, and verified, before anything is removed. Nothing in the Decision needs to change — but
step 3's "delete nothing yet" acquires a second reason, and the retirement of the query grid becomes
a partner-communication act rather than a repository one.

**Neutral**

- Staleness is untouched. views-faoapi *had* an API and still served a 139-day-old artifact behind
  green health (ADR-033). **Staleness is a pipeline problem, not a delivery-mechanism problem**, and
  neither architecture fixes it.

---

## Alternatives Considered

**A — Keep the query grid and fix its defects.** Rejected on arithmetic. Even setting aside the 12
open concerns on that surface, the partition requirement in ADR-037 would need 96.5 GB
held in memory on a 22 GiB box. Render-on-demand does not become risky; it becomes impossible.

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

1. **Is `country` the right survivor?** The Decision picks `country` (ISO3) over `gaul0` and says
   why, but the reasoning is about what a partner will key on, which is the operator's call rather
   than an engineering one. If `gaul0` is the right key, one line of the Decision changes and
   nothing else does. Until ratified the Decision stands and `gaul0` keeps serving from the query
   path, so neither answer is blocked.

2. **Does the historical file channel survive alongside the built historical artifacts?** The
   Decision builds historical at four levels. That is a *computed* product; the raw 164 MB parquet
   the producer uploaded is a different thing, and a consumer may want it. Keeping both is cheap and
   they answer different questions, but nobody has asked for the raw one. Note that its discovery
   path is broken either way — the refusal message names two provenance fields the endpoint does not
   return — and that must be fixed whichever way this lands.

3. **Nothing in this ADR states how a consumer discovers which artifacts exist.** The Decision names
   what is built, where, and when, but not how it is found. That is ADR-037's subject and is
   deliberately not settled here; recorded so the gap is visible rather than implied.

---

## Related

**Findings behind this ADR** are recorded in the maintainer's local risk register, which is
not part of this repository. Every measurement this ADR relies on is stated inline above so the
decision can be read without it.

**ADRs in this repository:** ADR-026 (the surface this amends), ADR-033 (fail-visible selection,
unaffected), ADR-034 (the CRAF'd data contract — content-only, names no endpoint), ADR-037 (run
identity).

**Cross-repo.** ADR numbers are assigned per repository, so the same number names different
documents in different repositories. Foreign references are therefore written `repo/ADR-nnn`; a
bare `ADR-nnn` always means this repository.

- `views-faoapi/ADR-026` — their 2026-08-22 amendment bounding the same route family
- `views-faoapi/DELIVERY_SPECIFICATION.md` §3 — why aggregation must happen server-side
- `views-datafactory/ADR-021` — why they rejected an application server
- `views-datafactory/ADR-050` — their consumer contract as a file rather than a package
