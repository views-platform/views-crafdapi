# ADR-001: Ontology of views-faoapi

**Status:** Accepted  
**Date:** 2026-05-27  
**Deciders:** Project maintainers  

---

## Context

The views-faoapi repository is a FastAPI service that delivers VIEWS conflict forecast data to the UN FAO. It combines prediction data with geographic aggregation across PRIO-GRID cells and GAUL administrative levels, performs statistical analysis (HDI, MAP, posterior distributions), and manages data storage through Appwrite.

This system is intended to support long-lived development under uncertainty, with multiple contributors and evolving requirements around forecast delivery, geographic resolution, and statistical methodology.

Without an explicit ontology, systems tend to accumulate:
- implicit concepts
- overloaded abstractions
- objects that mix responsibilities
- semantics that exist only in developers' heads

This leads to ambiguity, fragile refactors, and silent divergence between intent and implementation.

An explicit ontology is required to define **what kinds of things are allowed to exist** in this repository, and which kinds of things are explicitly disallowed.

---

## Decision

This repository defines a **closed set of conceptual categories** ("entities") that are allowed to exist.

Each category has:
- a clear semantic role
- an expected stability level
- explicit boundaries

Anything that does not clearly belong to one of these categories is considered **out of scope** and must be re-designed or rejected.

---

## Core Ontological Categories

The following six categories constitute the complete ontology of views-faoapi:

| Category | Directory / Mechanism | Purpose | Authority | Stability | Must Not Contain |
|---|---|---|---|---|---|
| **Infrastructure** | `managers/` | API lifecycle, Appwrite storage abstraction, caching, path management, authentication, logging | Derived --- implements decisions made in domain and config layers | Evolving (tied to Appwrite SDK, FastAPI) | Domain logic, statistical computation, data transformation |
| **Domain** | `data/` | Dataset abstractions, statistical analysis (HDI, MAP, posterior distributions), geographic aggregation, tensor operations | Authoritative for data semantics and statistical computation | Stable --- changes here affect output correctness | Infrastructure concerns, storage details, HTTP routing |
| **Configuration** | `configs/` | Declarative specification of runtime behavior (logging config, environment settings) | Authoritative for operational parameters | Stable structure, evolving values | Logic, computation, or side effects |
| **Observability** | `wandb/` | Experiment tracking, alerting, monitoring integration | Derived --- consumes domain and infrastructure events | Evolving (tied to WandB platform) | Domain logic, data mutation, control flow decisions |
| **Reference Data** | `shapefiles/` | Immutable geographic reference data (GAUL administrative boundaries) for spatial aggregation | Authoritative (sourced from FAO/GAUL) | Stable --- updated only on new FAO boundary releases | Derived data, computation results, application state |
| **Predictions** | Runtime artifacts via `PredictionStoreManager` | Conflict forecast distributions with typed provenance metadata --- the primary data artifact | Authoritative for forecast content and metadata | Stable schema, evolving content | Processing logic, infrastructure details |

### Infrastructure (`managers/`)

Infrastructure encompasses the API lifecycle (FastAPI application setup, routing, middleware), Appwrite storage abstraction (file upload, retrieval, caching), path management, authentication, and logging. It is **derived** --- it implements decisions made in the domain and configuration layers and has no authority over data semantics or statistical methods. It evolves as external dependencies (Appwrite SDK, FastAPI) evolve. Infrastructure must not contain domain logic, statistical computation, or data transformation.

### Domain (`data/`)

The domain layer owns dataset abstractions, statistical analysis (HDI credibility intervals, MAP estimates, posterior distributions), geographic aggregation across PRIO-GRID cells and GAUL administrative levels, and tensor operations. It is **authoritative** for data semantics and statistical computation. This layer is expected to be stable because changes here directly affect output correctness and forecast integrity. The domain must not contain infrastructure concerns, storage details, or HTTP routing logic.

### Configuration (`configs/`)

Configuration provides declarative specification of runtime behavior, including logging configuration and environment settings. It is **authoritative** for operational parameters. The structure is expected to be stable while values may evolve across deployments. Configuration must not contain logic, computation, or side effects.

### Observability (`wandb/`)

Observability covers experiment tracking, alerting, and monitoring integration through WandB. It is **derived** --- it consumes events from domain and infrastructure layers but does not influence them. It evolves as the WandB platform evolves. Observability must not contain domain logic, data mutation, or control flow decisions.

### Reference Data (`shapefiles/`)

Reference data consists of immutable geographic reference data, specifically the GAUL administrative boundary shapefiles (Level 1 and Level 2), sourced from the FAO. These are **authoritative** as the canonical geographic boundaries for spatial aggregation. They are stable and updated only when the FAO releases new boundary definitions. Reference data must not contain derived data, computation results, or application state.

### Predictions (runtime artifacts via `PredictionStoreManager`)

Predictions are the primary data artifact of the system: conflict forecast distributions with typed provenance metadata. They are **authoritative** for forecast content and metadata. The schema is expected to be stable while content evolves with each forecast cycle. Predictions must not contain processing logic or infrastructure details.

---

## Stability Rules

- **Domain** (`data/`) and **Reference Data** (`shapefiles/`) are expected to be stable across the lifetime of the project. Changes to these categories require careful review because they affect output correctness and geographic fidelity.
- **Infrastructure** (`managers/`) and **Observability** (`wandb/`) are explicitly allowed to evolve or be replaced as external platforms and SDKs change.
- **Configuration** (`configs/`) has a stable structure but evolving values --- new configuration keys may be added, but the declarative, side-effect-free nature must be preserved.
- **Predictions** have a stable schema but evolving content --- the structure of forecast metadata must not change silently, but new forecast data is expected with each prediction cycle.

Stability is a design constraint, not a preference.

---

## Explicit Non-Entities

The following are **not allowed** as first-class concepts in views-faoapi:

- Implicit or inferred semantics (e.g., guessing prediction type from column name patterns)
- Objects that mix multiple ontological roles (e.g., a manager that also performs statistical computation)
- "Convenience" abstractions that hide meaning (e.g., a utility module that silently bridges domain and infrastructure)
- Concepts that exist only via naming conventions (e.g., geographic level inferred from DataFrame shape)

If a concept matters, it must be explicit.

---

## Consequences

### Positive
- Shared vocabulary across contributors for discussing forecast delivery, statistical analysis, and geographic aggregation
- Reduced conceptual drift as the system evolves
- Clear review criteria for new abstractions --- every new module or class must belong to exactly one category

### Negative
- Requires upfront discipline to classify new components
- Some refactors may be blocked until concepts are clarified

These trade-offs are accepted.

---

## Notes

This ADR defines *what exists*, not *how components depend on each other*.  
Dependency rules are defined separately in ADR-002.
