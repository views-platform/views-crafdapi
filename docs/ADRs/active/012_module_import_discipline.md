# ADR-012: Module Import Discipline and Startup Side Effects

**Status:** Implemented  
**Date:** 2026-05-27  
**Implemented:** 2026-05-28  
**Deciders:** Project maintainers  

**Addresses risk register:** C-02 (module-level `app = create_app()`), C-04 (circular import chain), C-10 (topology violation via wandb import)

---

## Context

**Implementation Status (2026-05-28):** C-02 resolved -- lazy `create_app()` factory at `api.py:1303`; module-level `app` variable at `api.py:1300` is set by the factory, not at import time. C-04 resolved -- `log.py` no longer imports from `api.py` (the `APIPathManager` import was removed; `LoggingModule` accepts `ModelPathManager`). C-10 resolved -- `wandb` imports in `model.py` are now lazy, inside `APIManager.run()` at `model.py:792-793`; no module-level wandb imports remain.

Three interrelated import discipline violations create fragility in the `managers/` layer:

**Module-level app creation (C-02).** `managers/api.py` formerly executed `app = create_app()` at module level (originally at line 1472-1473). This line was reached whenever any symbol was imported from `api.py` for any purpose --- tests, type checking, IDE tooling, or other modules importing shared classes. The `create_app()` function (now at `api.py:1303`) triggered a cascade: `APIPathManager("un_fao")` instantiation, `pyprojroot` root discovery, `.env` loading via `load_dotenv()`, `CrafdApiManager` construction (which reads all `APPWRITE_*` env vars), FastAPI app instantiation with all route registrations, and `SIGINT`/`SIGTERM` signal handler installation via the lifespan context manager. The module was untestable in isolation because importing it required the full project directory structure and a valid `.env` file.

**Circular import chain (C-04).** `managers/log.py` formerly imported `APIPathManager` from `api.py` (at line 7). `api.py` imports from `model.py`. `model.py` lazily imported `LoggingModule` from `log.py` inside `ModelManager.__init__`. The cycle was: `log.py` -> `api.py` -> `model.py` -> `log.py`. The cycle was broken only by the deferred import, which was fragile --- moving that import to module level would have triggered an `ImportError`. `log.py`'s import of `APIPathManager` from `api.py` was unnecessary: `LoggingModule.__init__` accepts a `ModelPathManager` (the parent class, defined in `model.py`), not specifically an `APIPathManager`. Now resolved: `log.py` no longer imports from `api.py`; `model.py:13` imports `LoggingModule` at module level without cycle.

**Topology violation (C-10).** `managers/model.py` formerly imported `wandb_alert` from `views_crafdapi.wandb.utils` and `wandb` directly at module level (lines 12-13). ADR-002 declares that Infrastructure (`managers/`) must not depend on Observability (`wandb/`). The imports were used at `model.py:748` (`wandb.init()`) and `model.py:770` (`wandb_alert()`). This coupled the entire manager inheritance chain (`ModelPathManager` -> `ModelManager` -> `APIManager` -> `CrafdApiManager`) to WandB, making it impossible to import or test any manager class without WandB installed. Now resolved: wandb imports are lazy inside `APIManager.run()` at `model.py:792-793`; no module-level wandb imports remain.

---

## Decision

1. **No module-level function calls that mutate global state, perform I/O, or install signal handlers.** Module-level code in Python files must be limited to imports, constant definitions, class/function definitions, and logger initialization. The `app = create_app()` call at `api.py:1473` must be removed.

2. **`create_app()` must be called explicitly by the ASGI server entrypoint, not at import time.** Use uvicorn's `--factory` flag to invoke `create_app` lazily at server startup.

3. **Imports must follow ADR-002 topology strictly.** `log.py` must not import from `api.py`. Since `LoggingModule` only needs `ModelPathManager`, it must import from `model.py` instead.

4. **Cross-layer imports (Infrastructure -> Observability) must use lazy imports or dependency injection.** `model.py` must not import `wandb` or `wandb_alert` at module level. These imports must be deferred to the functions that use them (`APIManager.run()` at `model.py:748-774`).

---

## Rationale

- Module-level side effects violate the principle of least surprise. Python's import system is not an application lifecycle manager; conflating the two creates coupling between unrelated concerns.
- The circular import chain is currently held together by a single deferred import. Any refactoring that moves `model.py:516` to module level breaks the entire manager layer. This is a structural fragility, not a style concern.
- The wandb topology violation prevents testing `ModelManager` or `CrafdApiManager` in environments without WandB credentials, which includes CI and local development without WandB configuration.
- Uvicorn's `--factory` flag exists precisely for this use case: it calls the factory function at worker startup, not at import time, preserving proper lifecycle ordering.

---

## Considered Alternatives

### Alternative A: `TYPE_CHECKING` conditional imports

- **Pros:** Simple. Solves type hint circular imports cleanly.
- **Cons:** Only addresses type-checking imports, not runtime usage. `log.py:7` uses `APIPathManager` at runtime (passed to `LoggingModule.__init__`), not just for type hints. Does not solve C-02 or C-10.
- **Reason for rejection:** Insufficient scope. Addresses only one of three problems.

### Alternative B: Lazy module pattern via `importlib.import_module()`

- **Pros:** Fully resolves circular imports and defers side effects. Already used in the codebase for config script loading.
- **Cons:** Adds complexity and indirection. Moves import errors from load time to first use, making debugging harder.
- **Reason for rejection:** Appropriate as a tactical fix for the wandb import (C-10) but not as a general strategy. The primary fixes (removing module-level `create_app()`, fixing the `log.py` import target) are simpler and more direct.

### Alternative C: Full dependency injection via constructor parameters

- **Pros:** Cleanest architectural separation. Each class receives its dependencies explicitly. Fully testable with mocks.
- **Cons:** Largest refactor. Requires changing the constructor signatures of `ModelManager`, `APIManager`, and `CrafdApiManager`. Requires a composition root that wires dependencies.
- **Reason for rejection:** Correct long-term direction but disproportionate effort for the immediate problem. The targeted fixes in this ADR are prerequisites for DI regardless.

---

## Consequences

### Positive

- `api.py` becomes importable without triggering app initialization, enabling isolated unit testing of classes defined in the module (resolves C-02)
- The circular import chain is eliminated by changing one import target in `log.py` (resolves C-04)
- `ModelManager` and its subclasses become usable without WandB installed (resolves C-10)
- Import ordering becomes predictable and acyclic
- Test discovery no longer triggers full app startup

### Negative

- The uvicorn invocation must change from `uvicorn views_crafdapi.managers.api:app` to `uvicorn views_crafdapi.managers.api:create_app --factory`. This affects the deployment entrypoint (`views-models/apis/un_fao/main.py`) and CI configurations that start the server.
- Lazy wandb imports add a small overhead on first call to `APIManager.run()`. This is negligible since `run()` is called once at server startup, not per-request.
- Any code that currently relies on `from views_crafdapi.managers.api import app` to get a pre-initialized app instance will break. Such code must call `create_app()` explicitly.

---

## Implementation Notes

1. **Module-level app creation removed.** The former `app = create_app()` call was removed. The module-level `app` variable at `api.py:1300` is now `None` until `create_app()` (`api.py:1303`) is called by uvicorn. The `create_app()` factory sets the global `app` and `_fao_manager` via `api.py:1298-1300`.

2. **Change uvicorn invocation** in all entrypoints (deployment scripts, `pyproject.toml` scripts) from:
   ```
   uvicorn views_crafdapi.managers.api:app
   ```
   to:
   ```
   uvicorn views_crafdapi.managers.api:create_app --factory
   ```

3. **`log.py` import target fixed.** `log.py` no longer imports from `api.py`. The `APIPathManager` import was removed entirely; `LoggingModule` does not require it. `model.py:13` now imports `LoggingModule` at module level without circular dependency.

4. **wandb imports deferred in `model.py`.** Module-level wandb imports removed. Both `import wandb` and `from views_crafdapi.wandb.utils import wandb_alert` are now lazy inside `APIManager.run()` at `model.py:792-793`.

5. **Audit for other module-level side effects** in `managers/` files. Ensure no other module executes functions, opens connections, or modifies global state at import time.

---

## Validation & Monitoring

- **Import isolation test:** A test that imports `views_crafdapi.managers.api` in a subprocess without `.env` or Appwrite credentials must succeed without raising exceptions. This test should be added to the CI suite.
- **Circular import detection:** Run `python -c "import views_crafdapi.managers.log"` in a clean environment. It must not trigger `api.py` side effects.
- **Topology enforcement:** A static analysis check (e.g., `import-linter` or a custom grep-based CI check) should verify that `managers/` files do not contain module-level imports from `wandb/`.
- **Failure signal:** If `create_app()` is accidentally called at import time again (e.g., via a global variable reassignment), tests that import `api.py` will fail with environment errors, providing immediate feedback.

---

## Open Questions

- Should `create_app()` accept configuration parameters (e.g., an `AppwriteConfig` or a settings object) rather than reading environment variables directly? This would further improve testability but is a larger change.
- Are there any downstream consumers (notebooks, scripts, other services) that import `app` from `api.py` directly? These must be identified and updated.
- Should the wandb dependency be made optional (an extras group in `pyproject.toml`) so the core API can run without it installed?

---

## References

- C-02, C-04, C-10 in the technical risk register (`reports/technical_risk_register.md`)
- ADR-002 (Topology and Dependency Rules) --- the layer dependency matrix that C-10 violates
- ADR-008 (Observability and Explicit Failure) --- logging requirements during startup
- `managers/api.py:1298-1303` (module-level `app` variable and `create_app()` factory), `managers/log.py` (no longer imports from `api.py`), `managers/model.py:792-793` (lazy wandb imports inside `run()`), `managers/model.py:13` (module-level `LoggingModule` import, no longer circular)
