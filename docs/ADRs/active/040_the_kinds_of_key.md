# ADR-040: The kinds of key in CRAF'd's Appwrite project

**Status:** Accepted. Amended 2026-11-21: the open question below is decided by ADR-044 (a third kind of key, the server's own read key `crafdapi-server`).
**Date:** 2026-11-17
**Deciders:** Simon (PRIO), Claude Code
**Supersedes:** ADR-038 (the scopes a caller key needs, written for the old shared project)
**Informed:** views-postprocessing, views-appwrite

---

## Context

CRAF'd's forecast now lives in its own Appwrite project (`6af4d7fa00255ed9cc67`). On 2026-11-17
that project had no keys at all, and no record said which keys it should have. ADR-038 listed the
keys of the old shared project, where one key could do several jobs and nobody could say which.

## Decision

There are two kinds of key in CRAF'd's project. There are no others.

1. **One delivery key, to write.** views-postprocessing uses it to put each month's forecast into
   the project. crafdapi never holds it, and it is never given to a partner.
   - Scopes: `buckets.read`, `files.read`, `files.write`, `tables.read`, `rows.read`, `rows.write`.
   - Not `buckets.write` or `tables.write`: the bucket and the table are made once, by hand.
   - Name: `delivery-` plus the month issued, for example `delivery-2026-11`.

2. **Reader keys, all alike, one per holder.** Each partner organisation gets one. VIEWS holds one
   too, as a test partner: with it we see exactly what a partner sees, before any real partner gets
   a key. Our reader key also runs our own checks (the release check and the daily freshness
   check), so there is no separate probe key.
   - Scopes: exactly `buckets.read`, `files.read`, `rows.read`. Each one is needed by a call the
     service makes with the caller's key: `buckets.read` by the key check
     (`managers/api.py`, `_validate_api_key`, which lists buckets), `rows.read` to find the latest
     forecast, `files.read` to read it.
   - Name: `partner-` plus the organisation plus the month issued. Ours is `partner-views-` plus
     the month, for example `partner-views-2026-11`.
   - Expiry: one year. How a key is created, sent, revoked and renewed is in
     `deployment/PARTNER_KEYS.md`.

Nobody but the delivery writes, so no reader key ever has a write scope.

## Open (decided 2026-11-21, ADR-044)

**Does the server need a key of its own?** Yes. The service loads the forecast at start and when a
new delivery appears, before any partner asks, so it reads storage with a third kind of key:

3. **The server's read key,** one per server, `crafdapi-server`.
   - Scopes: exactly `files.read` and `rows.read`, proved with a real request before it goes live.
   - Expiry: one year, not on the same day as FAO's server key (2027-10-29); the first is planned to
     expire on 2027-05-21.
   - Kept on the server in a file of its own, apart from the other settings, and in Bitwarden.

Partner (reader) keys now only decide who gets in; the service no longer reads storage with them.
They keep their three scopes, because the key check lists buckets (`buckets.read`); whether a
smaller set would do is not measured.

## Consequences

- Before a key is given to anyone, it is tried with `scripts/smoke.py`. A key missing a scope does
  not say so: the service answers a plain `401`, which looks like a revoked key. On a `401` from a
  new key, compare its scopes with this ADR first.
- A change that makes the service call Appwrite in a new way, with the caller's key, updates the
  reader scopes here in the same change.
- Keys are recorded in Bitwarden, never in this repository.

## Record

- 2026-11-17: the project had no keys (read in the console by the maintainer). This ADR was agreed
  the same day, before any key was made.
