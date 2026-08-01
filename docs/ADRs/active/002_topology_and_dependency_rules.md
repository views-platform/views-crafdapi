# ADR-002: Topology and Dependency Rules

**Status:** Accepted  
**Date:** 2026-05-27  
**Deciders:** Project maintainers  

---

## Context

In complex systems, architectural fragility often emerges not from incorrect
logic, but from uncontrolled dependencies between components.

The views-faoapi project comprises multiple layers --- domain logic for statistical analysis and geographic aggregation, infrastructure for API lifecycle and Appwrite storage, configuration, observability, and reference data. Without explicit topology rules:

- high-level modules begin depending on low-level implementation details,
- circular dependencies emerge,
- and system evolution becomes constrained by accidental coupling.

A clear rule is required to define **who may depend on whom**.

---

## Decision

This repository enforces a strict, directional dependency structure.

> Dependencies must follow declared architectural direction.
> No component may depend on a layer above it.

Dependency direction is part of the system's structural integrity.

Violations are architectural defects.

---

## Layering and Allowed Dependencies

The views-faoapi repository defines the following layers and their permitted dependency directions:

| Layer | Directory | May Depend On | Must NOT Depend On |
|---|---|---|---|
| **Domain** | `data/` | (no internal dependencies) | Infrastructure (`managers/`), Observability (`wandb/`) |
| **Configuration** | `configs/` | (no runtime dependencies) | Domain (`data/`), Infrastructure (`managers/`), Observability (`wandb/`) |
| **Reference Data** | `shapefiles/` | (no code dependencies) | Everything --- these are static data files |
| **Infrastructure** | `managers/` | Domain (`data/`), Configuration (`configs/`) | Observability (`wandb/`) |
| **Observability** | `wandb/` | Infrastructure (`managers/`) | Domain (`data/`) |

### Key invariants

- **Domain is independent.** The `data/` layer owns statistical computation (HDI, MAP, posterior distributions) and geographic aggregation logic. It must not import from `managers/`, `wandb/`, or any infrastructure module. Domain code must be testable and executable without a running API or Appwrite connection.

- **Configuration is pure and declarative.** The `configs/` layer must not depend on any runtime layer. It must not execute code at import time or import modules from `data/`, `managers/`, or `wandb/`.

- **Reference Data has no code dependencies.** The `shapefiles/` directory contains static GAUL boundary files. It has no imports and no runtime behavior.

- **Infrastructure may reach down, not up.** The `managers/` layer may import from `data/` (to invoke domain logic) and from `configs/` (to read configuration). It must not import from `wandb/`.

- **Observability consumes infrastructure events.** The `wandb/` layer may depend on `managers/` for event consumption but must not depend on `data/` for domain logic.

Dependency direction must remain **acyclic** at all times.

### Accepted Deviations

The following deviations from the strict layering rules are permitted:

- **Lazy imports from Infrastructure to Observability within function bodies.** `managers/model.py` imports `wandb` and `wandb_alert` inside `APIManager.run()` (at `model.py:792-793`), not at module level. This is permitted per ADR-012 Decision 4: the import creates no load-time dependency, and the module remains importable and testable without WandB installed. New lazy imports following this pattern do not require an ADR amendment, but module-level imports from `managers/` to `wandb/` remain forbidden.

---

## Architectural Boundaries

Each component must:

- Declare its responsibility zone (see ADR-001),
- Respect dependency direction (this ADR),
- Avoid implicit cross-layer coupling.

This ADR governs **structural dependency direction only**.

> The definition and validation of boundary contracts (schemas, configuration validation, handshake rules) are governed separately by ADR-009.

Topology defines *who may depend on whom*.  
ADR-009 defines *what must be true at the boundary*.

---

## Forbidden Patterns

The following are concrete examples of architectural violations in views-faoapi:

- **`data/` importing from `managers/`** --- e.g., the statistics module importing the API manager or Appwrite client. Domain logic must never depend on infrastructure.

- **`configs/` executing code at import time** --- e.g., configuration modules that establish Appwrite connections, make HTTP requests, or import runtime modules when loaded. Configuration must be inert.

- **`configs/` importing runtime modules** --- e.g., configuration files importing from `managers/` or `data/` to compute default values. Configuration values must be declared, not computed.

- **Circular import chains** --- e.g., `managers/` importing from `data/` which imports back from `managers/`. This is an existing identified risk (C-04 in the technical risk register) and must be actively prevented.

- **`wandb/` importing from `data/`** --- e.g., observability code importing statistical functions or dataset abstractions directly. Observability must consume events through infrastructure, not reach into the domain.

- **`data/` importing from `wandb/`** --- e.g., domain functions calling logging or tracking utilities directly. Domain code must have no knowledge of the observability layer.

If a dependency feels "convenient but wrong," it probably is.

---

## Consequences

### Positive

- Improved modularity --- domain logic can be tested without infrastructure
- Easier reasoning about change impact --- a change in Appwrite SDK does not propagate into statistical computation
- Safer refactoring --- clear boundaries reduce the blast radius of changes
- Reduced architectural entropy over time

### Negative

- May require additional abstraction layers or interfaces
- Can introduce short-term friction during refactoring when a convenient cross-layer shortcut is disallowed

These costs are accepted intentionally.

---

## Notes

This ADR defines structural direction of dependencies.

It does not define:

- boundary contract validation (ADR-009),
- semantic authority (ADR-003),
- or testing obligations (ADR-005).

Topology governs structure.  
Contracts govern interaction.
