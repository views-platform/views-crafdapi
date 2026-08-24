# Roadmap

**Definition of done:**

> **CRAF'd receives forecasts worth citing, every month, without anyone remembering to.**

**What "worth citing" means here — specced 2026-08-24, and it is narrower than it sounds.** It does
**not** mean the forecasts are accurate or that they beat a baseline; forecast skill is somebody
else's responsibility and is deliberately outside this goal. It means CRAF'd receives output that
can be *attributed and used*: we can name which pgm and cm ensembles produced it, all the conflict
targets are present, and every one of them comes back out of the API with the correct columns. A
placeholder ensemble fails that test only because nobody can point at what made it.

**What "without anyone remembering to" covers — and what it does not.** It covers the **delivery**:
the monthly act of putting the current forecast and the current observations into the bucket. It
does **not** cover *producing a new forecast* — running an ensemble is a research act with its own
cadence, and step 4 deliberately leaves it manual.

That distinction is load-bearing, so state the consequence plainly: **a delivery that runs on
schedule while no new ensemble has been run re-ships the same forecast with fresher observations.**
That is a useful outcome — the `*_actual` column advances, which is the half that changes monthly by
nature — but it is not a new forecast, and the byte-count check in step 2 is what tells the two
apart.

**What "every month" means, concretely.** A delivery on or after the 22nd of each calendar month.
**A month counts as a missed month when no delivery has landed by the last day of that month** —
which is stricter than the 45-day freshness SLA that `data-freshness.yml` alarms on. The SLA is a
backstop for a badly missed delivery, not the definition of the cadence; a 44-day gap satisfies the
monitor and still fails this goal. The heartbeat in step 4 is what closes that gap.

*Set 2026-08-24, after the previous definition of done — EPIC #40, "CRAF'd can actually use this
API, proven with real data" — was completed and closed.*

*The four steps are agreed and most specs are now written down. **Three things are still open**, each
marked in place rather than papered over with a plausible-sounding sentence:*

| | what | whose |
|---|---|---|
| **`DECISION NEEDED`** | how multi-target composition works across pgm and cm | design, upstream |
| **`SPEC NEEDED`** | who runs the *first* delivery (step 4 decides where the recurring one runs) | operator |
| **`SPEC NEEDED`** | who owns the cron entry and the heartbeat account | operator |

*Everything else marked "Decided" below was decided by me, with the reasoning stated, and is open to
being overruled.*

*This document was then attacked with `/falsify` against the claim that it contains no hand waving,
ambiguity, or decision gap. **It did not survive — 4 hard findings and 4 soft.** All eight are fixed
above, and `tests/test_falsify_roadmap_specs.py` now guards each one, so a regression to the same
ambiguity fails the suite. The two that mattered: the goal said "without anyone remembering to"
while step 4 kept Hop A manual, and the goal required naming cm ensembles the document itself
records as non-existent.*

---

## Why a new one was needed

EPIC #40 proved the **pipes** are real: a genuine forecast run, genuinely delivered to Appwrite,
genuinely served, with arithmetic verified against the raw draws to `max|diff| = 0`.

It did not prove the forecasts are worth anything. `reports/ops/first_crafd_delivery.md:12,22`:

> **Ensemble** — `rusty_bucket`: **8 × `ConflictologyModel` baselines, not a trained ML model**
>
> **This is a placeholder ensemble.**

So the machinery works and a stand-in is flowing through it. The three words that changed in the new
definition are **"worth citing"**, **"every month"**, and **"without anyone remembering to"** — and
each maps to a step below.

---

## Step 1 — Name what feeds the bucket, and get all of it out

**Specced 2026-08-24 with the operator. This is not about model quality.** Beating a baseline is
someone else's responsibility and is explicitly *not* part of this definition of done.

What step 1 actually requires:

1. **We can say which pgm ensembles and which cm ensembles produce the forecast in the bucket.**
   Named, not inferred.
2. **All three conflict targets are present** — `lr_ged_sb`, `lr_ged_ns`, `lr_ged_os`.

   **Assessed against the target set as it stands on the day of assessment, and that set is
   currently those three.** More targets will follow; a fourth arriving does *not* retroactively
   un-do this step. It opens a new, smaller piece of work — add the target — which the multi-target
   design below has to make cheap. Without this sentence "done" could never be reached, because the
   set is open-ended by design.
3. **All of it comes back out through the API** — all historical data and all forecasts, with the
   correct columns.

The column **set** is settled: `schema.bulk_columns()` is the authority — 45 columns, 6 identity +
3 series × 13 — documented in `docs/api/data_dictionary.md` and reconciled across the repo on
2026-08-23 (**C-244**, which had the repo carrying three different totals).

Two things about those columns are **not** settled, and are decided further down rather than here:
the three exceedance columns will be **renamed** when CRAF'd confirms real thresholds (the ADR-034
decision), and `METHODOLOGY_VERSION` bumps once at that rename (step 3). So the shape is fixed and
two of the names are not.

### The hard part: more than one target, across models that disagree about how many they produce

The operator's words: *"we have models such as hydranet which can forecast multiple targets at once,
but we have no cm models that can do that for ensembles. And this becomes more messy when more
models with new targets enter. So we need to figure that out."*

**`DECISION NEEDED` — how multi-target composition works.** A pgm model may emit several targets in
one run; cm ensembles emit one at a time; the set of targets grows over time. There is no design for
composing those into one delivery today.

The stated constraint on the answer, which rules a lot out: **simple, robust, working, and it should
make sense. Not complicated, and not especially flexible — flexibility can come later.** So the bar
is a design that handles today's three targets and admits a fourth without a rewrite; it is *not* a
general target-algebra.

This decision is upstream of the delivery, not inside this repo. It should be settled before step 2
runs, because it determines what a delivery even contains.

### cm is in scope, and it is a prerequisite — not a footnote

The operator's spec names **pgm *and* cm** ensembles. That is kept rather than quietly narrowed, so
the consequence has to be stated: **step 1 cannot complete until #81 completes.** #81 is not a
related epic mentioned in passing; it is on the critical path of this roadmap's first step.

That is an uncomfortable dependency, because #81 is blocked on things outside this repo — chiefly a
cm baseline ensemble that does not exist. The alternative was to drop cm from the definition of
done, which would have made this document tidier and wrong.

**If cm turns out to be further away than it looks, the decision to take is whether to split the
goal** — pgm-only as a first milestone, cm as a second — rather than to leave step 1 open
indefinitely. That is a decision for the operator when #81's own blockers are costed, not one to
pre-empt here.

### What #81 says, and it is blocked

**#81** (`epic`, `blocked`, `needs-decision`) holds it, and is worth reading before any work starts.
Its own summary: *"the pgm+cm two-ensemble delivery does not exist anywhere in the platform."*

Four blockers in this repo — the wire manifest cannot distinguish two levels of analysis; there is
no LOA dimension in any cache key, so two runs would collide; `ForecastDataset` hard-raises on a
non-`priogrid_id` index; and the 9-column GAUL sidecar requirement is meaningless at country level.

And upstream: the wire contract needs an `loa` field ⇒ `contract_version` **1.6**, which this build
currently **refuses** — and the ordering is unforgiving, *"the consumer must ship 1.6 capability
before any producer emits it, or the delivery is refused on arrival."* Also: **no cm baseline
ensemble exists to deliver**, and ADR-013 lists cm as an explicit non-goal.

#81 also names a risk that is easy to miss and would surface as a partner complaint: with cm
authoritative at `/country` and pgm aggregated at `/gaul0`, **the same geography would be served two
different numbers by two endpoints, and nothing would detect the divergence.**

### ADR-034 — decided

**Ship provisional, rename later.** ADR-034 stays `Proposed` and the exceedance columns keep their
placeholder thresholds (`p_gt25`, `p_gt100`, `p_gt1000`). When CRAF'd confirms real numbers those
columns get **renamed**, and any consumer scoring against them changes code at that point. The
data dictionary already records them as placeholders; that is the disclosure this decision relies on.

## Step 2 — Run one real monthly delivery, end to end

Proves the manual path works before anyone automates it. The two blockers that made this impossible
were cleared 2026-08-23/24 (views-models#399, #403), and the procedure is `deployment/MONTHLY_REFRESH.md`.

**Blocked by step 1's multi-target decision** — that decision determines what a delivery contains,
so running one first would prove the wrong thing.

**Pass conditions — decided, and each is a thing you can look at:**

| check | passes when |
|---|---|
| `/provenance/forecast` | shows a **new** `run_id`, not `rusty_bucket_forecasting_20260727_095355` |
| `/health` | `healthy`, `forecast_freshness.is_stale: false` |
| `smoke.py --expect-tag <live tag>` | ALL PASS, both coverage checks included |
| `/data/forecast/bulk` | byte count **differs from 461,991** |

That last one is the load-bearing check and the easiest to skip. 461,991 bytes is what v0.4.0,
v0.5.1 and v0.6.1 all returned — byte-identical across three releases, because they were all serving
the same run. **An unchanged byte count after a delivery means the delivery did not land**, however
green everything else looks.

**`*_actual` is an observation, not a pass condition.** A delivery run after the 20th should be the
first time that column carries real numbers instead of zeros (**C-293**). Recording whether it does
is valuable; making it a gate is not, because it depends on upstream timing this repo does not
control.

**`SPEC NEEDED` — who runs the *first* one.** Step 4 decides where the *recurring* delivery runs
(the Hetzner box); this is the separate question of who performs the one-off first run, which need
not be on that machine — any host with conda, several GB, and network reach to Appwrite and the
datafactory zarr host will do. Doing it somewhere other than the box is arguably better for a first
run: it proves the procedure is not accidentally dependent on that one machine's state.

## Step 3 — Fix the two broken served surfaces

Tracked as **#125**. Not required by CRAF'd today, but the API cannot honestly be called
consumer-ready while they stand.

- `/data/{category}/latest` returns rows carrying **no values** behind `HTTP 200` (**C-232**,
  Tier 1, open since 2026-08-10). Measured live: 88 MB of rows containing only `month_id` and
  `priogrid_id`.
- **No data route has any size bound** (**C-284**). `GET /pg/data/historical/subset` with no query
  string asks for 34.5 GB — more than the machine has.

**Done 2026-08-24 — `/latest` is retired.** Both route handlers are gone, along with their entries
in the root endpoint catalog, `README.md`, `docs/api/README.md`, and ADR-026 (amended in place, the
original struck through rather than deleted). **C-232 is resolved by retirement**, not by a fix.

An earlier draft of this section made the retirement conditional on an nginx log read. That was the
wrong gate, and the decision table shows why:

| | keep it | retire it |
|---|---|---|
| nobody calls it | harmless | harmless |
| someone calls it | **silent wrong answer** | loud 404 |

Retirement is at least as good in both branches, so no log read can change the answer — a 404 tells
a caller to stop; a valueless 200 does not. The log read remains worth doing as a *partner-
communication* question: if CRAF'd was calling `/latest`, they have been receiving empty rows and
should be told. It is no longer a precondition. Nothing this repo ships called it — not
`CrafdApiClient`, not `smoke.py`, not any notebook.

**C-284 is unchanged in substance.** The two hollow routes are gone; the 20 `subset` and `hdi-map`
routes still carry no bound, including the unparameterised 34.5 GB case, which was always the worst
one. Tracked as #125.

**Decided — the bound is estimated bytes, not rows.** A row-count cap bounds nothing: a historical
row costs ~1,625 B through the serializer and a forecast row ~7,731 B, because the second carries a
draw per sample per target and **sample width varies per delivery**. The gate computes estimated
bytes from dataset shape, before materialising anything, and refuses above the budget. The budget
figure itself needs one measurement on the real artifact.

**Decided — `METHODOLOGY_VERSION` does not bump yet, and bumps once.** It stays at `3`. C-244 argues
the nine added exceedance columns warrant a bump under ADR-023, and that is right — but those same
columns are about to be **renamed** when CRAF'd confirms real thresholds (the ADR-034 decision
above). Bumping now and again at the rename is churn in a value CRAF'd reads. It bumps once, at the
rename, covering both changes.

## Step 4 — Automate the monthly run

Today it is two commands a human must remember on the 22nd. `data-freshness.yml` now opens an issue
when a month is missed, so a lapse is at least *visible* — but visible is not automatic.

**Decided — automate Hop B only. Hop A stays manual.** Hop A (the ensemble run) is the modelling
step; when it runs is a research decision, not a schedule. Automating the delivery removes the
failure people actually have — forgetting to deliver — without pretending the expensive half is
routine.

**Hop B is not deterministic, and that was the wrong reason to automate it.** An earlier draft of
this section claimed it was. `MONTHLY_REFRESH.md` says the opposite in its own words: Hop B ships
whatever the newest manifested run on the shelf is and clips the historical leg to the producer's
observed boundary, so *"when you run it determines what you get."*

The real justification is better: **that time-dependence is exactly what a monthly schedule wants.**
Running on the 22nd picks up whatever the 21st's compile produced and whatever the newest ensemble
run is. A deterministic step would be one that ignored the calendar, which is not what is wanted
here. What has to be true instead is that repeating it is *safe* — it is, in the sense that a
re-run supersedes rather than corrupts (newest-`$createdAt` wins), at the cost of another run
appearing in the store.

**Decided — it runs on the Hetzner box.** It is the only machine with conda, the memory, and network
reach to both Appwrite and the zarr host, and it already runs datafactory's monthly cron, so the
pattern exists. GitHub's hosted runners cannot do it.

The fate-sharing objection does not apply here: the *monitor* stays on GitHub, so the thing that
notices a failure is not the thing that failed. That separation is the point, and it is why
`data-freshness.yml` was not put on the box.

**Decided — failure surfaces as a heartbeat, matching datafactory.** A healthchecks.io ping on
success, with a period and grace window, answering *"did the delivery run?"* — the question the
existing daily monitor cannot answer, because it only sees staleness 45 days later. datafactory
already uses exactly this for its pipeline; this is the same mechanism for the delivery.

That leaves three signals with three distinct jobs, none alerting on another's failure:

| | question | when |
|---|---|---|
| Better Stack | is the service up? | 3 min |
| heartbeat *(new)* | did the delivery run? | monthly |
| `data-freshness.yml` | is what we serve still current? | daily |

**`SPEC NEEDED` — who owns the cron entry and the heartbeat account.** Operator question.

---

## What is already true, so nobody re-derives it

- The **compute is correct.** All 23 data routes were driven against the real artifacts; HDI
  invariants hold and an independent recomputation from the raw draws matches to `max|diff| = 0`.
  See `reports/architecture/what_the_api_actually_serves.md`.
- The **delivery mechanism is sound.** One launcher run uploads both the forecast and the historical
  artifact, behind one interlock derived from a declaration that carries a reason and a date.
- **Historical data arrives ~20th of the following month**, and datafactory compiles it on the 21st
  by cron. That is why a delivery runs on or after the 22nd. This is written down in
  `deployment/MONTHLY_REFRESH.md` and **nowhere else in the platform**.
- **A missed month is now visible** — `data-freshness.yml`, daily, opens one self-closing issue.

## What this roadmap does not cover

- Anything about **what the forecasts say**. That is step 1 and it is a research question.
- The 56 open register entries. Most are not on this path; the ones that are, are named above.
- views-faoapi, which shares an ancestor and several defects but is not this repo's problem.
