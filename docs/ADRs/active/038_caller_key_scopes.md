# ADR-038: The Appwrite scopes a caller key needs, derived from the code

**Status:** Proposed
**Date:** 2026-10-20
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** ADR-027 (authentication and per-key isolation), ADR-031 (credential model), the views-faoapi session (its `deployment/KEY_SPLIT_AND_ROTATION_RUNBOOK.md` and `tests/test_auth_probe_scope.py`), the Appwrite console (read 2026-10-20)
**Informed:** deployment operators

*Numbering: ADR-036 and ADR-037 are taken by PR #152 (delivery architecture), not yet merged.*

---

## Context

ADR-027 delegates authorization to Appwrite: a caller's `X-API-Key` is handed straight to the
Appwrite client, so what a caller can read is exactly what their key is scoped to. ADR-027 §5 then
puts "the Appwrite key's own scope design" out of its scope, as "an Appwrite-console concern". No
other record took it up. The result, found on 2026-10-20 (S0 #155):

- Nobody had written down which scopes a caller key needs. CRAF'd's key was scoped by hand in the
  console, and nothing records why it has the scopes it has.
- A new smoke-test key could only be scoped by copying CRAF'd's, which only repeats an unchecked
  choice.
- views-faoapi learned the cost of guessing: a key built exactly as its runbook said answered 401
  on every request, because the service's key check needed a scope the runbook never granted. The
  failure looked like a revoked key, not a missing scope.

This ADR records the scopes the code actually needs, and how they were found.

---

## Decision

### 1. A caller key needs exactly three scopes

Derived on 2026-10-20 by tracing every Appwrite SDK call reachable from a serving route with the
caller's key. All of them are reads; no serving route writes, and nothing runs a create-if-missing
step at startup or on a key's first request.

| Scope | SDK calls | Why |
|---|---|---|
| `buckets.read` | `storage.list_buckets` (`managers/appwrite/manager.py`, from `_validate_api_key` and the `/health` probe) | the check run on each new key's first request; without it every authenticated route answers 401 |
| `documents.read` | `databases.list_documents` (`managers/appwrite/metadata.py`, `search_files_by_metadata`) | find the latest forecast run, its shards and sidecar, the historical file, and provenance |
| `files.read` | `storage.get_file`, `storage.get_file_download`, `storage.list_files` (`managers/appwrite/manager.py`) | read and download the delivered files; `/files/*` |

The writes in `managers/appwrite/` (`create_*`, `update_document`, `delete_*`) and the reads only
they use (`databases.list`, `list_collections`, `list_attributes`, `storage.get_bucket`) are
reachable only from upload code and tests. `tests/test_serving_isolation.py` keeps the write and
provisioning methods off the serving modules. It does **not** cover the upload-only reads
(`get_bucket`, `debug_collection_attributes`, the metadata `list`/`list_collections` paths), so for
those §3's rule is the only guard.

`databases.read` is not needed by any traced call. Confirmed 2026-10-20: `scripts/smoke.py
--expect-tag v0.7.0` against production with `crafdapi-probe`, created holding only these three
scopes, passed the key
check (`appwrite=True`), forecast and historical coverage for IDN (1,030 cells each) and the warm
check. Its one failure, `/health` reporting `degraded`, was the stale forecast (#153), not the key.

### 2. The keys in use, against that list

| Key | Holder | Expiry | Scopes | Against §1 |
|---|---|---|---|---|
| `crafd-caller-read` | CRAF'd | never | `buckets.read`, `documents.read`, `files.read`, `databases.read`, `tables.read`, `rows.read` | three extra read scopes, none used by this service |
| `crafdapi-probe` (created 2026-10-20) | the operator's smoke test, notebooks, integration tests and the data-freshness monitor (`APPWRITE_DATASTORE_API_KEY`, locally and as the repository secret) | never | the three in §1 | exact |
| `VIEWS Pipeline Core` | the pipeline; until 2026-10-21 also `APPWRITE_DATASTORE_API_KEY` | 2026-11-17 | 20 scopes | not a caller key; this service no longer uses it |

The extra scopes on `crafd-caller-read` are read-only, but they may reach database and table
data that `documents.read` and `files.read` do not, possibly other services' (C-371). Narrowing
them is left to the next rotation of that key, not done live, because a mis-scoped partner key
fails as an apparent revocation. The key never expires, so nothing forces that rotation; C-371's
trigger is the reminder.

### 3. How the list is kept true

- A new caller key is created with §1's scopes, then checked with `scripts/smoke.py` before it is
  given to anyone. A missing scope fails there, but **not by name**: the key check answers a
  generic 401 (C-264 keeps the store's wording out of responses), which looks like a revoked key.
  On a 401 from a new key, compare its scopes in the console against §1 before anything else.
- Any change that adds an Appwrite SDK call to a serving route updates §1 in the same change.

---

## Rationale

The scopes follow from the calls, not the other way round. A list reasoned down from an
over-scoped key (views-faoapi's route) is a guess that happens to be checked later, if at all.
Tracing the calls makes every scope answer to a line of code, and makes an unused one visible.

---

## Considered Alternatives

- **Copy `crafd-caller-read`'s scopes.** Rejected: it repeats an unrecorded choice, including three
  scopes nothing uses.
- **Bind the list to the code with a test,** as views-faoapi's `tests/test_auth_probe_scope.py`
  binds its runbook's table to its key check. Deferred, not rejected: worth adding when the key
  check is narrowed (open items below, #176), so the test pins the final list rather than an interim
  one.

---

## Consequences and open items

- **The key check asks for more than it needs.** Checking a key by listing *buckets* is the only
  reason `buckets.read` is on the list. views-faoapi narrowed the same check to listing one file in
  the delivery bucket, which needs `files.read`, a scope every caller already holds. Doing the same
  here would cut the list to two scopes. Registered as C-370 and filed as #176; not done in this ADR.
- **Unverified:** whether an Appwrite API key can be limited to particular buckets or collections.
  If it cannot, every caller key reads every bucket in the project that its scopes cover,
  including other services' data. To check in the Appwrite console or documentation before
  CRAF'd's key is next rotated. Registered as C-371.
