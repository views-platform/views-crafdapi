# ADR-014: Dead Code Removal Policy

**Status:** Implemented  
**Date:** 2026-05-27  
**Implemented:** 2026-05-28  
**Deciders:** Project maintainers  

---

## Context

**Implementation Status (2026-05-28):** All commented-out code blocks removed from core modules. No commented-out function or class definitions remain in `managers/api.py`, `managers/appwrite.py`, or `data/handlers.py`. The env var validation block was restored per ADR-013 (now active code at `api.py:28-46`). C-08 resolved.

Approximately 693 lines of commented-out code exist across the three largest source files:

- `managers/api.py`: 196 lines (13.3%), including old endpoint definitions at lines 767-831
- `managers/appwrite.py`: 334 lines (15.6%), including previous upload methods at lines 966-1028 and old metadata/upload logic at lines 1460-1672
- `data/handlers.py`: 163 lines (10.3%), including old metadata retrieval at lines 1177-1206 and deprecated processing at lines 1328-1367

These represent prior implementations that were replaced but never removed. The most dangerous
instance was the commented-out environment variable validation (see C-03), which previously ensured all 8 `APPWRITE_*` variables were present at startup. This has been restored as active code per ADR-013 (now at `api.py:28-46`).

Dead code increases cognitive load, makes diffs noisy, risks accidental uncomment of
superseded logic, and obscures the intended behavior of surrounding live code.

This decision addresses risk register entry C-08.

---

## Decision

1. **Commented-out code blocks >3 lines must not be committed.** Code that may be needed
   later belongs in git history (`git log`, `git blame`), not inline comments.
2. **Deferred features** must be tracked via issues or ADRs referencing the last commit
   containing the implementation, not via commented-out code.
3. **Single-line comments explaining WHY code was removed are permitted;** the code itself is not.
4. **Exception:** The env var validation block (`api.py:383-396`) must be **uncommented and
   restored** per ADR-013, not deleted.
5. **New commented-out blocks** in PRs must be flagged during review and resolved before merge.

---

## Rationale

Git preserves every line ever committed via `git log -p` and `git blame`. Inline dead code
duplicates this capability while imposing costs: reviewers must distinguish live from dead
code, contributors must assess whether blocks are intentionally preserved or forgotten, and
the 334 dead lines in `appwrite.py` interleave with active methods, obscuring control flow.
The env var validation incident (C-03) demonstrates that commenting out code can create
silent correctness bugs. A removal policy prevents this class of error.

---

## Considered Alternatives

### Alternative A: `# DEPRECATED:` markers with expiry dates
- **Reason for rejection:** Still clutters the codebase; expiry dates are rarely enforced.

### Alternative B: Feature flags for conditionally active code
- **Reason for rejection:** Appropriate for runtime toggles, not for permanently superseded code.

### Alternative C: Separate `archive/` branch
- **Reason for rejection:** Adds maintenance burden; duplicates git's native history capability.

---

## Consequences

### Positive
- Removes 693 lines of noise from the three core files
- Prevents accidental reactivation of superseded logic
- Forces explicit tracking (issues, ADRs) for deferred features
- Resolves C-08 in the risk register

### Negative
- Contributors must check git history to recover old implementations
- Initial cleanup requires careful review of each block (delete vs. restore vs. document)

---

## Implementation Notes

1. **`managers/api.py`:** All commented-out code blocks removed. The env var validation block was restored as active code per ADR-013 (now `_validate_appwrite_env()` at `api.py:40-46` and `_REQUIRED_APPWRITE_ENV_VARS` at `api.py:28-37`). No commented-out function or class definitions remain.

2. **`managers/appwrite.py`:** All commented-out code blocks removed. No commented-out function or class definitions remain.

3. **`data/handlers.py`:** All commented-out code blocks removed. No commented-out function or class definitions remain.

4. **CI advisory check:** `grep -rn '^\s*#\s*def \|^\s*#\s*class ' src/views_crafdapi/` returns no matches, confirming cleanup is complete.

---

## Validation & Monitoring

- **Post-cleanup:** Run `grep -c '^\s*#' <file>` before and after; comment-line counts
  should decrease by the documented amounts.
- **Regression detection:** CI advisory check flags new commented-out definitions in PRs.
- **Functional verification:** After restoring env var validation, test startup with a missing
  `APPWRITE_*` variable to confirm a clear error is raised.
- **Reconsideration trigger:** If the team frequently recovers old code from git history
  (more than once per sprint), revisit whether a structured archival approach is needed.

---

## Open Questions

- Should the CI check be blocking from day one, or after a grace period for initial cleanup?
- Are there commented-out blocks outside the three identified files?
- Should the 3-line threshold be adjusted? (1 line is too aggressive; 3 allows brief examples.)

---

## References

- C-08 in the technical risk register (`reports/technical_risk_register.md`)
- C-03 in the risk register (commented-out env var validation)
- ADR-013: Environment variable validation restoration
- ADR-008: Observability and Explicit Failure
- `managers/api.py`, `managers/appwrite.py`, `data/handlers.py` — affected files
