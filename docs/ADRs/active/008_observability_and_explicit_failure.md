# ADR-008: Observability and Explicit Failure

**Status:** Accepted  
**Date:** 2026-05-27  
**Deciders:** Project maintainers  

---

## Context

This repository supports systems where silent failure, degraded semantics,
or partial execution can cause cascading downstream impact.

Stack traces alone are insufficient for traceability in distributed,
long-running, or operational pipelines.

To preserve architectural integrity and post-hoc auditability,
failures must be both:

- **explicitly raised**, and
- **persistently recorded**.

Inconsistent logging and error propagation patterns increase
debugging complexity and obscure structural failures.

The project already maintains structured logging infrastructure via
`configs/logging.yaml`, which defines a YAML-based configuration with
five rotating file handlers (INFO, DEBUG, WARNING, ERROR, CRITICAL),
each with independent retention policies (10 to 90 days of backups).
This ADR establishes the architectural requirements that this logging
infrastructure must serve, ensuring that the existing handlers are used
consistently and that failure semantics are not left to ad hoc decisions.

---

## Decision

The repository adopts the following invariant:

> Structural failures must be both **logged persistently** and **raised explicitly**.

### 1. Explicit Failure

- Invariant violations must raise exceptions.
- Structural failures must not be downgraded to warnings.
- Errors must not be silently swallowed.
- Fallback behavior must not hide semantic failure.

Fail-loud (ADR-003) applies fully to runtime behavior.

---

### 2. Persistent Observability

- Raised structural failures must be logged at `ERROR` level or higher.
- Critical system-wide failures must be logged at `CRITICAL`.
- Logging must occur before or at the point of raising.
- Logging is not a substitute for raising; raising is not a substitute for logging.

The existing `configs/logging.yaml` provides the infrastructure for this:
the `error_file_handler` (60-day retention) and `critical_file_handler`
(90-day retention) ensure that structural failures are persisted with
sufficient history for post-hoc investigation. Code must use these
levels consistently to ensure failures are captured in the appropriate
rotating log files.

---

### 3. Scope

This ADR applies to:

- data validation failures (e.g., malformed MultiIndex, missing metadata columns, invalid alpha values),
- configuration inconsistencies (e.g., missing `APPWRITE_*` environment variables, incompatible cache TTL settings),
- semantic ambiguity (e.g., ambiguous geographic aggregation level, unresolvable PRIO-GRID to GAUL mapping),
- broken invariants (e.g., HDI nesting violations, tensor reshape dimension mismatches, inconsistent sample sizes),
- orchestration breakdowns (e.g., Appwrite connection failures, cache corruption, partial file downloads),
- and other structural system failures.

It does not prescribe formatting, spacing, or specific logging utilities.
Operational conventions may evolve separately.

---

## Consequences

### Positive

- Persistent traceability of structural failures
- Reduced debugging entropy
- Strong alignment with fail-loud invariant (ADR-003)
- Improved production observability

### Negative

- Slight increase in boilerplate
- Requires discipline in error handling

These costs are accepted.

---

## Notes

This ADR defines architectural requirements for failure handling.

It does not define log formatting standards, log retention policies,
or logging infrastructure configuration, which are operational concerns.
The current `configs/logging.yaml` with its five-handler, rotating-file
architecture satisfies the infrastructure needs of this ADR; changes to
that configuration are operational decisions that do not require ADR revision
so long as the invariants defined here remain satisfied.

Observability must support understanding.
Failure must never be silent.
