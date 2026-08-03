# ADR-027: Authentication and Per-API-Key Isolation

**Status:** Accepted
**Date:** 2026-06-26
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** ADR-026 (API surface), ADR-011 (caching strategy), ADR-013 (environment-variable validation), ADR-016 (concurrency safety), ADR-009 (boundary contracts), the live auth path in `src/views_crafdapi/managers/api.py` + `managers/appwrite/`
**Informed:** UN FAO / FSFC API consumers; deployment operators

---

## Context

faoapi sits in front of an Appwrite backend that already owns the project's identity, buckets, and access control. The service needs a way to (a) authenticate each HTTP caller, (b) decide what data that caller may read, and (c) keep one caller's cached data and Appwrite client from leaking into another's. Issue #4 asks for the authentication model to be written down as a decision rather than left implicit in `_get_prediction_manager` / `ApiKeyAuth`, because authentication is a contract FAO integrates against and a security boundary an operator must understand.

The choice that had to be made — and was made in the code but never recorded — is **whether faoapi maintains its own identity/authorization system or delegates entirely to Appwrite**. Recording it matters now because the surface is becoming FAO-facing (ADR-026), because the path-to-public work (C-77/#123) makes credential handling a live concern, and because the per-key cache partitioning is a correctness property (the wrong partitioning would serve one caller another's data).

---

## Decision

### 1. The `X-API-Key` header carries the caller's Appwrite API key; faoapi runs no separate identity store

Every data, analysis, file, provenance, and cache endpoint requires an `X-API-Key` request header (a required FastAPI `Header(...)`; a missing key is a `422`). **The value is the caller's Appwrite API key** — faoapi does not mint, store, or manage its own user accounts, passwords, or tokens. The key is handed straight to the Appwrite client (`ApiKeyAuth.setup` → `client.set_key(...)`), so **authentication and authorization are delegated to Appwrite**: what a caller can read is exactly what their Appwrite key is scoped to. faoapi is an authenticated compute/proxy layer, not an identity provider.

### 2. faoapi never persists the raw key; it partitions state by a truncated hash

faoapi derives `api_key_hash = sha256(key).hexdigest()[:16]` (`_get_api_key_hash`) and uses **only that hash** to key the in-memory per-caller caches. The raw key exists only transiently inside the per-key Appwrite client in memory (to authenticate upstream calls) and in the request header; it is **never written to a cache filename, a log, or disk**. On disk the artifacts are named `{partition}_{category}_value/` / `_meta.json` / `.lock` (`managers/disk_cache.py`), where `{partition}` is a **salted HMAC** of `api_key_hash` (ADR-031/#323) — so the on-disk label is neither the key nor derivable from it, even by an observer of the filesystem. The persisted form is the columnar **value store**, not a pickle (C-149).

### 3. Per-key isolation across all cache layers

All cached state is keyed by `api_key_hash`, so callers are isolated by construction:

- **Manager cache** (`_manager_cache`, `LRUCache(maxsize=100)`) — a per-key Appwrite file manager + prediction-store manager (each holding that key's authenticated client).
- **In-memory dataframe/dataset cache** (`_dataframe_cache`, `TTLCache(maxsize=50, ttl=4h)`) — per-key, per-category latest dataset.
- **Disk cache** (`managers/disk_cache.py`) — per-key, per-category dataset **value store** (arrow/npz, no pickle — C-149) behind a per-key lock, labelled by a salted partition (ADR-031/#323; ADR-011, ADR-016).

A request authenticated with key *A* can only reach the `A`-partitioned managers and caches; it can never be served `B`'s cached dataset.

### 4. Transport security and key hygiene are deployment responsibilities

The key travels in a header and therefore **must be carried over TLS** — the deployment terminates HTTPS (ADR-022). Key rotation, scoping, and revocation are Appwrite-side operations (an operator narrows a key's bucket scopes or revokes it in Appwrite; faoapi inherits the change on the next authenticated call). faoapi's only obligation is to not leak the key (Decision 2).

### 5. Scope

This ADR governs **how a caller is authenticated and isolated**. It does *not* govern: the Appwrite key's own scope design (an Appwrite-console concern), the secret-in-history remediation for the *project datastore* key (C-77/#123 — a separate operational task), rate limiting (not currently implemented — see Open Questions), or the env-var validation of faoapi's *own* service credentials (ADR-013).

---

## Rationale

- **Don't rebuild identity faoapi doesn't own.** Appwrite already authenticates and authorizes against the buckets that hold the data; a parallel faoapi identity store would duplicate that, drift from it, and become a second place to get authorization wrong. Delegating makes Appwrite the single source of truth for "who may read what" (DRY at the system boundary; ADR-003's declarations-over-inference applied to access).
- **The hash is enough to partition, and safer than the key.** Cache correctness needs only a stable per-caller token; a truncated SHA-256 gives that without ever putting the secret on disk or in a filename, which keeps the path-to-public surface clean (C-77 is about a *different*, embedded key, but the same discipline applies here).
- **Isolation by construction beats isolation by check.** Keying every cache layer on the hash means cross-caller leakage cannot happen through a forgotten check — there is no shared, un-partitioned cache to leak through.
- **Inheriting Appwrite's scope model gives free least-privilege.** A read-only, single-bucket Appwrite key for FAO is configured in Appwrite, not in faoapi code.

---

## Considered Alternatives

### A: faoapi-native auth (its own users/tokens/JWT)
- **Pros:** independent of Appwrite; could add API-specific scopes/rate tiers.
- **Cons:** a second identity system to secure, sync, and audit; authorization could disagree with Appwrite's, the system that actually holds the data; large surface for an MVP. **Rejected.**

### B: Pass the key but cache globally (no per-key partition)
- **Pros:** higher cache hit rate; simpler cache keys.
- **Cons:** a caller could be served another caller's dataset — a confidentiality break; defeats Appwrite's per-key scoping at the cache layer. **Rejected** as a correctness/security failure.

### C: Store the raw key (e.g. as the cache filename or in logs) for convenience
- **Pros:** trivially debuggable.
- **Cons:** writes a live credential to disk/logs — exactly the path-to-public hazard the platform is removing (C-77). **Rejected.**

---

## Consequences

### Positive
- One credential, one authorization authority (Appwrite); no duplicated identity logic to drift.
- Cross-caller confidentiality holds by construction across all three cache layers.
- The raw key is never persisted, keeping disk/logs free of live credentials.

### Negative / trade-offs
- faoapi's access control is **only as good as Appwrite key hygiene** — an over-scoped key over-exposes data, and faoapi cannot tighten what Appwrite handed out. This coupling is now explicit.
- No faoapi-level rate limiting or per-caller quota exists today; abuse controls would have to be added here or at the proxy (Open Questions).
- A truncated 16-hex-char (64-bit) hash is a cache partition key, **not** a security token; it is never trusted for authorization (the raw key is always re-validated upstream by Appwrite on each call), so collision is a cache concern, not an auth bypass — but the choice of truncation length is recorded here for audit.

---

## Implementation Notes

- Auth entry points: `CrafdApiManager._get_api_key_hash`, `_get_prediction_manager`, `_get_appwrite_manager` (`managers/api.py`); the Appwrite-side credential application is `ApiKeyAuth.setup` (`managers/appwrite/`).
- Every endpoint declares `x_api_key: str = Header(..., description="Appwrite API Key")`; the root `/` endpoint documents the header requirement in its payload.
- Disk-cache partitioning: `managers/disk_cache.py` `_value_dir` / `_meta_path` / `_lock_path` all prefix with a **salted HMAC partition label** (`hmac(server_salt, api_key_hash)`, #323) — never the raw `api_key_hash`, so the on-disk label is neither the key nor derivable from it (§2).
- Do **not** add logging that prints `x_api_key`; log the hash if a per-caller log key is needed.

---

## Open Questions

- **Rate limiting / quotas** — none today; decide whether to enforce at faoapi or at the deployment proxy before opening the API more widely.
- **Hash truncation length** — 16 hex chars (64 bits) is ample for cache partitioning at current key counts; revisit only if the manager/dataframe-cache population grows by orders of magnitude.
- **Key-in-header vs. `Authorization: Bearer`** — the custom `X-API-Key` header is the current contract (ADR-026); a move to a standard `Authorization` scheme would be a consumer-visible change to coordinate.

---

## References

- faoapi **ADR-026** (API surface & resource model), **ADR-011** (caching strategy & eviction), **ADR-016** (concurrency safety), **ADR-013** (environment-variable validation), **ADR-022** (deployment/TLS), **ADR-003** (declarations over inference)
- Live auth path: `src/views_crafdapi/managers/api.py` (`_get_api_key_hash`, `_get_prediction_manager`, `_get_appwrite_manager`); `src/views_crafdapi/managers/appwrite/` (`ApiKeyAuth`); `src/views_crafdapi/managers/disk_cache.py` (per-key partitioning)
- Risk register: **C-77** (path-to-public credential hygiene — distinct embedded key), cluster **D** (path-to-public)
- Issue **#4** (API ADRs)
