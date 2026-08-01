# ADR-015: Dependency Hygiene

**Status:** Implemented  
**Date:** 2026-05-27  
**Implemented:** 2026-05-28  
**Deciders:** Project maintainers  

---

## Context

**Implementation Status (2026-05-28):** `cachetools` is now actively used at `api.py:25` (`from cachetools import LRUCache, TTLCache`). C-09 resolved via ADR-011 adoption -- the dependency is justified by `_init_caches()` at `api.py:292-295`.

`cachetools==6.2.1` is declared as a runtime dependency in `pyproject.toml:24` but is not
imported anywhere in `src/`. This was confirmed by searching for `from cachetools` and
`import cachetools` across the entire source tree — no matches exist.

The dependency is likely a remnant from an earlier caching approach replaced by the current
dict-based in-memory caches (`_manager_cache`, `_dataframe_cache`, `_file_cache` at
`managers/api.py` -- now replaced by bounded caches at `api.py:292-295`). The project has 16 runtime dependencies; each unused one
represents supply chain risk (compromised upstream), installation overhead, and cognitive
confusion (contributors expect every declared dependency to have a corresponding import).

This decision addresses risk register entry C-09.

---

## Decision

1. **Every runtime dependency in `pyproject.toml` must have at least one corresponding
   import in `src/`.** Dependencies without imports are dead and must be removed.
2. **Dependencies must be audited when features that touch imports are added or removed.**
   Removing the last import of a package requires removing the `pyproject.toml` entry in the same PR.
3. **Dev dependencies** are exempt from the import requirement — test utilities and linters
   are invoked via CLI, not imported by application code.
4. **Conditional resolution with ADR-011:** If ADR-011 adopts `cachetools` for LRU eviction,
   C-09 resolves naturally. Otherwise, remove the dependency.

---

## Rationale

Unused dependencies are dead code in the dependency manifest. The same principles behind
ADR-014 (dead code removal) apply: `pyproject.toml` should truthfully declare what the
project needs (aligning with ADR-003, authority of declarations). `cachetools` is
well-maintained, but unused dependencies are pure risk with zero benefit regardless of
package quality. Removing an unused dependency is trivial; keeping it is a perpetual
(if small) supply chain exposure.

---

## Considered Alternatives

### Alternative A: Automated import scanning in CI (`deptry`, `pip-audit`)
- **Pros:** Catches unused dependencies automatically on every PR
- **Cons:** Adds tooling overhead; may produce false positives for plugin-style dependencies
- **Reason for not choosing as primary:** Current project size (16 deps) does not yet justify
  the overhead. Recommended as a follow-up (see Implementation Notes).

### Alternative B: Manual periodic audits
- **Reason for rejection:** The current situation (unused dep already present) proves manual
  audits are insufficient as the sole mechanism.

### Alternative C: Accept unused dependencies as harmless
- **Reason for rejection:** Increases supply chain risk unnecessarily; violates ADR-003.

---

## Consequences

### Positive
- Reduces supply chain risk by eliminating unused dependency vectors
- Ensures `pyproject.toml` accurately reflects actual requirements
- Slightly reduces install time and package size
- Resolves C-09 in the risk register

### Negative
- Requires discipline during PR review to check for orphaned dependencies
- ~~Conditional resolution with ADR-011 means C-09 may remain temporarily open~~ Resolved: ADR-011 implemented, C-09 closed

---

## Implementation Notes

1. **Resolved via ADR-011 adoption.** `cachetools` is now actively imported at `api.py:25`:
   ```python
   from cachetools import LRUCache, TTLCache
   ```
   Used in `_init_caches()` at `api.py:292-295`. C-09 resolved -- the dependency is justified by actual usage.

4. **Add `deptry` to dev dependencies (recommended follow-up):**
   ```toml
   [project.optional-dependencies]
   dev = ["deptry>=0.16", ...]
   ```
   Run `deptry src/` to scan for unused, missing, or transitive-only dependencies.
   Consider adding as a CI step alongside linting.

---

## Validation & Monitoring

- **Immediate:** `cachetools` was retained and adopted per ADR-011. `from cachetools import LRUCache, TTLCache` at `api.py:25` confirms active usage.
- **Ongoing:** If `deptry` is adopted, its CI output flags new unused dependencies in PRs.
- **Reconsideration trigger:** If runtime dependencies exceed 25, promote automated scanning
  from recommended to required.

---

## Open Questions

- ~~Is there a timeline for ADR-011? If months away, remove `cachetools` now and re-add if needed.~~ **Resolved (2026-05-28):** ADR-011 implemented; `cachetools` adopted and actively used.
- Are there other unused dependencies? A `deptry` scan would answer definitively.
- Should the audit requirement be enforced via PR checklist or code review alone?

---

## References

- C-09 in the technical risk register (`reports/technical_risk_register.md`)
- C-07 in the risk register (unbounded caches — context for why `cachetools` may have been added)
- ADR-011: Caching strategy (may resolve C-09 by adopting `cachetools`)
- ADR-014: Dead Code Removal Policy (parallel principle)
- ADR-003: Authority of Declarations Over Inference
- `pyproject.toml:24` — `cachetools==6.2.1` declaration
- `managers/api.py:25` (`from cachetools import LRUCache, TTLCache`), `api.py:292-295` (bounded cache declarations in `_init_caches()`)
