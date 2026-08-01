# ADR-019: Appwrite SDK Version Pinning

**Status:** Accepted  
**Date:** 2026-05-29  
**Deciders:** Project maintainers  

**Addresses risk register:** C-23 (`list_documents` deprecation)

---

## Context

The Appwrite Python SDK 19.2.0 deprecates `databases.list_documents()` in favor of a
future `tablesDB.list_rows()` API that does not yet exist in any released SDK version.
The codebase has 5 call sites across 3 methods (`search_files_by_metadata`,
`update_file_metadata`, `_store_metadata_document`) that use the deprecated method.

The SDK emits `DeprecationWarning: Call to deprecated function 'list_documents'. This API
has been deprecated since 1.8.0. Please use 'tablesDB.list_rows' instead.` (1.8.0 refers
to the Appwrite server API version, not the Python SDK version.) The function still works
correctly but will be removed in a future SDK release.

Additionally, `pyproject.toml` declared `appwrite==13.3.0` while the actual installed
version used in development and testing is `19.2.0`. This discrepancy means the declared
dependency does not reflect the runtime environment, violating ADR-003 (authority of
declarations) and ADR-015 (dependency hygiene).

Tripwire tests exist in `tests/test_sdk_compat.py::TestListDocumentsDeprecation`:
1. `test_list_documents_method_exists` — fails when SDK removes the method
2. `test_list_documents_call_sites_inventory` — AST check enforcing exactly 5 call sites
3. `test_list_documents_emits_deprecation_warning` — confirms the warning is emitted

---

## Decision

1. **Pin `appwrite==19.2.0` in `pyproject.toml`.** This is the version the codebase is
   developed and tested against. The pin prevents accidental upgrades that could remove
   `list_documents` before we migrate.

2. **Do not migrate to `tablesDB.list_rows` until it exists in a released SDK.** The
   replacement API is announced in the deprecation message but is not available. Migration
   is blocked on Appwrite, not on us.

3. **Retain tripwire tests as the upgrade signal.** When a future SDK release removes
   `list_documents`, `test_list_documents_method_exists` will fail immediately, providing
   a clear signal to migrate. The test suite is the monitoring mechanism, not manual
   version checking.

4. **Migration plan when triggered:** (a) upgrade SDK version in `pyproject.toml`,
   (b) replace 5 `self.databases.list_documents(` calls with the new API,
   (c) update `test_list_documents_call_sites_inventory` to track new call pattern,
   (d) verify via existing normalization tests in `test_sdk_compat.py` and live
   integration tests.

---

## Rationale

Pinning is the safest option because it eliminates the risk of surprise breakage from
an SDK upgrade while preserving the ability to migrate on our own schedule. The
alternative — migrating now — is impossible because the replacement API does not exist.
The other alternative — leaving unpinned or pinned to the wrong version — violates
ADR-003 and ADR-015 and creates silent upgrade risk.

The version correction from 13.3.0 to 19.2.0 in `pyproject.toml` is a declaration fix,
not a behavioral change. The codebase already depends on SDK 19.2.0 features (Pydantic
models, `to_dict()`, capability detection in `_as_dict()`). ADR-018 documents the
normalization layer built specifically for SDK 19.2.0's response format.

---

## Considered Alternatives

### Alternative A: Migrate to `tablesDB.list_rows` now
- **Reason for rejection:** The API does not exist in any released SDK. Migration is
  blocked on Appwrite.

### Alternative B: Downgrade to SDK 13.3.0 (as declared)
- **Reason for rejection:** The codebase depends on SDK 19.2.0 behaviors. ADR-018's
  normalization layer, `_as_dict()` with `to_dict()` support, and capability detection
  all assume 19.2.0. Downgrading would break the normalization boundary.

### Alternative C: Use version range (`appwrite>=19.2.0,<20`)
- **Reason for rejection:** A minor version bump could remove the deprecated API. Exact
  pinning is safer until the migration path is clear.

---

## Implementation Notes

1. Update `pyproject.toml` line 14: `"appwrite==13.3.0"` → `"appwrite==19.2.0"`
2. Update C-23 in risk register to document the pin decision
3. No code changes required — this is a declaration correction
4. ADR-018 already documents the SDK 19.2.0 normalization strategy

---

## Related

- **ADR-003:** Authority of declarations over inference — `pyproject.toml` must reflect
  actual runtime dependency
- **ADR-015:** Dependency hygiene — declared versions must match installed versions
- **ADR-018:** SDK response normalization — documents the normalization layer built for
  SDK 19.2.0
- **C-23:** `list_documents` deprecation — this ADR documents the pinning decision
