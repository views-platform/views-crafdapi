# ADR-031: Credential Model and Clone-Safety Boundary

**Status:** Proposed — the engineering boundary (§1–§4) is decided and being implemented under epic #325; the **credential-ownership model** (§5, decision *b*) is pending operator ratification (D-25/D-26).
**Deciders:** Claude (design authority, §1–§4); Simon (operator, §5 — credentials, external party, cross-repo).
**Records:** the decisions that make `views-faoapi` safe to clone into `views-productionapi` (a write API) and `un-crafdapi` (a read API), from the clone-readiness investigation and þing-02 (`views_platform/þingit/02_credential_identity_key_ownership/`).

---

## Context

`views-faoapi` is about to be cloned into two new APIs. Whatever the seed carries is copied twice. The clone-readiness investigation (epic #325) and þing-02 established:

- faoapi is a **read-only serving app**, but it carries a full **producer write / provision / delete subtree** (`prediction.py:upload_predictions/delete_prediction`; `appwrite.py` `upload_*`/`create_*`/`delete_*`). It is **dead on the serving path today by non-invocation, not by a guard** — no route reaches it (register cluster B; **C-37/C-179/C-181**).
- **#322 (Tier 1)** — inside that subtree, `upload_file_with_metadata` deletes a metadata document on *any* `get_file` failure (discarding the error `code`), so a mis-scoped key deletes valid metadata. A **second, separate** hazard sits beside it: the FOUND_BY_NAME branch does `delete_file(old)` **then** `upload_file(new)` — a crash between the two destroys the only copy.
- The disk cache labels each caller's partition with `sha256(x_api_key)[:16]` — the **key is the cache identity** (register **C-180/C-181**), re-implemented per repo, inviting a silent cross-serve on clone.
- **Auth / credential / cache-partition logic is smeared across `api.py` + `appwrite.py`**, forked (not shared) across the fleet (**C-179**). One Appwrite key is simultaneously FAO's, dev's, and ops' (**C-178**).

A clone that is a *write* API activates the dead subtree at birth; a clone that shares a cache dir or a divergent partition function cross-serves; three forked copies of the auth policy diverge on the next change. These must be closed in the seed.

This ADR is faoapi's consumer-side companion to the platform contract **PLATFORM-001** (homed in `views-appwrite`, referenced by pinned URL, never copied) and to **ADR-027** (per-key isolation).

## Decision

### 1. The reader / writer / provisioning boundary is explicit and enforced

The serving path (routes → `dataset_service` → the cache and the *read* managers) **must never reach the producer write/provision/delete subtree**. This is:

- **Locked by test** — a serving-isolation tripwire (`tests/test_serving_isolation.py`, S1/#326) fails if any serving-layer module calls a producer method; strengthened after §3's split to also forbid *importing* the writer/provisioning modules.
- **Made structural** — `appwrite.py` and `prediction.py` are split so the reader is a separate module from the writer/provisioning (S9/#334, S10/#335). A **read-clone imports only the reader**; a **write-clone additionally takes the writer**. The producer subtree stays behind the default-OFF provisioning gate (`_require_provisioning`), which is a permanent design choice, not a migration.

Rationale: SRP (one concept per file), ISP (serving depends only on the read surface it uses), and the observation that "dead by non-invocation" is not a property a clone preserves — only "dead by structure" is.

### 2. The cache-partition label is not derivable from the caller's key

The on-disk partition label must be **neither the key value nor derivable from it**, while distinct keys still map to distinct partitions and the scheme is stable across workers and restarts. faoapi implements this with a single server-side salt (`hmac(salt, api_key_hash)`, S5/#323); the invariant is guarded by `tests/test_cache_isolation.py` (S2/#327, register **C-177**). Cross-repo, the partition/isolation *invariant* is pinned by a **shared conformance test vector** (A4/#324) that each clone runs against its own client — so three repos cannot drift into a cross-serve without importing shared code (§3); centralizing the function into one importable definition is deferred to **D8**.

### 3. Each API writes its own thin client; the seam is a shared **contract + test**, not shared code

The Appwrite seam is governed by the ratified platform contract (**The Appwrite Seam Contract**, formerly `PLATFORM-001`, v1.3.0 in `views-appwrite`), referenced by pinned URL — never copied. Under that contract's §5.8, the shared surface — `AppwriteConfig`, the auth strategy, `OperationResult`, the SDK-compat helpers, the partition function, env/registry validation — is **not a shared importable library**. Each API **writes its own thin client to the contract** (WET-before-DRY — three understood copies beat one guessed abstraction), and `views-appwrite` **stays parked** (no `src/`, no import edge). What *is* genuinely shared, without any repo importing a common implementation:
- the **contract** (the rules, by pinned URL), and
- a **conformance test vector** (A4/#324) that every clone runs against its own client — so the partition/isolation *invariant* (§2) cannot silently drift across three repos.

**The shared-*implementation* extraction is NOT decided here.** Whether `views-appwrite` should ever host importable runtime code (so the partition function is defined once, changed in one place) is **þing-01 decision D8**, deferred behind its own trigger; the current platform evidence — the seam contract's §5.8, and its amendment log which already **rejected** the "make `views-appwrite` a real dependency before we clone" proposal (which *this ADR's author conceded at þing-02*) — leans **against** it. This ADR therefore **proposes** the eventual DRY extraction (register **C-179/C-180/C-181**, decision **D-03**) but records it as **pending D8**, not as faoapi's to decide unilaterally. **S12/#337 is gated on D8**, not merely on "views-appwrite existing".

Not shared at all (kept duplicated deliberately): the `forecast/` domain tree and the serving-SLA policy — a second API serves a different product; sharing them now would couple three products to one contract.

### 4. Destructive storage operations are upload-then-swap, never delete-before-write

In a write-clone's client (§3), #322 is fixed at the seam: (a) the orphan-cleanup deletes metadata **only** on a genuine not-found — matched by both the `storage_file_not_found` type and, since the SDK leaves the type unreliable for non-JSON errors, a "could not be found" message check — fail-visible otherwise (the faoapi-local half lands now, S4/#322); (b) the "same name, different hash" replace is redesigned to **write the new artifact, then swap/retire the old** — never delete the only copy before the replacement exists. **The delete-before-write branch must not be cloned as-is.**

### 5. Credential-ownership model — direction, pending operator ratification (decision *b*)

*This section is the operator's (credentials, an external party, cross-repo). It records the recommended direction; ratification is D-25/D-26.*

- **Keep the caller-supplied-key model for v1** (the caller presents its own Appwrite key, faoapi re-uses it read-only — the "confused deputy"). It is simple and shipping. **Recorded limit:** because the key *is* the identity, faoapi cannot offer per-consumer rate-limits/quotas/audit without a stable id (register **C-175/C-180**). (D-25.)
- **Make the seam contract (in `views-appwrite`) the specification** a future issued/scoped-key model is written against, so the three clients evolve in step against one *contract* (not one shared codebase, per §3).
- **Do not clone until the credential model is written and faoapi is pristine** (D-26). Cloning first trebles the shared-key surface (**C-178**) and the confused-deputy model.
- **Operator preconditions (filed, not executed by design):** split FAO's key from dev/ops + re-issue read-scoped caller keys + retire the legacy fanned-out key + rotate (S13/#338, **C-178**, cross-ref #123); the shared conformance vector (#324) blocked on `views-appwrite` hosting it + a test Appwrite project.

## Consequences

- **Enables clean clones:** a read-clone drops the writer; a write-clone keeps it and inherits the safe (upload-then-swap, code-checked) destructive path; neither inherits a key-derived cache label or a forked auth policy.
- **Costs:** the reader/writer split is real refactoring work (S9–S11); three thin clients written to one contract (WET, by ratified choice); one server-side salt file per deployment; a one-time cold cache when the label scheme changes. A shared importable client (S12) is an *additional* cost only if D8 approves it.
- **Open / operator-owned:** §5 (D-25/D-26), S12/S13/#324 are blocked (cross-repo / operator / test-project). This ADR is `Proposed` until §5 is ratified; §1–§4 proceed under epic #325 regardless.
- **Standing rule for future contributors:** the serving path may not import the writer/provisioning modules; the partition label may not derive from the key; destructive storage ops are upload-then-swap. Violations are caught by the S1/S2 tripwires.

## References

Epic #325; þing-02 (`þingit/02_credential_identity_key_ownership/`); **The Appwrite Seam Contract** (`views-appwrite`, formerly `PLATFORM-001`, v1.3.0 — its §5.8 governs §3 above; faoapi issue #340 tracks the name update); þing-01 **D8** (the deferred shared-implementation question); ADR-027 (per-key isolation), ADR-033 (fail-visible). Register: C-37, C-160, C-175, C-177, C-178, C-179, C-180, C-181; D-03, D-25, D-26. Sibling fix: views-pipeline-core#329 (the same C-231 shape).
