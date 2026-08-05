# ADR-035: Deploy-time Appwrite coordinate provisioning (and the faoapi legacy divergence)

**Status:** Accepted
**Date:** 2026-08-05
**Deciders:** Project maintainer (operator) + agent

---

## Context

A consumer API (faoapi, crafdapi) needs a set of **Appwrite coordinates** (endpoint, project id,
bucket/collection ids and names, metadata database) plus **one secret** (the datastore API key) in
its process environment to serve. How those reach the production box is a deployment decision that,
until now, was **not written down accurately** — and the repository's description of it had drifted
from what the live box actually does. This ADR records the real mechanism, the divergence between
faoapi and crafdapi, and the forward plan. It was written while provisioning crafdapi's first
production deploy (epic #12 / S11), when the drift was discovered.

### The intended mechanism (PLATFORM-001 D2 / þing-01 #275)

Coordinates are **read from the owned, versioned registry** —
`views-appwrite/docs/ADRs/platform/coordinate_registry.toml` — and the **one secret is supplied by
the operator** in the environment, never sourced from a `.env`. This retired an earlier "copy-chain"
where credentials were grepped out of a personal laptop `.env`. See **The Appwrite Seam Contract**
(formerly PLATFORM-001), referenced by pinned URL at tag `appwrite-seam-v1.4.4` —
[`appwrite_seam_contract.md`](https://github.com/views-platform/views-appwrite/blob/appwrite-seam-v1.4.4/docs/ADRs/platform/appwrite_seam_contract.md)
— and ADR-031 (credential model & clone safety). *(This ADR first cited "a pinned commit" with no
link — itself the pin-decay the contract's §10 tracks; corrected per views-crafdapi#34.)*

`deployment/bootstrap.sh part2` realizes this:

1. clones the repo at the release tag and builds the venv (`uv sync`);
2. runs `deployment/registry_to_env.py <registry>` with the **venv's** Python — emitting `NAME=value`
   lines for every non-secret `connection`/`target` coordinate (secret entries are value-less *slots*
   and are never emitted);
3. appends `APPWRITE_DATASTORE_API_KEY=<operator secret>`;
4. writes the result to `~/.env.<api>` (`chmod 600`), which the systemd unit reads as its
   `EnvironmentFile`.

So the registry is needed **only at bootstrap time**, to build the static env file once. At runtime
the service just reads `~/.env.<api>`.

### What the live faoapi box actually does (the divergence)

faoapi's production box was set up on **2026-07-20, before #275 existed**. Its
`/home/views-faoapi-deploy/.env.faoapi`:

- is a **hand-built static file** (evidence: legacy/duplicated variable names —
  `APPWRITE_BUCKET_ID`, duplicated `APPWRITE_UNFAO_BUCKET_ID` — that a clean generator would never
  produce; `Birth == Modify == 2026-07-20 08:55:28`, untouched since, including through the
  2026-08-02 v1.4.1 redeploy which updated only the repo + tag file);
- was **not** produced by the registry bootstrap — there is **no `views-appwrite` checkout on the
  box** and no `coordinate_registry.toml` anywhere on it.

It almost certainly came from the pre-#275 laptop-`.env` copy method. The service is **up** — but
"up" is not "correct": whether the frozen file's values still match the registry has **never been
checked** (the registry has moved through v1.3.0 … v1.4.4 since), and one of its variables is
duplicated (`APPWRITE_UNFAO_BUCKET_ID` twice — last-wins, silently, if the two disagree). That
read-only measurement is tracked in **views-faoapi#360**; it is deliberately *not* bundled with a
re-bootstrap (Decision 3). **The registry bootstrap that the repo documents has therefore never
actually run in production.** crafdapi is its first real use.

### Verification before first use

Because crafdapi would be the first production run of the registry path, it was verified end-to-end
first. Running `registry_to_env.py` against the real registry with Python 3.13 (what the box venv
uses) exits 0 and emits **all 8 variables** `views_crafdapi`'s `_REQUIRED_APPWRITE_ENV_VARS` needs,
with real values (`APPWRITE_ENDPOINT`, `APPWRITE_DATASTORE_PROJECT_ID`, the four
`APPWRITE_CRAFD_BUCKET_*`/`COLLECTION_*`, and `APPWRITE_METADATA_DATABASE_ID/NAME`) — 16 coordinate
lines in total (crafd's 8 plus faoapi's `UNFAO_*`/`PROD_FORECASTS_*`, which crafd harmlessly ignores).
`bootstrap.sh part2` then appends the operator secret. Note: `registry_to_env.py` requires
`tomllib` (Python ≥3.11); the box venv (3.13) satisfies this, but a run under a system Python 3.10
fails with `ModuleNotFoundError: tomllib` — always run it via the repo venv.

---

## Decision

1. **crafdapi is provisioned via the registry bootstrap (#275) — the first production use of the
   intended mechanism.** It does **not** copy faoapi's legacy hand-built approach. crafdapi is a clean
   clone; it is the right place to establish the correct pattern, and future clones copy crafdapi, not
   faoapi's legacy box state.

2. **The registry is supplied to `bootstrap.sh part2` via the documented `APPWRITE_REGISTRY`
   override.** The box has no `views-appwrite` checkout, so a copy of the **versioned**
   `coordinate_registry.toml` is placed on the box and `APPWRITE_REGISTRY` points at it. This is the
   override the bootstrap explicitly provides — the file's content is the owned registry (non-secret
   identifiers); the **secret is never in it** and comes only from the operator slot. This is not the
   retired copy-chain (that copied the *secret* from a laptop `.env`); the discipline that matters —
   secret from operator slot, coordinates from the owned registry, nothing baked into code — holds.
   But the registry's own "never copy" rule is about **provenance**, not only secrecy: a copied file
   cannot say where it came from, so it cannot be told from a stale one (views-crafdapi#34 finding 3).
   We accept the copy now and recover the lost provenance via Decision 2a; Decision 4 is the full fix.

2a. **`bootstrap.sh` stamps `APPWRITE_REGISTRY_VERSION` (from the registry's `[meta].version`) as the
   first line of `.env.crafdapi`.** So "which registry version built this box?" is `grep`-able rather
   than folkloric — the copied file's missing git provenance becomes a recorded version marker. The
   registry has moved five versions in four days; without this a copied file is indistinguishable from
   a stale one. (views-crafdapi#34 finding 4; parallels views-postprocessing's `SEAM_CONTRACT_VERSION`
   drift marker, whose detector fired correctly on 2026-08-03.)

3. **faoapi is NOT re-bootstrapped now.** Its hand-built env works and re-bootstrapping a live,
   SLA-bound partner service purely for tidiness is not worth the disruption risk. faoapi is
   reconciled onto the registry path at its **next planned re-deploy**, not before. Until then, two
   deployment realities coexist by design, and this ADR is the record so it is not a surprise.

4. **Preferred long-term form of (2): a pinned `views-appwrite` checkout on the box**, so the registry
   has git provenance and re-deploys are repeatable without re-copying a file. This is a follow-up
   improvement, not a blocker for crafdapi's first deploy.

---

## Consequences

- **Positive:** crafdapi models the intended #275 practice end-to-end; the coordinate mechanism is now
  documented and verified rather than folkloric. The secret never touches the registry, any repo, or
  a log.
- **Negative / accepted:** faoapi and crafdapi are provisioned differently until faoapi's next
  re-deploy. The `APPWRITE_REGISTRY` copy is a file on the box, not a git checkout, until the
  follow-up in (4) lands.
- **Follow-ups:**
  - Reconcile `ADR-022` (Deployment Strategy) — it still describes the original pip/conda/`manager.run()`
    path and predates the systemd + `bootstrap.sh` + tag-gate + registry model actually in use.
  - Place a pinned `views-appwrite` checkout on the box (decision 4) and re-point `APPWRITE_REGISTRY`
    at it.
  - Reconcile faoapi onto the registry bootstrap at its next re-deploy; retire its legacy env file
    then.
  - Measure the faoapi divergence now (read-only: diff its `.env` values against the registry at a
    pinned tag; check the duplicated `APPWRITE_UNFAO_BUCKET_ID` lines agree) — **views-faoapi#360**.
    Turns Decision 3 from an inference into a measurement.
  - When the platform-level principled ADR lands (**views-appwrite#54**), cite it here by pinned tag.

---

## References

- **The Appwrite Seam Contract** (formerly PLATFORM-001) + `coordinate_registry.toml`, pinned at tag
  **`appwrite-seam-v1.4.4`**:
  [contract](https://github.com/views-platform/views-appwrite/blob/appwrite-seam-v1.4.4/docs/ADRs/platform/appwrite_seam_contract.md)
  ·
  [registry](https://github.com/views-platform/views-appwrite/blob/appwrite-seam-v1.4.4/docs/ADRs/platform/coordinate_registry.toml)
  — the cross-repo source of truth for coordinates and the secret-slot discipline. The platform-level
  *principled* ADR (the repo-agnostic clone playbook this concrete ADR realizes) is proposed in
  **views-appwrite#54**; when it lands at a tag, this ADR should cite it by that pin.
- **Review:** views-crafdapi#34 (this ADR) and views-faoapi#360 (the faoapi divergence measurement).
- **þing-01 #275** — re-point bootstrap off the laptop `.env` onto the registry + operator secret slot.
- `deployment/bootstrap.sh`, `deployment/registry_to_env.py`, `deployment/RELEASE_RUNBOOK.md` — the
  mechanism.
- **ADR-013** (environment variable validation) — the fail-loud `_REQUIRED_APPWRITE_ENV_VARS` gate the
  emitted env must satisfy.
- **ADR-031** (credential model & clone safety), **ADR-022** (deployment strategy — needs reconciliation).
