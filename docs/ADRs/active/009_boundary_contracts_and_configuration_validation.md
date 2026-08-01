# ADR-009: Boundary Contracts and Configuration Validation

**Status:** Accepted  
**Date:** 2026-05-27  
**Deciders:** Project maintainers  

---

## Context

Complex systems fail most often at boundaries:

- between modules,
- between configuration and runtime,
- between data producers and consumers,
- between planning and execution.

Ambiguous configuration, hidden defaults, and implicit contracts
introduce silent semantic drift and runtime fragility.

To preserve architectural integrity and fail-loud guarantees (ADR-003),
all external and internal boundaries must be explicit and validated.

In views-faoapi, the following boundaries are architecturally significant:

- **Appwrite SDK boundary:** The application connects to Appwrite through 8 `APPWRITE_*` environment variables (`APPWRITE_ENDPOINT`, `APPWRITE_DATASTORE_PROJECT_ID`, `APPWRITE_UNFAO_BUCKET_ID`, `APPWRITE_UNFAO_BUCKET_NAME`, `APPWRITE_UNFAO_COLLECTION_ID`, `APPWRITE_UNFAO_COLLECTION_NAME`, `APPWRITE_METADATA_DATABASE_ID`, `APPWRITE_METADATA_DATABASE_NAME`). Missing or misconfigured variables produce runtime failures deep in the call stack rather than at startup.

- **API key authentication boundary:** Every API request passes an `X-API-Key` header that is validated against Appwrite. This boundary separates unauthenticated requests from authenticated state and controls access to all forecast data.

- **Data ingestion boundary:** Prediction files are downloaded from Appwrite as raw bytes and parsed through a format-detection cascade (parquet, CSV, JSON, pickle, feather) before being constructed into a `ForecastDataset` with a validated MultiIndex and required metadata columns.

- **Geographic aggregation boundary:** PRIO-GRID cell-level distributions are aggregated to GAUL administrative levels (L0, L1, L2) and country (ISO3) through element-wise summation. This boundary transforms the index structure and the semantic meaning of the data.

- **Cache boundary:** Data flows through a three-tier cache (in-memory dict, disk pickle with file locks, remote Appwrite) with a 3.5-week TTL. Each tier has different consistency guarantees, serialization formats, and failure modes.

---

## Decision

This repository adopts the following invariants:

> All architectural boundaries must declare explicit contracts.  
> All configuration must be validated at entry.  
> No semantic defaults may exist silently.

---

## 1. Boundary Contracts

Every boundary between components must define:

- Explicit input schema
- Explicit output schema
- Declared invariants
- Failure semantics

Boundaries include:

- Configuration (environment variables) to runtime (Appwrite client initialization)
- Data ingestion (raw file bytes from Appwrite) to processing (`ForecastDataset` construction)
- Cell-level data (PRIO-GRID) to aggregated data (GAUL administrative levels)
- API request (HTTP with headers) to internal processing (authenticated manager instances)
- Cache tiers (in-memory to disk to remote), each with declared TTL and staleness semantics

Implicit contracts are prohibited.

If a boundary assumption cannot be declared clearly,
the boundary is ill-defined and must be redesigned.

---

## 2. Configuration as First-Class Artifact

Configuration is not a convenience layer.
It is an architectural artifact.

Configuration must:

- Be explicit
- Be versionable
- Be externally inspectable
- Be validated before execution
- Not rely on hidden defaults

The 8 `APPWRITE_*` environment variables, the server configuration (`host`, `port`, `workers`, `reload`), the cache TTL (3.5 weeks), and the logging configuration (`configs/logging.yaml`) are all configuration artifacts that must satisfy these requirements.

Changing configuration must not silently alter system meaning.

---

## 3. Validation at Entry (Handshake Principle)

All configuration and external inputs must be validated at the system boundary.

Validation must occur:

- Before state mutation
- Before execution begins
- Before orchestration proceeds

The system must fail early if:

- Required `APPWRITE_*` environment variables are missing or empty
- API keys fail authentication against Appwrite before any data access
- Downloaded prediction files cannot be parsed into a valid DataFrame
- The resulting DataFrame lacks required metadata columns (`_METADATA_COLS`)
- The MultiIndex does not conform to the expected `(month_id, priogrid_id)` structure
- Cache metadata is corrupted or TTL values are inconsistent

Borrowed or assumed state is prohibited.

---

## 4. Separation of Configuration Domains

Configuration domains must be separated conceptually.

In views-faoapi, the following domains are relevant:

- **Connection parameters** (Appwrite endpoint, project ID, bucket/database/collection IDs): affect which remote resources are accessed
- **Behavioral parameters** (cache TTL, worker count, reload mode, authentication method): affect runtime behavior and performance
- **Metadata parameters** (bucket names, collection names, database names): informational identifiers that should not affect computation

Cross-domain coupling must be explicit.

Configuration that affects behavior must not be disguised as documentation.

---

## 5. Redundancy and Consistency Checks

Where ambiguity risk is high, explicit redundancy is preferred.

In views-faoapi, relevant examples include:

- Declaring both `bucket_id` and `bucket_name` in `AppwriteConfig`, with validation that they refer to the same resource
- Declaring both `collection_id` and `collection_name`, with consistency checks against the remote Appwrite state
- Validating that the `category` field in `PredictionMetadata` matches the category inferred from the API route
- Verifying that the number of samples in prediction arrays is consistent across all target variables and all cells

Redundant declarations must be validated for consistency.

Silent derivation is discouraged where semantic meaning is involved.

---

## 6. Failure Semantics

Configuration validation failures must:

- Be logged (ADR-008)
- Be raised explicitly (ADR-008)
- Halt execution

Warnings are insufficient for structural configuration errors.

A missing `APPWRITE_ENDPOINT` or an invalid `auth_method` must not be downgraded to a warning with a fallback default. These are structural failures that must prevent the system from starting.

---

## Consequences

### Positive

- Eliminates hidden configuration drift
- Reduces boundary fragility
- Strengthens fail-loud guarantees
- Improves reproducibility and traceability

### Negative

- Requires explicit schemas
- Adds validation boilerplate
- Increases up-front configuration clarity requirements

These costs are accepted.

---

## Notes

This ADR does not prescribe:

- Specific file layouts
- Specific configuration libraries
- Specific schema frameworks

Operational configuration structures may vary by project,
provided they comply with the invariants defined here.
