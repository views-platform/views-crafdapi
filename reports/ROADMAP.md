# Roadmap

**Definition of done:**

> **CRAF'd receives forecasts worth citing, every month, without anyone remembering to.**

*Set 2026-08-24, after the previous definition of done — EPIC #40, "CRAF'd can actually use this
API, proven with real data" — was completed and closed. This document is deliberately unfinished:
the four steps are agreed, the **hard specs are not**, and every place one is missing is marked
`SPEC NEEDED` rather than papered over with a plausible-sounding sentence.*

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

## Step 1 — Replace the placeholder ensemble

**The only step that changes what CRAF'd actually receives.** Modelling work, not plumbing; the one
step no amount of engineering here substitutes for.

Today `rusty_bucket` is eight clones of the `heavy_strider` datafactory baseline.

- `SPEC NEEDED` — **What replaces it?** A named ensemble, or a named model.
- `SPEC NEEDED` — **What makes a forecast "worth citing"?** This is the load-bearing phrase in the
  whole definition of done and it currently means nothing testable. Candidates, none chosen: a skill
  score against a stated baseline over a stated evaluation window; a named person's sign-off;
  passing an existing evaluation harness. Until this is written down, step 1 cannot be declared done
  by anyone but you, on the day, by opinion.
- `SPEC NEEDED` — **Who decides, and against what evidence?**
- `SPEC NEEDED` — **Does ADR-034 have to be Accepted first?** It is still `Proposed` — *"awaiting
  CRAF'd/product sign-off on the target list"* — and its exceedance thresholds
  (`p_gt25`/`p_gt100`/`p_gt1000`) are placeholders whose confirmation would **rename** those served
  columns. Shipping a real forecast under placeholder thresholds is a decision, not an oversight.

**Not blocked by anything in this repo.**

## Step 2 — Run one real monthly delivery, end to end

Proves the manual path works before anyone automates it. The two blockers that made this impossible
were cleared 2026-08-23/24 (views-models#399, #403), and the procedure is written down in
`deployment/MONTHLY_REFRESH.md`.

- `SPEC NEEDED` — **What counts as proof it worked?** Proposed, for agreement: a *new* `run_id` in
  `/provenance/forecast`; `/health` healthy with `is_stale: false`; `smoke.py` ALL PASS; and the
  bulk artifact's byte count **changing** from 461,991 — because an unchanged byte count would mean
  the same run is still being served.
- `SPEC NEEDED` — **Must the `*_actual` column be populated?** A delivery run after the 20th should
  carry the previous month's observations, so this is the first chance to see that column carry real
  numbers rather than zeros (register **C-293**). Whether that is a pass condition or an observation
  is not decided.
- `SPEC NEEDED` — **Who runs it, and on what?** The launcher needs conda, several GB, and network
  reach to Appwrite and the datafactory zarr host.

## Step 3 — Fix the two broken served surfaces

Tracked as **#125**. Not required by CRAF'd today, but the API cannot honestly be called
consumer-ready while they stand.

- `/data/{category}/latest` returns rows carrying **no values** behind `HTTP 200` (**C-232**,
  Tier 1, open since 2026-08-10). Measured live: 88 MB of rows containing only `month_id` and
  `priogrid_id`.
- **No data route has any size bound** (**C-284**). `GET /pg/data/historical/subset` with no query
  string asks for 34.5 GB — more than the machine has.

- `SPEC NEEDED` — **Retire `/latest`, or bound it?** C-284 calls retirement *"probably right and the
  largest change"*. Retiring is a breaking change to a documented public route; bounding keeps it
  and costs a size gate on 23 routes.
- `SPEC NEEDED` — **What is the bound?** Not a row count: a historical row costs ~1,625 B through
  the serializer and a forecast row ~7,731 B, and sample width varies per delivery. It has to be
  estimated bytes from dataset shape.
- `SPEC NEEDED` — **Does `METHODOLOGY_VERSION` bump?** Currently `3`. **C-244** argues the nine
  added exceedance columns warrant it under ADR-023. It changes a value CRAF'd reads.

## Step 4 — Automate the monthly run

Today it is two commands a human must remember on the 22nd. `data-freshness.yml` now opens an issue
when a month is missed, so a lapse is at least *visible* — but visible is not automatic.

- `SPEC NEEDED` — **Where does it run?** Needs conda, several GB of memory, and network reach to
  Appwrite and the zarr host. GitHub's hosted runners will not do it. The Hetzner box already runs
  datafactory's monthly cron and could host this too — at the cost of the monitor sharing fate with
  the thing it monitors, which is the objection that put `data-freshness.yml` on GitHub rather than
  the box.
- `SPEC NEEDED` — **What happens on failure, and who is told?** datafactory uses a healthchecks.io
  heartbeat for *"did the monthly pipeline run?"*. There is no equivalent for the delivery.
- `SPEC NEEDED` — **Does Hop A (the ensemble run) automate too, or only Hop B (the delivery)?** They
  are separate commands in separate places and only Hop B is cheap.

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
