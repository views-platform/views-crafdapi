# ADR-029: SAST Signal — Gate CodeQL to Public Visibility

**Status:** Accepted
**Date:** 2026-06-26
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** ADR-005 (testing as critical infrastructure), ADR-008 (observability & explicit failure), register C-76, C-74; issues #85, #123
**Informed:** contributors, reviewers

---

## Context

The committed `codeql.yml` ("CodeQL Advanced") workflow fails on **every** run with *"Advanced Security must be enabled for this repository to use code scanning."* CodeQL code scanning requires **GitHub Advanced Security (GHAS)** — a paid, per-committer add-on — on a **private** repository. `views-platform/views-faoapi` is private and GHAS is not enabled, so the workflow can never succeed (register **C-76**, issue **#85**).

Two harms follow: (1) a permanently-red check trains reviewers to ignore a red CI bar — the same signal-loss pathology as the test probes (C-74); and (2) a UN-facing, security-sensitive service *appears* to run static analysis but does not, a false assurance that nearly mattered during a security PR.

A resolution is needed now because the test-signal-trustworthiness epic (#125) is explicitly removing permanently-red checks, and because the repo's planned path to public (rotate secrets + scrub history, **#123**) changes the economics: **CodeQL code scanning is free on public repositories.**

---

## Decision

### 1. The `analyze` job is gated to run only when the repository is public

`codeql.yml`'s `analyze` job carries `if: ${{ github.event.repository.private == false }}`. On the private repo the job is **skipped** (not failed); when the repo flips public (#123), code scanning becomes free and the job **self-activates** on the next push/PR — no further change required.

### 2. No GHAS purchase, no interim red check, no redundant SAST tool

We do **not** buy GHAS to make CodeQL pass while private, and we do **not** add a second SAST (bandit/semgrep) as an interim gate. Rationale below; the key fact is that GitHub Actions is currently disabled org-wide by a billing limit that the same public-release (#123) resolves, so an interim SAST workflow could not run in CI before CodeQL itself can.

### 3. CodeQL is the SAST of record for this repo, effective on public release

Once public, CodeQL (Python, `build-mode: none`) is the static-analysis gate. Tightening the query suite (e.g. `security-extended`) is a follow-up once it is running.

### 4. Scope

This ADR governs the **SAST CI signal**. It does not change the test workflow (ADR-026-adjacent `run_pytest.yml`), the path-to-public remediation itself (C-77/#123), or any application code.

---

## Rationale

- **A skipped job is honest; a red job lies.** Gating on visibility removes the false "SAST is running" signal and the trained-to-ignore-red harm, without pretending analysis happens when it cannot.
- **Self-activation beats a follow-up PR.** Tying activation to `repository.private == false` means the security gate turns on exactly when it becomes both free and possible, with zero manual step to forget.
- **Don't pay to silence a red bar.** Buying GHAS purely to make a private-repo check pass spends money to paper over a signal problem the public move resolves for free.
- **No redundant interim tool.** Actions cannot run at all until billing is restored by going public; an interim bandit/semgrep workflow would be equally unable to run before then, so it adds surface without earning signal. If the public move slips materially, revisit (Open Questions).

---

## Considered Alternatives

### A: Enable GHAS (pay) to make CodeQL pass while private
- **Pros:** SAST runs immediately.
- **Cons:** recurring per-committer cost to solve a problem the imminent public move removes for free. **Rejected.**

### B: Delete `codeql.yml`, re-add after going public
- **Pros:** no red check while private.
- **Cons:** loses the configured workflow and requires a remembered follow-up PR to restore it; easy to forget. **Rejected** in favour of the self-activating gate (keeps the config, no follow-up).

### C: Replace CodeQL with a free SAST (bandit/semgrep) now
- **Pros:** a SAST that needs no GHAS.
- **Cons:** can't run until Actions billing is restored (= going public), at which point CodeQL is free anyway; adds a second tool to maintain. **Rejected now**; reconsider only if the public move is materially delayed.

---

## Consequences

### Positive
- No permanently-red SAST check; CI red regains meaning (supports the #125 / C-74 goal).
- CodeQL turns on automatically and for free the moment the repo is public — the security control becomes real with no extra action.
- The configured workflow is preserved (not deleted), so there is nothing to restore.

### Negative / trade-offs
- **There is no SAST coverage while the repo remains private.** This is accepted: the private window is meant to be short (bounded by #123), and the prior "coverage" was illusory (the job only ever failed). If the private window stretches, the Open Question reopens.
- Correct gating depends on `github.event.repository.private` being present in the event payload; if absent, the condition is falsy and the job safely skips (fails closed to "no red", not "false green").

---

## Implementation Notes

- The gate is a single job-level `if` in `.github/workflows/codeql.yml`; the rest of the workflow is unchanged.
- Verify after going public: confirm `analyze` runs (not skipped) and uploads results; then consider `queries: security-extended,security-and-quality`.
- Register **C-76** is resolved by this ADR (no permanently-red SAST check; SAST posture documented and self-activating).

---

## Open Questions

- **If the public move (#123) is materially delayed**, revisit adding a free, Actions-independent SAST (e.g. a local `bandit` pre-commit / `make sast`) so the private window is not analysis-blind.
- **Query suite hardening** (`security-extended`) — defer until CodeQL is running on the public repo.

---

## References

- faoapi **ADR-005** (testing as critical infrastructure), **ADR-008** (observability & explicit failure)
- Workflow: `.github/workflows/codeql.yml`
- Risk register: **C-76** (CodeQL non-functional / GHAS), **C-74** (red-by-default signal loss), cluster **D** (path-to-public), cluster **E** (CI signal-loss)
- Issues: **#85** (this work), **#123** (path-to-public — the precondition that makes CodeQL free)
