# ADR-018: SDK Response Normalization at Appwrite Boundary

**Status:** Accepted  
**Date:** 2026-05-28  
**Deciders:** Project maintainers  

**Addresses risk register:** C-19 (`_get()` alias resolution), C-20 (raw dict access on Pydantic), C-21 (OperationResult propagation), C-22 (`_as_dict()` drops `_data`), C-24 (field named "data" collision), C-25 (normalization boundary undocumented)

---

## Context

The Appwrite Python SDK underwent a breaking architectural change between versions:

- **SDK 13.3.0** (production, Hetzner): All API methods return **plain dicts**. Keys use Appwrite's JSON naming (`$id`, `$collectionId`, `fileId`, `sizeOriginal`). Code accesses values via `result["$id"]` or `result.get("name")`.

- **SDK 19.2.0** (local development): All API methods return **Pydantic `BaseModel` instances**. JSON keys become `Field(alias="$id")` aliases on Python attributes like `id`, `collectionid`, `sizeoriginal`. Pydantic models are not subscriptable (`model["$id"]` raises `TypeError`), and `getattr(model, "$id")` returns the Pydantic sentinel `PydanticUndefinedType` because `$id` is an alias, not a Python attribute name.

- **The `_data` PrivateAttr trap:** SDK 19.2.0's `Document`, `Preferences`, and `Row` models store user-defined fields in a `PrivateAttr` named `_data`, not as Pydantic fields. Calling `model_dump(by_alias=True)` --- Pydantic's standard serializer --- **silently drops all user data**. Only the SDK's own `to_dict()` method includes `_data`, but it nests it under a `"data"` key that SDK 13's flat dicts do not have.

`pyproject.toml` pins SDK 19.2.0 (corrected from a stale 13.3.0 pin — see ADR-019). The codebase is developed and tested against 19.2.0. The normalization layer must tolerate both SDK generations without conditional imports or version checks, since production environments may lag behind.

Four bugs (C-19 through C-22) were traced to normalization happening too late or incompletely: `_get()` could not resolve `$`-prefixed aliases on Pydantic models; raw `session["$id"]` crashed on Pydantic; `OperationResult.data` held un-normalized Pydantic models that broke downstream `.get()` calls; and `_as_dict()` used `model_dump()` which silently dropped `_data` contents.

---

## Decision

1. **Normalize at the boundary.** Every Appwrite SDK response is converted to a plain dict via `_as_dict()` before entering application code. The 8 `OperationResult(data=...)` construction sites and 4 raw access sites all call `_as_dict()` on the SDK response. Application code downstream of these boundaries receives only plain dicts and never handles Pydantic models.

2. **`to_dict()` over `model_dump()`.** For models with `_data` PrivateAttr (`Document`, `Preferences`, `Row`), `to_dict()` is the only serialization method that includes user data. `model_dump()` is a Pydantic-generic serializer that has no knowledge of `_data` and silently drops it. `_as_dict()` checks for `to_dict()` before falling back to `model_dump()`.

3. **Flatten the nested `"data"` key.** `to_dict()` nests user data under `"data"`: `{"$id": "x", "data": {"field1": "v1", ...}}`. SDK 13's flat shape is `{"$id": "x", "field1": "v1", ...}`. `_as_dict()` detects a `dict`-valued `"data"` key in the `to_dict()` output, pops it, and merges its contents into the top-level dict.

4. **Capability detection via `hasattr(obj, 'to_dict')`.** Forward-compatible with future SDK models. Deliberately chosen over explicit type checks per ADR-003 rationale: the `to_dict` method IS the declared contract of Appwrite models.

5. **`_get()` uses attribute access for non-`$` keys; `_as_dict()` + `dict.get()` for `$`-prefixed keys.** Direct `getattr` for non-`$` keys preserves rich sub-objects (e.g., `DocumentList.documents` returns a list of `Document` objects that can each be individually processed). For `$`-prefixed keys, attribute access fails on Pydantic aliases, so `_get()` falls through to `_as_dict()` + `dict.get()`.

---

## Rationale

- **Single normalization point eliminates a class of bugs.** C-19 through C-22 all stemmed from SDK responses reaching application code in their raw SDK-specific form. Normalizing at the boundary makes the problem structurally impossible to recur.
- **Callers never need to know which SDK version is installed.** Application code uses plain dict access (`result["$id"]`, `result.get("name")`), which works identically regardless of whether the SDK returned a dict or a Pydantic model.
- **`to_dict()` is the SDK's own normalization contract.** It is the method Appwrite SDK models define for serialization. Using it respects the SDK's declared interface rather than relying on Pydantic internals.
- **The `_SENTINEL` pattern in `_get()` distinguishes "not found" from "attribute is None".** `getattr(obj, key, _SENTINEL)` returns `_SENTINEL` only when the attribute does not exist, allowing `_get()` to correctly return `None`-valued attributes without falling through to the dict path.

---

## Considered Alternatives

### Alternative A: Normalize at each access site (status quo ante)

- **Pros:** No centralized conversion; each call site handles its own input.
- **Cons:** 9+ access sites, each a potential bug. C-19 through C-22 proved this approach fails in practice --- four independent bugs all caused by missed normalization points.
- **Reason for rejection:** Empirically demonstrated to be fragile. The bug surface area scales linearly with the number of access sites.

### Alternative B: Explicit type dispatch (`isinstance(obj, Document)`)

- **Pros:** Precise control per model type.
- **Cons:** Requires importing every Appwrite model class. Breaks when SDK adds new models. Creates coupling between the normalization layer and specific SDK internals.
- **Reason for rejection:** Violates ADR-003 (authority of declarations over inference). The `to_dict()` capability is the stable contract; specific class identity is not.

### Alternative C: Upgrade production to SDK 19.2.0

- **Pros:** Eliminates the dual-SDK problem entirely.
- **Cons:** Hetzner deployment is pinned to SDK 13.3.0. Upgrade requires coordination across the team, CI/CD changes, and production validation. The Pydantic model behavior differences would still need handling at access sites.
- **Reason for rejection:** Operational constraint. The upgrade may happen eventually, but the codebase must be correct now.

---

## Consequences

### Positive

- All `OperationResult.data` values are guaranteed to be plain dicts. Downstream code uses standard dict operations without risk of `TypeError` from Pydantic models.
- The normalization logic is concentrated in two functions (`_as_dict`, `_get`) at the top of `appwrite.py`, making it auditable and testable in isolation.
- 18 contract tests in `test_sdk_compat.py` exercise the normalization layer against real SDK model classes, catching regressions immediately.
- Resolves C-19, C-20, C-21, C-22 (four Tier 1-2 bugs) and C-25 (undocumented design).

### Negative

- **C-24 resolved:** The flatten step is now guarded by `hasattr(obj, '_data')`, limiting it to `_data`-bearing models (Document, Preferences, Row). For these models, user fields named `"data"` inside the nested dict are correctly preserved by `d.update(nested)`.
- **`_get()` on list-type models returns raw sub-objects.** `_get(document_list, "documents")` returns a list of `Document` objects (not dicts). Each must be individually passed through `_as_dict()` by the caller. This is intentional --- eagerly serializing sub-objects would lose `_data` contents if done via `model_dump()`.

---

## Implementation Notes

1. **`_as_dict()` at `appwrite.py:32-51`** --- the normalization cascade:
   - `dict` → passthrough
   - `hasattr(obj, 'to_dict')` → `obj.to_dict()` + flatten nested `"data"` key if `hasattr(obj, '_data')`
   - `isinstance(obj, BaseModel)` → `obj.model_dump(by_alias=True)` (fallback for models without `to_dict`)
   - `hasattr(obj, '__dict__')` → `vars(obj)` (covers `SimpleNamespace` from test mocks)
   - Default → passthrough (returns the object unchanged)

2. **`_get()` at `appwrite.py:54-66`** --- dual-strategy accessor:
   - `dict` → `obj.get(key)`
   - Non-`$` key → `getattr(obj, key, _SENTINEL)` with sentinel check
   - `$`-prefixed key or attribute miss → `_as_dict(obj)` then `dict.get(key)`
   - Final fallback → `getattr(obj, key, None)`

3. **8 boundary normalization sites** where `OperationResult(data=_as_dict(result))` is applied.

4. **4 raw access normalization sites** where dict subscript on SDK responses was replaced with `_as_dict()` before access (session, existing_docs, user, create_bucket).

---

## Validation & Monitoring

- **18 contract tests** in `tests/test_sdk_compat.py` using real SDK model classes (`Document`, `Preferences`, `File`, `Bucket`, `DocumentList`, `FileList`). These test `_as_dict()` and `_get()` against every model type the application uses.
- **7 falsification tests** in `tests/test_falsify_dual_sdk.py` and `tests/test_falsify_understanding.py` confirm known limitations (Pydantic subscript behavior, `"data"` field collision). Marked `xfail` to document rather than mask.
- **124 unit tests + 14 integration tests** pass against both SDK versions.
- **Failure signal:** If a `TypeError: 'X' object is not subscriptable` appears in production logs for any Appwrite model class, the boundary normalization has a gap --- a new SDK call site was added without `_as_dict()`.

---

## Open Questions

- When production upgrades to SDK 19.2.0, can the dict passthrough branch be removed? No --- it remains useful for test mocks and any code that constructs dicts directly.
- Should `_as_dict()` log a warning when falling through to the final `return obj` branch? Low priority --- all current SDK types are covered by earlier branches.
- ~~Should the `"data"` key flatten be made conditional (only when the object has a `_data` attribute)?~~ **Resolved:** Yes, `hasattr(obj, '_data')` guard added. See C-24 resolution in the risk register.

---

## References

- Risk register entries: C-19, C-20, C-21, C-22 (resolved), C-24 (open), C-25 (resolved by this ADR)
- Plan: `wise-dazzling-shore.md` (original TDD fix plan and Sprint 1 plan)
- Tests: `tests/test_sdk_compat.py`, `tests/test_falsify_dual_sdk.py`, `tests/test_falsify_understanding.py`
- ADR-003 (Authority of Declarations over Inference) --- justifies capability detection over type checks
- ADR-009 (Boundary Contracts and Configuration Validation) --- boundary contract principles
- ADR-014 (Dead Code Removal Policy) --- governs removal of old pre-normalization code
