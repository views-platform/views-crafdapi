# ADR-013: Environment Variable Validation and Fail-Fast Configuration

**Status:** Implemented  
**Date:** 2026-05-27  
**Implemented:** 2026-05-28  
**Deciders:** Project maintainers  

**Addresses risk register:** C-03 (commented-out env var validation)

---

## Context

**Implementation Status (2026-05-28):** `_validate_appwrite_env()` module-level function at `api.py:40-46`. `_REQUIRED_APPWRITE_ENV_VARS` constant at `api.py:28-37` (all 8 vars). Called at `api.py:208` (constructor) and `api.py:302` (`_create_appwrite_config`). Commented-out validation replaced with active fail-fast validation. C-03 resolved.

`managers/api.py:373-441` defines `_create_appwrite_config()`, which constructs the `AppwriteConfig` object used to initialize the Appwrite SDK client. This method previously contained a validation block (`api.py:383-396`) that checked all required environment variables and raised a clear `ValueError` listing any missing ones. That validation is now **entirely commented out**.

The current live code (`api.py:428-440`) passes `os.getenv()` results directly to `AppwriteConfig`. When any variable is unset, `os.getenv()` returns `None`, which propagates silently into the Appwrite SDK client. The system starts successfully and appears healthy. The failure surfaces only on the first authenticated data request, producing an opaque `AppwriteException` that does not indicate the root cause (a missing environment variable).

The 8 required environment variables are:

| Variable | Purpose |
|----------|---------|
| `APPWRITE_ENDPOINT` | Appwrite server URL |
| `APPWRITE_DATASTORE_PROJECT_ID` | Project ID for the datastore |
| `APPWRITE_CRAFD_BUCKET_ID` | Storage bucket ID for prediction files |
| `APPWRITE_CRAFD_BUCKET_NAME` | Storage bucket name (used in logging and metadata) |
| `APPWRITE_CRAFD_COLLECTION_ID` | Collection ID for file metadata |
| `APPWRITE_CRAFD_COLLECTION_NAME` | Collection name (used in logging and metadata) |
| `APPWRITE_METADATA_DATABASE_ID` | Database ID for metadata storage |
| `APPWRITE_METADATA_DATABASE_NAME` | Database name (used in logging and metadata) |

The commented-out validation (`api.py:383-396`) only checked 5 of these 8 variables. It did not validate `APPWRITE_CRAFD_BUCKET_NAME`, `APPWRITE_CRAFD_COLLECTION_NAME`, or `APPWRITE_METADATA_DATABASE_NAME`. The restored validation must cover all 8.

---

## Decision

1. **All required environment variables must be validated before constructing `AppwriteConfig`.** Validation must occur inside `_create_appwrite_config()` before the `return AppwriteConfig(...)` statement.

2. **Validation must collect ALL missing variables and report them in a single error.** The system must not fail on the first missing variable and leave the operator to discover the remaining ones through repeated restart cycles.

3. **Validation must run at app startup**, inside the `create_app()` -> `CrafdApiManager.__init__()` -> first `_create_appwrite_config()` call path. It must not be deferred to the first data request.

4. **No `None` values may propagate into `AppwriteConfig` for required fields.** The `AppwriteConfig` constructor must receive validated, non-None string values for all 8 required parameters.

5. **The commented-out code block at `api.py:383-412` must be either restored (with corrections) or replaced entirely.** It must not remain as dead code.

---

## Rationale

- **Fail-fast is a correctness requirement, not a convenience.** A deployment with missing credentials that starts successfully and then fails on the first request creates a dangerous window where the system appears healthy to load balancers and monitoring but cannot serve any data. This violates ADR-003 (declarations over inference) and ADR-008 (explicit failure).
- **Listing all missing variables in a single error is an operational necessity.** In production deployments with 8 required variables, discovering them one at a time through restart cycles wastes operator time and increases deployment risk windows.
- **The original validation was commented out, not deleted.** This suggests it was disabled temporarily (perhaps during development with partial credentials) and never re-enabled. The code's intent was correct; only its execution state is wrong.
- **`os.getenv()` returning `None` is not an error in Python** --- it is the expected behavior for unset variables. The responsibility for treating `None` as an error falls on the application code, not the standard library.

---

## Considered Alternatives

### Alternative A: Pydantic `BaseSettings` for env var parsing

- **Pros:** Type-safe. Automatic validation. Supports `.env` files natively. Generates clear error messages for missing fields. Can validate types (e.g., URL format for endpoint).
- **Cons:** Adds Pydantic as a dependency (or requires it to already be installed). Introduces a new pattern not currently used in the codebase. Requires defining a Settings class.
- **Reason for rejection:** Good long-term direction, but disproportionate for the immediate fix. The existing code structure (a method that reads env vars and constructs a config) is adequate when validation is restored. Pydantic `BaseSettings` can be adopted as a follow-up if configuration complexity grows.

### Alternative B: `os.environ[]` (KeyError on missing)

- **Pros:** Zero additional code. Python raises `KeyError` automatically for missing variables.
- **Cons:** Fails on the **first** missing variable, not all of them. The `KeyError` message does not explain the operational context (which env vars are required, what they are for). Does not allow checking all 8 simultaneously.
- **Reason for rejection:** Fails the "report all missing at once" requirement. Operator experience is unacceptable for multi-variable configuration.

### Alternative C: `python-dotenv` validation

- **Pros:** Already used for `.env` file loading (`load_dotenv()` at `api.py:187`).
- **Cons:** `python-dotenv` loads variables from files into `os.environ` but does not validate their presence. It has no built-in "required variables" concept.
- **Reason for rejection:** Wrong tool for the job. `load_dotenv()` is a loading mechanism, not a validation mechanism.

---

## Consequences

### Positive

- Deployments with missing credentials fail immediately at startup with a clear, actionable error message listing all missing variables (resolves C-03)
- The time between "deploy" and "discover misconfiguration" drops from "first user request" to "server startup" --- typically seconds versus minutes or hours
- The commented-out dead code block (formerly at `api.py:383-412`) has been replaced with active validation at `api.py:28-46`, reducing cognitive load and dead code volume (also addresses C-08 partially)
- Aligns with ADR-003 (fail loud on semantic ambiguity) and ADR-008 (explicit failure with logging)

### Negative

- A deployment that previously "started successfully" with missing vars will now fail at startup. This is intentionally breaking: a deployment without credentials is not functional, and pretending it is creates worse outcomes.
- The validation adds approximately 15-20 lines of code to `_create_appwrite_config()`. This is minimal and justified.

---

## Implementation Notes

1. **Commented-out validation replaced** with the module-level `_validate_appwrite_env()` function at `api.py:40-46` and `_REQUIRED_APPWRITE_ENV_VARS` constant at `api.py:28-37`. All 8 variables are validated. Missing vars produce a single `ValueError` listing all missing names, preceded by a `logger.critical()` call.

2. **Validation called at startup** via `CrafdApiManager.__init__()` at `api.py:208` and inside `_create_appwrite_config()` at `api.py:302`, which returns the validated env dict for direct use in `AppwriteConfig` construction.

3. **`_REQUIRED_APPWRITE_ENV_VARS` is a module-level constant** at `api.py:28-37`, referenceable by tests and health checks.

---

## Validation & Monitoring

- **Startup validation test:** A test that calls `_create_appwrite_config()` with one or more `APPWRITE_*` variables unset must verify that a `ValueError` is raised listing all missing variables, not just the first.
- **All-missing test:** All 8 variables unset must produce an error listing all 8. Three of 8 unset must list exactly those 3.
- **CRITICAL log assertion:** The test must verify that `logger.critical()` is called before the `ValueError` is raised.
- **Production signal:** A "Missing required environment variables" message in the CRITICAL log during deployment indicates a failed rollout.
- **Failure mode for reconsideration:** If the set of required variables changes frequently, consider migrating to Pydantic `BaseSettings`.

---

## Open Questions

- ~~Should `APPWRITE_CRAFD_BUCKET_NAME`, `APPWRITE_CRAFD_COLLECTION_NAME`, and `APPWRITE_METADATA_DATABASE_NAME` be truly required, or are they informational?~~ **Resolved (Sprint 2.5):** Decision 4 is authoritative — all 8 vars are required. The `_NAME` vars are used in Appwrite metadata queries and bucket listing, not just logging. Implementation now validates all 8.
- Should validation also check that `APPWRITE_ENDPOINT` is a valid URL format (e.g., starts with `https://`)? This would catch copy-paste errors but adds format validation beyond presence checking.
- ~~Should the `REQUIRED_ENV_VARS` list be defined in a central configuration module rather than inside `_create_appwrite_config()`?~~ **Resolved (Sprint 2.5):** `_REQUIRED_APPWRITE_ENV_VARS` is now a module-level constant in `api.py`, referenced by tests and the standalone `_validate_appwrite_env()` function.

---

## References

- C-03 in the technical risk register (`reports/technical_risk_register.md`)
- C-08 (commented-out code) --- the dead validation block is the most dangerous instance of C-08
- ADR-003 (Authority of Declarations Over Inference) --- fail loud on semantic ambiguity
- ADR-008 (Observability and Explicit Failure) --- structural failures must be logged and raised
- ADR-009 (Boundary Contracts and Configuration Validation) --- Appwrite SDK boundary contract
- `managers/api.py:28-46` (`_REQUIRED_APPWRITE_ENV_VARS` constant and `_validate_appwrite_env()` function), `api.py:208` (constructor call), `api.py:302` (`_create_appwrite_config` call)
