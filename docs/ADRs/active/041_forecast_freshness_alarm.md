# ADR-041: The alarm for an old forecast lives in Better Stack, not in GitHub

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** ADR-032 (uptime monitoring), ADR-040 (the kinds of key), an eight-perspective expert
review (2026-11-18), views-faoapi's Better Stack setup
**Informed:** whoever maintains this service's monitoring

---

## Context

Two things can go wrong that a maintainer must hear about:

1. **The service is down.** Better Stack checks `/ping` every 3 minutes and emails when it stops
   answering (ADR-032).
2. **The service is up, but serves an old forecast,** because a monthly delivery did not arrive.
   `/ping` cannot see this, and `/health` still answers `200`: only the text of its answer says
   `"status":"degraded"`.

Since August 2026 the second case has been watched by a daily GitHub Actions job,
`.github/workflows/data-freshness.yml` (404 lines). It reads `/health` with a key stored as a
GitHub secret, and opens, comments on and closes GitHub issues. It was built because this
repository's notes said Better Stack's free plan cannot read the text of an answer. That was wrong:
views-faoapi runs exactly that check on the same free plan (its monitor `faoapi — forecast health`).

The job also ties the alarm to GitHub's scheduler, secrets and issue tracker. GitHub is this
project's home for code; the maintainer does not want it to be the home for operations too.

## Decision

1. **Better Stack watches both cases.** A second monitor checks `GET /health` every 3 minutes,
   sends our own reader key in the `X-API-Key` header, and alerts when the answer does not contain
   `"status":"healthy"`.
2. **The key is VIEWS's own reader key** (`partner-views-...`, ADR-040). It is read-only and holds
   no partner's identity. It is stored in the monitor's settings at Better Stack.
3. **The GitHub job is deleted** once the monitor is live. GitHub holds code, tests and pull
   requests; it raises no operational alarms.
4. **The rule for "too old" stays in crafdapi** (`/health`, `forecast_freshness`). The monitor only
   compares one word, so it can be moved to another tool in minutes.

This was chosen on its own merits: it needs no new code, it works on the plan we have, and it has
run for views-faoapi since August 2026. That faoapi does the same is evidence it works, not the
reason.

## When

At the switch-over of the delivery to CRAF'd's own Appwrite project, together with #208 (the server
loads the forecast when it starts). Two reasons:

- the job's key belongs to the old shared project and is refused after the switch-over;
- `/health` reports the forecast the server has loaded. Until #208, a newly delivered forecast reads
  as old until something loads it, so the monitor would raise a false alarm after each delivery.

Until then the GitHub job runs unchanged.

## Consequences

- One place for every alarm about this service: Better Stack email.
- About 400 lines and one GitHub secret fewer.
- A read-only key is stored at an outside company (EU, ADR-032). If Better Stack is left, that key
  is revoked and a new one made.
- The monitors and their settings are written down in this repository, so they can be re-created
  elsewhere (ADR-032 §3). The setup guide is `deployment/MONITORING.md` (#213).
- This supersedes the freshness part of ADR-032 §2a for crafdapi, and the GitHub job described in
  `reports/ops/betterstack_monitoring.md`.
