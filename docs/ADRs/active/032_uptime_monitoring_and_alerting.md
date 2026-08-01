# ADR-032: Uptime Monitoring and Alerting for the Live API

**Status:** Accepted
**Date:** 2026-07-20
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** register C-85 (no production monitoring), C-79 (infra/endpoint exposure), ADR-022 (deployment), ADR-008 (observability & explicit failure); the 2026-06-29 outage post-mortem; a `/falsify` audit of the tool choice and an 8-perspective expert review (both 2026-07-20)
**Informed:** UN FAO / FSFC consumers; deployment operators; the owners of the other planned VIEWS APIs (this is their template); views-datafactory (a follow-up review is requested — see "Relationships")

---

## Context

The live UN-facing API (`faoapi.viewsforecasting.org`) had no external uptime monitoring: when it went down on 2026-06-29, nobody was alerted — we learned of it by hand. Register **C-85** tracks this gap. The code half is done (an unauthenticated `GET /ping` liveness probe, designed to be polled from outside with no auth or Appwrite dependency); the remaining half is choosing and running the thing that polls it and alerts us.

**Two monitoring styles, because the choice turns on the difference:**
- **Polling** — an outside service repeatedly visits our `/ping` on a schedule and raises an alert if it stops answering. This is the right shape for an **always-on service** (like this API), which has nothing that naturally "reports in."
- **Heartbeat** — a job phones an outside service when it finishes, and that service alerts us if the expected call never arrives. This is the right shape for a **scheduled batch job** (like views-datafactory's monthly pipeline), which runs and completes.

This ADR is about **polling**. A tool built only for heartbeats cannot watch an always-on API, and vice-versa — which is why one platform-wide tool does not automatically fit both jobs.

Two facts shaped how carefully we made this choice:

1. **We nearly repeated our own mistake.** A `/falsify` audit found we had no written definition of "best" — we were leaning on an unverified recommendation. It surfaced that our leading candidate (UptimeRobot's free tier) **bans commercial/organizational use as of December 2024**, and that a strong option (self-hosted Uptime Kuma) had never been considered. This ADR exists partly so the decision is grounded in criteria, not vibes.

2. **The two shapes pull in opposite directions.** An 8-perspective expert review split cleanly:
   - **Own it / self-host** (open, no lock-in, EU, focused — matches PRIO's Hetzner ethos and the Clean Architecture instinct to keep vendors at arm's length): favoured by the values lens (Hickey's *simple-over-easy*, Kleppmann's data-ownership, Martin's dependency boundaries).
   - **Rent it / SaaS** (zero-upkeep, independent vantage): favoured by the operations lens — most sharply **Nygard's "who watches the watchman?"**: a self-hosted monitor the team must keep alive, especially on shared infra, is *another unsupervised process that can die unnoticed* — the exact failure class that caused the June outage.

The deciding factor is not philosophy but our operational reality: a very small team that just lived through an unsupervised-process outage should not make its safety net another unsupervised process.

---

## Decision

### 1. What "best" means here (the criteria)

**Gates — a "no" eliminates a tool:**
- **G1.** Actively polls an external HTTPS URL on a schedule (rules out heartbeat-only tools like healthchecks.io).
- **G2.** License/terms permit an organisational, UN-facing service (rules out UptimeRobot's free tier — commercial-use ban).
- **G3.** Alerts by at least email, to more than one person (so one person being away can't leave an outage unseen — the June failure was "nobody knew").

**Weighted preferences (highest first):**
- Low upkeep — nothing the team must run and patch.
- **Stay unentangled** — no lock-in; leaving must be cheap (Clean Architecture: the monitor is a *detail*, kept replaceable behind a boundary we own).
- Independent vantage — the monitor must survive the thing it monitors dying.
- EU / data-residency fit (PRIO/GDPR; cross-ref C-79, which is about not exposing infra detail).
- Scales cheaply to the other planned APIs.
- Focused scope — a tool that watches things, not a sprawling platform we grow into.

### 2. The choice, now: an external SaaS poller (Better Stack)

We register `GET /ping` on **Better Stack**, verified against vendor documentation (2026-07-20) to clear all three gates on its **free tier**:

- **G1 (polling):** 10 uptime monitors at 3-minute checks. (It also offers heartbeats, so a single account *could* later cover batch jobs too — relevant to the platform, not required here.)
- **G2 (license):** its free plan **explicitly permits commercial / organisational use** — the direct opposite of UptimeRobot's ban, and the reason it clears the gate that eliminated UptimeRobot.
- **G3 (multi-recipient alerts):** email and Slack alerts to **unlimited team members** at no cost. (Richer on-call "responder" rotations are a paid add-on at ~$29/responder/month; we do not need them for basic multi-person email alerting.)

It is also **EU by default**, which is verified fact rather than assumption: Better Stack is headquartered in Prague (Czech Republic) and, by default, stores monitoring data in **EU data centres** under GDPR, ISO/IEC 27001, and SOC 2 Type II, with a Data Processing Agreement available. So our endpoint and uptime history stay within EU jurisdiction — meeting the residency preference (cross-ref C-79) with evidence, not hope.

We use **only** its uptime feature; the rest of its platform (logs, incidents, on-call, status pages) is deliberately left untouched (§3).

**Why SaaS over self-hosting, given our values lean the other way:** the monitor's whole purpose is an **independent vantage point**. A SaaS provides that for free. A self-hosted monitor only provides it if it runs on genuinely separate infrastructure *and* the team actually keeps it alive — two things we cannot honestly guarantee today. For our present situation the **operations concern (an untended monitor is another thing that can fail unnoticed) outweighs the purity concern (own everything ourselves)**: don't let the safety net become the next unsupervised process. (In the expert review this is Nygard's "who watches the watchman?" winning over Hickey's "own it, stay unentangled" — the latter is right in principle and preserved as the revisit path in §4.)

### 2a. A second monitor — forecast freshness (S3 / #246, C-50)

Beyond the `/ping` liveness poll, add a **content-check monitor** on the authenticated `GET /health`. It returns HTTP 200 while the *service* is up, but its body carries `"status": "degraded"` and a `forecast_freshness` block when the **served forecast is older than the freshness SLA** (default **45 days**; override with env `CRAFDAPI_FORECAST_FRESHNESS_SLA_DAYS`). Configure the Better Stack monitor to **alert when the response body does _not_ contain `"status":"healthy"`** (equivalently, contains `"is_stale":true`). This pages when a monthly forecast stops refreshing — closing **C-50**, where a 139-day-old artifact previously served with green health. It needs an API key (keep it in the monitor's request headers). HTTP status stays about *service* health (503 iff Appwrite is unreachable), so a stale forecast never masquerades as an outage. *(The freshness verdict is also queryable at `GET /provenance/forecast`.)*

### 3. Stay unentangled — the boundary that makes SaaS safe

Renting is acceptable **only because we keep the decision reversible**:
- The list of monitored targets and alert recipients lives as a short, owned record in this repo (an ops note), not only in the vendor's dashboard — so re-creating it elsewhere is minutes of work.
- We use **only** the uptime feature; we do not build on the vendor's logs/incidents/on-call/status-page surface, so we grow no dependency on their ecosystem.
- Consequence: leaving Better Stack is a short, one-sitting task — re-creating a handful of URL checks and their recipients in another tool — not a migration project. This is the Clean Architecture principle applied literally — the vendor is a detail at arm's length, not an architectural commitment.

### 4. The revisit-when path (self-hosted, deferred not rejected)

Self-hosted **Uptime Kuma** (open-source, focused, EU-hostable, no lock-in — the philosophically cleaner fit) becomes the preferred option **when, and only when, two facts change together**:
1. the monitor has **clear ownership** — a named person responsible for running and patching it, with cover when they are away (the same bus-factor test as G3, so the watcher itself can't become the next untended process), **and**
2. it can live on a genuinely independent host — a separate box, ideally a different provider, so it cannot share faoapi's fate.

The governing invariant, kept forever: **a monitor must be able to survive the thing it monitors dying.** SaaS passes this by default; self-hosting passes it only with independent infra plus upkeep we will actually do.

**Added payoff of the revisit (recorded 2026-07-20):** self-hosted Uptime Kuma also unlocks **personal-messenger alert channels — Signal, Telegram — that Better Stack does not offer natively.** This is a genuine bonus of the revisit path, *not* a reason to self-host now: email + phone-call alerting already meets the need loudly, so a preferred messenger channel does not justify taking on the operational cost today. It is banked as one more thing the self-hosted move buys once its two conditions are met.

### 5. Scope

This decision covers faoapi now and is the **template for the other planned VIEWS APIs** — each new API adds its `/ping` as one more monitored URL under the same account and the same reversibility discipline. It does **not** change views-datafactory, whose healthchecks.io heartbeat is the correct tool for a monthly batch job (a separate follow-up asks that repo to review, not a mandate — see Relationships).

---

## Consequences

**Good**
- C-85's operational half closes: downtime is detected and alerts fire to more than one person, from an independent vantage.
- No new unsupervised process; nothing added to the team's patch burden.
- Reversible by construction — no ecosystem lock-in despite using a SaaS.
- A reusable, criteria-backed template for every future API.

**Costs / accepted trade-offs**
- We give up, *for now*, the full "own-it" ideal — mitigated by the reversibility discipline (§3) and the documented revisit path (§4).
- A free-tier cap (Better Stack: ~10 monitors) will eventually need review as APIs multiply; that review is a known future trigger, not a surprise.
- **Alert channels are email + phone call** (Slack/Teams deliberately unused; no native Signal/WhatsApp on Better Stack). Accepted now because a phone call is the loudest possible signal and email covers the multi-person requirement; personal-messenger channels are deferred to the self-hosted revisit (§4).
- Some data (our endpoint URL, uptime history) sits with an EU SaaS; weighed against C-79 and judged acceptable for a *liveness* endpoint that exposes nothing an outside observer couldn't already probe.

**Ongoing**
- Keep the monitored-target/recipient list current in-repo.
- Re-open this ADR if a gate is violated (e.g. a terms change) or the two revisit-when facts (§4) become true.

---

## Alternatives considered

- **healthchecks.io** — heartbeat-only (the service phones home). Correct for batch/cron (datafactory uses it well), but it cannot poll an always-on API. Fails G1.
- **UptimeRobot (free)** — generous (50 monitors) and capable, but **bans commercial/organisational use since Dec 2024**. Fails G2. (Its own 2024 terms change is, ironically, a live example of the lock-in/rug-pull risk that motivates §3.)
- **Uptime Kuma, self-hosted, now** — the values-optimal choice (open, focused, EU, no lock-in), but self-hosting a monitor a very small team must keep alive — especially on shared infra — recreates the unsupervised-process failure of June. Deferred to §4, not rejected.
- **Do nothing / manual checks** — the status quo that produced the outage. Rejected.

---

## Relationships

- **Closes the operational half of C-85** (the code half — `/ping` — already landed under epic #184 S2).
- **Cross-refs C-79** (infra/endpoint exposure) and **ADR-008** (observability & explicit failure); **ADR-022** (deployment) is the sibling that owns how the service runs.
- **Grounded in** the 2026-07-20 `/falsify` audit and expert review; the audit's failing-test stubs (`tests/test_monitoring_decision.py`) turn green once this ADR is accepted.
- **Follow-up (deferred until this ADR is accepted):** open a views-datafactory issue inviting it to review whether its monitoring should evolve in light of this decision — framed as a prompt to reconsider, not a mandate; its heartbeat choice remains correct for a batch job.
