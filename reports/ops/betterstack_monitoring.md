# Better Stack — crafdapi monitoring (the owned record)

> **How to set the monitors up, check them and re-create them: `deployment/MONITORING.md` (#213).**
> This note is the older record of what was monitored and why. Where it disagrees with the guide
> or ADR-041 (the GitHub freshness job as the current check, `/health` being healthy before the
> first delivery, "when it fires, the delivery is missing"), the guide and ADR-041 are right:
> `/health` is `degraded` until a forecast is loaded, and a degraded `/health` has several causes.

*Per ADR-032 §3, the list of monitored targets + recipients lives in the repo, not only in the
vendor dashboard, so re-creating it elsewhere is minutes of work. crafdapi co-hosts with faoapi and
shares the **same Better Stack account**; each API adds its `/ping` as one more monitored URL.*

---

## What is monitored

| Monitor | Checks | Auth | Interval | An alert means |
|---------|--------|------|----------|----------------|
| **crafdapi /ping (liveness)** | `GET https://crafdapi.viewsforecasting.org/ping` returns 200 | none | 3 min | the **service** is down (outage) |
| **`data-freshness.yml`** (GitHub Actions, not Better Stack) | `GET /health` and reads its verdict — `status`, `appwrite_connected`, `forecast_freshness.is_stale` | `X-API-Key: $APPWRITE_DATASTORE_API_KEY` (GitHub repo secret of the same name) | daily 07:15 UTC | the **data** is stale — a monthly delivery was missed. Opens one self-closing issue |

**Two questions, two mechanisms, deliberately kept apart.** `/ping` answers *is the service up?*;
`data-freshness.yml` answers *is what it serves still current?* Neither alerts on the other's
failure — if `/health` is unreachable the workflow logs a notice and opens nothing, because that is
an availability event Better Stack has already alarmed on within three minutes. A duplicate alarm
for one event teaches people to ignore both.

The freshness half was a workflow rather than a Better Stack monitor because this note once said
Better Stack's free tier cannot inspect a response *body*. **That was wrong** (corrected
2026-11-22, #213): the free tier matches a keyword in the body and sends request headers, as
views-faoapi's `faoapi — forecast health` monitor has done since August 2026. `/health` returns
**200 with `status: "degraded"`** when the data is stale, so a keyword check on
`"status":"healthy"` catches it. ADR-041 moves the alarm to Better Stack; the setup is in
`deployment/MONITORING.md`, which also lists what a degraded `/health` can mean.

- **Created:** 2026-08-06 (crafdapi v0.1.0 deploy).
- **SSL/TLS verification:** on (catches an expired/broken cert on the public path).
- **Regions:** Europe / North America / Asia / Australia.
- **Recovery period:** 3 min · **Confirmation:** immediate.

## Alerting

- **Channel: email only.** Better Stack's free tier alerts by email; phone **Call**, SMS, and push are
  paid add-ons the plan does not have (see ADR-032 §3, corrected 2026-08-06). Louder channels are the
  paid / self-hosted upgrade path (ADR-032 §4).
- **Recipients:** the same primary responder as faoapi (see faoapi `reports/ops/betterstack_deployment.md` §4).

## Why `/ping`, not a forecast endpoint

`/ping` stays `200` even while the forecast endpoints return the **fail-visible 503** on an empty/stale
bucket (ADR-033). So this monitor tracks **liveness, not data-readiness** — it will not false-alarm
while crafd is serving 503 pre-first-delivery. A stale forecast must never masquerade as an outage.

## When it alerts — the response

An alert means `/ping` stopped answering. Fix on the **box**, never in Better Stack:

```bash
sudo systemctl status views-crafdapi --no-pager   # is it running?
sudo journalctl -u views-crafdapi -n 50           # why did it fall over?
sudo systemctl restart views-crafdapi             # the usual fix
```

If a recent release caused it, roll back per `deployment/RELEASE_RUNBOOK.md`. Recovery auto-resolves
the incident on the next check. crafdapi is a **separate service from faoapi** (`:8001` vs `:8000`) —
restarting one never touches the other.

## Deferred: the forecast-freshness monitor (`/health`, ADR-032 §2a)

Once crafdapi serves **real** data, add a second monitor on `GET /health` (content check: body must
contain `"status":"healthy"`; needs `X-API-Key`) — mirroring faoapi's `faoapi — forecast health`. Not
added now: pre-first-delivery `/health` is healthy but there is no forecast to be stale, so it would
have nothing to guard.

## References

- **ADR-032** — why Better Stack, the two monitor styles, the email-only channel, the revisit path.
- faoapi `reports/ops/betterstack_monitoring.md` / `betterstack_deployment.md` — the fuller shared
  response routine + account/recipient record (same account).
- **ADR-033** — fail-visible serving (why `/ping` stays green during a 503 window).
