# ADR-044: The service loads the forecast itself, with a read key of its own

**Status:** Accepted
**Date:** 2026-11-21
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** the views-appwrite repository (its key registry and the contract between the
platform's storage and the APIs that read it); Nygard, *Release It!* (1st edition, 2007), sections
10 and 12.1–12.2; Hohpe and Woolf, *Enterprise Integration Patterns* (2003), chapter 8; Newman,
*Building Microservices* (2nd edition, 2021), chapter 5
**Informed:** partners; views-faoapi, which reads its storage the old way

---

## Context

Until this decision the service read the forecast from storage with the key of whichever partner
was asking. Each partner key did two jobs at once: it let the partner in, and it was the credential
used to read storage. The forecast was loaded on the first request after it arrived, so that
request waited for the whole load, which can take minutes. The web server in front of the service
gives up after 60 seconds by default, so that first partner would most likely have seen an error.
Each key's loaded data was also kept apart from every other key's, on disk and in memory, although
every partner is served exactly the same forecast.

On 2026-11-17 Simon decided that no partner should ever wait for a load: the service loads the
forecast itself, when it starts and when a new one arrives, before any partner asks. At start there
is no partner and so no partner key. The service therefore needs a key of its own.

This question had been open in two places: in ADR-040 (the kinds of key), and in the platform's
storage contract in views-appwrite. That contract asks whether an API should check the caller's key
and then read storage with its own key, and says that going on with the caller's key was "a
deferral, not a decision".

## Decision

### 1. The server has its own read key

The service holds one read key of its own, named `crafdapi-server` in the platform's key registry
(views-appwrite). It loads the forecast and the recorded deaths with this key and with no other.

- **Permissions:** exactly two, `files.read` and `rows.read`: one to list the newest deliveries,
  one to download them. The two are proved by the two requests themselves, not by a slice that
  answers: after the key is installed, the service log shows one line for the listing
  (`loader: store listing: newest forecast manifest ...`, which must name a manifest, not
  `NONE`) and one per download (`downloaded ...: N bytes`). Reading those lines once, on the
  server, with the new key, is the proof.
- **Expiry:** one year, and not on the same day as FAO's server key, which expires on 2027-10-29.
  Keys that all expire together turn one missed renewal into an outage of both services. The first
  key is planned to expire on 2027-05-21.
- **Where it is kept:** on the server, in a file of its own
  (`/home/views-crafdapi-deploy/.env.crafdapi-server-key`), separate from the other storage
  settings, readable only by the service account, under the setting name
  `APPWRITE_CRAFDAPI_SERVER_API_KEY`. It is also kept in Bitwarden. It is never written in a
  repository and never shown in a log.

### 2. Partner keys only decide who gets in

A partner's key is checked with storage. A key found valid is remembered for one minute, so a key
deleted in Appwrite stops working within a minute, which is what the service specification
promises ("it stops working within minutes"). A partner key is no longer used to read any data.

### 3. The service loads the forecast before anyone asks

At start, and then every five minutes, the service asks storage whether a new forecast or new
recorded deaths have been delivered. When something is new, it:

1. loads the forecast and the recorded deaths with the server key, in the background, while
   requests go on being answered;
2. builds the nine bulk files (ADR-043);
3. only then switches every request to the new data, in one step.

Partners are always answered from data that is already loaded. If a load fails, the data already
loaded stays in service, the health check (`/health`) reports `degraded`, and the next check tries
again. If the newest forecast is refused (the last good one is served instead), or the recorded
deaths cannot be loaded, the check tries again at most once an hour, not every five minutes: a
refused forecast is several gigabytes to download.

The bulk files of the previous delivery are kept until the next new delivery, because a download
that started just before the switch may still be reading one.

Until the first load after a start is complete, data requests answer `503` with "No forecast is
available right now. Try again later." From the disk cache this takes under a minute (an
estimate); a new delivery takes a few minutes, during which the previous forecast is still served.

### 4. One loaded copy for everyone

All partners are served the same loaded forecast. This is intended: the service specification says
every partner receives the same data, and keys exist only so that one partner can be cut off. If
partners ever had to see different data, this decision would have to be undone, and the data kept
apart per partner again. Who notices: whoever changes that sentence of the service
specification. A test (`tests/test_uniform_entitlement.py`) fails when the sentence changes, and
its message points here. The platform's shared test for keeping callers' cached data apart has a
mode for this case ("uniform entitlement"), which cites the service specification.

### 5. Every answer says which month the forecast was made in

A slice carries `forecast_made_in` (for example `"2026-07"`); a bulk file carries the response
header `X-Forecast-Made-In`. It is the forecast's first month: the newest recorded month is the
month the forecast is made in, and the forecast starts there.

## Reasons

- **It is the only way to meet "no partner waits".** Loading before anyone asks needs a key that
  does not belong to a request.
- **Each key does one job.** A key that lets someone in and a key that reads storage are different
  things. Hohpe and Woolf (chapter 8) describe keys given to an outside party as a way to restrict
  what that party can do; Newman (chapter 5) puts key management at the edge of a service, apart
  from how the service fetches its data.
- **Least privilege.** Nygard (section 12.1): "a process should have the lowest level of privilege
  needed to accomplish its task." The server key gets the two permissions its reads need, not the
  three a partner key has.
- **A stored credential is kept apart.** Nygard (section 12.2): a password stored in a text file
  "is vulnerable", and he keeps such credentials separate from other configuration. Hence the file
  of its own.
- **A cold start is a known failure.** Nygard (section 10): "cache warmup can cause initial requests
  to take minutes".
- **It follows a working example.** views-faoapi's server already holds a key of its own,
  `faoapi-server`, with read permissions only and a one-year expiry.

## Consequences

- **A key must be created before this version is released,** or the service starts but serves
  nothing (every data request answers `503`). The steps are in `deployment/RELEASE_RUNBOOK.md`,
  section 5.
- **The server key must be renewed once a year.** Renewal: create the new key, put it on the
  server, restart, check, then delete the old one.
- **A deleted partner key works for up to one more minute.**
- **Health checks (`/health`) report the forecast actually being served,** say whether anything
  is loaded yet (`forecast_loaded`), and report `degraded` when nothing is loaded or the newest
  delivery could not be loaded.
- **At shutdown a running load is asked to stop** and is waited for up to one minute.
- **The per-partner copies of the data go away in practice:** everything is loaded under the
  server key. The code that kept per-key copies remains and can be removed later.
- **The bulk files no longer make anyone wait:** they are built before a forecast is served
  (ADR-043, consequence 1, no longer applies).
- **views-faoapi still reads storage with its callers' keys.** Whether it follows, or records why
  not, is decided on its side.
- **The loading runs in a background thread while requests are answered.** Two threads must never
  use the same pandas objects at once, because a pandas index builds its lookup table on first use
  and two threads doing that together read a half-built table (measured on 2026-11-20: 8 wrong
  answers in 30). The loader therefore hands over data read freshly from disk, which no request
  has touched.

## Where this lives in the code

- `src/views_crafdapi/managers/forecast_loader.py`: the loader, the server key, the checks every
  five minutes.
- `src/views_crafdapi/managers/api.py`: the partner key check (`_check_partner_key`) and the
  routes, which answer from `_served()`.
- `deployment/views-crafdapi.service` and `deployment/bootstrap.sh` (`serverkey`): the key's file.
