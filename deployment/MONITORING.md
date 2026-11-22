# Monitoring: setting up crafdapi's two Better Stack monitors

*For the maintainer. How to create, check and re-create the two monitors that watch crafdapi. The
decisions behind them are ADR-032 (Better Stack, email alerts) and ADR-041 (the old-forecast alarm
lives in Better Stack). This page holds no key and no email address: keys are in Bitwarden,
recipients are in Better Stack.*

crafdapi has two alarms, for two different questions:

| Monitor | Question | Check | Key |
|---|---|---|---|
| `crafdapi — liveness` | Is the service up? | `GET https://crafdapi.viewsforecasting.org/ping` answers `200` | none |
| `crafdapi — forecast health` | Is what it serves current and complete? | `GET https://crafdapi.viewsforecasting.org/health` answers with a body containing `"status":"healthy"` | our own reader key, `partner-views-...` (ADR-040) |

`/health` answers `200` even when the forecast is old, missing or incomplete; it says so in its body
(`"status":"degraded"`). That is why the second monitor reads the body, and why the first one does
not: an alert from `liveness` always means the service is down, never that the data is old.

Both monitors live in the same Better Stack account as views-faoapi's (account details: views-faoapi
`reports/ops/betterstack_deployment.md`, section 2). The free plan is enough: it checks every 3
minutes, reads a body for a keyword, sends custom request headers, and alerts by email.

## 1. `crafdapi — liveness`

This monitor already exists (created 2026-08-06, listed as "crafdapi /ping (liveness)" in
`reports/ops/betterstack_monitoring.md`). Do not create a second one: two monitors on one URL send
two emails per outage. Open it, check its settings against the list below, and rename it
`crafdapi — liveness`. Create it with these settings only when re-creating it (a new account, or
another tool).

In Better Stack: **Monitors → Create monitor**.

1. **Alert us when:** the URL becomes unavailable.
2. **URL:** `https://crafdapi.viewsforecasting.org/ping`
3. **Check every:** 3 minutes.
4. **SSL/TLS verification:** on (it also catches an expired certificate).
5. **Regions:** Europe, North America, Asia, Australia.
6. **Recovery period:** 3 minutes. **Confirmation period:** immediate.
7. **On-call / who to alert:** the maintainer by email (and anyone else who should hear about an
   outage; see section 4).
8. **Name:** `crafdapi — liveness`. Save.

It should turn green within a few minutes.

## 2. Create `crafdapi — forecast health`

Create it **only after the first forecast delivered into CRAF'd's own Appwrite project has been
loaded** (ADR-041, "When"). Before that, `/health` honestly reports `degraded` (nothing loaded, or no
recorded deaths), and the monitor would alarm from the start.

**Monitors → Create monitor**:

1. **Alert us when:** the URL does not contain a keyword.
2. **URL:** `https://crafdapi.viewsforecasting.org/health`
3. **Keyword:** `"status":"healthy"` (with the quotes and no space, exactly as `/health` writes it).
   The monitor alerts when the keyword is **absent**: a degraded answer, an error, or no answer.
4. **Request headers** (under the advanced settings): name `X-API-Key`, value our reader key
   `partner-views-...`, copied from Bitwarden. Never paste it anywhere else.
5. **Check every:** 3 minutes. **SSL/TLS verification:** on. Same regions and recovery as the
   liveness monitor, but a **confirmation period of 10 minutes**: after every restart (each
   release) the service answers before its first load is done, and `/health` reads `degraded`
   (`forecast_loaded: false`) for under a minute from the disk cache, a few minutes after a new
   delivery. Without the delay, every release would send a false alarm.
6. **On-call / who to alert:** the same people as for liveness.
7. **Name:** `crafdapi — forecast health`. Save.

Before saving, check that the key works, on your own machine, in bash. This line prompts for the
key; paste it alone:
```bash
read -rsp "key: " KEY; echo
```
```bash
curl -s -H @<(printf 'X-API-Key: %s\n' "$KEY") https://crafdapi.viewsforecasting.org/health; unset KEY
```
The body must contain `"status":"healthy"`. (The header is passed from a file descriptor, so the
key does not appear in the process list.)

## 3. Check that an alert actually reaches you

A monitor that never alerts proves nothing. Once for each monitor:

1. Edit the monitor and break it on purpose: for `forecast health`, change the keyword to
   `"status":"never"`; for `liveness`, change the URL to
   `https://crafdapi.viewsforecasting.org/ping-does-not-exist`.
2. Wait: up to 6 minutes for `liveness`, up to 15 for `forecast health` (its 10-minute
   confirmation comes first). An email must arrive at every recipient.
3. Put the setting back. A recovery email must follow within about 6 minutes.

If someone listed as a recipient gets nothing, they are not receiving alerts. One possible reason,
not yet confirmed: on the free plan only "responders" may be alerted, not "viewers" (an open check
in views-faoapi `reports/ops/betterstack_deployment.md`, section 4).

## 4. When an alert arrives

- **`liveness` alerts:** the service is down. On the server, follow `RELEASE_RUNBOOK.md` section 2
  ("The API is down").
- **`forecast health` alerts while `liveness` is green:** the service is up but its data is not
  right. `/health` says which:
  - `forecast_freshness.is_stale` true: the forecast is older than the freshness limit; a monthly
    delivery was missed (`RELEASE_RUNBOOK.md` section 3).
  - `forecast_loaded` false: expected for a few minutes after a restart (the monitor's 10-minute
    confirmation covers it); longer, and the service cannot load: read the service log on the
    server (`sudo journalctl -u views-crafdapi -n 100`).
  - `forecast_loading` present: the newest delivery could not be loaded; the previous one is
    served. Read the service log.
  - `history_loaded` false: the recorded deaths are missing, so bulk files and slices of past
    months answer 503 until the history loads.
  - `forecast_serving_state` with `degraded`: the newest delivery was refused and the last good one
    is served (ADR-033); the log names the reason.
  - No JSON body at all, or `{"detail": ...}`:
    - **401:** the reader key in the monitor's header was refused: expired or revoked. Put the
      current key in the header (section 5).
    - **503 `Metadata store unavailable`:** Appwrite could not be asked while the key was checked
      (for example just after a restart, when no key check is remembered yet).
    - **503 `Service unhealthy`:** `/health` itself failed; read the service log.
    - **500 `Server configuration error`:** a required `APPWRITE_*` setting is missing on the
      server (`RELEASE_RUNBOOK.md` section 4).
- **`forecast health` alerts and `liveness` too:** it is an outage; start with `liveness`.

Not always caught: Appwrite answering errors other than a refused key. While the key's check is
remembered, `/health` still reads `healthy` (it reports `appwrite_connected: false` without
degrading); when it is not, `/health` answers 503 and the monitor alerts. The loader logs its failed
checks either way (#238).

## 5. Keep it working

- **When our reader key is renewed** (once a year, `PARTNER_KEYS.md`), update the `X-API-Key`
  header in `crafdapi — forecast health` the same day, before the old key expires, and repeat the
  curl check in section 2.
- **Once the forecast-health monitor is live** (and section 3 showed its alert arrives), the
  GitHub job it replaces,
  `.github/workflows/data-freshness.yml`, is deleted (ADR-041, decision 3; story #214).
- **To move away from Better Stack:** create the same two checks in another tool (a URL that must
  answer, a URL whose body must contain `"status":"healthy"` with one request header), then delete
  these. Revoke the reader key stored at Better Stack and make a new one (ADR-041). The rule for
  "too old" lives in crafdapi, not in the monitor, so nothing else moves.
