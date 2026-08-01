# Logging & Observability Standard

**Status:** Active  
**Governing ADRs:** ADR-003 (Philosophy of Engineering), ADR-005 (Testing), ADR-008 (Observability and Explicit Failure)  

---

## 1. Purpose

This document defines operational standards for:

- Logging behavior
- Log levels
- Error propagation patterns
- Alerting and observability expectations

This standard operationalizes:

> Structural failures must be raised explicitly and logged persistently. (ADR-008)

It does not redefine architectural principles.

---

## 2. Scope

This standard applies to the views-faoapi project, a FastAPI service delivering
VIEWS conflict forecast data to the UN FAO. The project uses Python's standard
logging library configured via `src/views_faoapi/configs/logging.yaml`, which defines YAML-based
logging configuration with 5 rotating file handlers covering different aspects
of system operation.

All logging in this project must conform to the configuration defined in
`src/views_faoapi/configs/logging.yaml` and the standards described below.

---

## 3. Core Principles

### 3.1 Fail Loud and Persist

- Structural failures must:
  - be logged at `ERROR` or higher
  - be raised as exceptions
- Logging is not a substitute for raising.
- Raising is not a substitute for logging.

Silent degradation is prohibited.

---

### 3.2 Logs Must Support Understanding

Logs must:
- provide sufficient context to reconstruct state
- include relevant identifiers (run_id, endpoint, GAUL level, forecast period, etc.)
- avoid ambiguity

Logs must not:
- rely on implicit assumptions
- require tribal knowledge to interpret

---

### 3.3 Logs Must Not Leak Sensitive Data

- Secrets must never be logged.
- Credentials (Appwrite keys, API tokens) must never be logged.
- Sensitive raw inputs must not be logged unless explicitly approved.

---

## 4. Log Levels (Normative Definitions)

We adopt the following level semantics:

### DEBUG
- Development diagnostics.
- Detailed internal state.
- Must not be required to understand production failures.

### INFO
- High-level lifecycle events.
- Start/finish of major stages.
- API request summaries and configuration state.

### WARNING
- Unexpected but recoverable conditions.
- Degraded behavior that does not violate invariants.
- Must not mask structural errors.

Warnings must not be used to hide invariant violations.

### ERROR
- Structural failure within a component.
- Operation failed and cannot proceed correctly.
- Must be raised and logged.

### CRITICAL
- System-wide failure.
- Corruption, irrecoverable state, or orchestration breakdown.
- Immediate attention required.

---

## 5. Error Propagation Pattern

Structural errors must follow this minimal pattern:

1. Construct a clear, descriptive error message.
2. Log the error (`ERROR` or `CRITICAL`).
3. Raise the appropriate exception with the same message.

Example:

```python
err_msg = "GAUL level L2 mapping not found for PRIO-GRID cell 142857."

logger.error(err_msg)

raise ValueError(err_msg)
```

Spacing conventions are not mandated.
Clarity and consistency are.

---

## 6. Logging Scope Expectations

### 6.1 Required Logging

The following must be logged:

* API request lifecycle (request received, processing started, response sent)
* Prediction file downloads from the VIEWS prediction store
* Cache hits and misses for forecast data and statistical computations
* Appwrite operations (file upload, download, listing, deletion)
* HDI and MAP computation stages (input validation, computation start/finish, result summary)
* Geographic aggregation stages (PRIO-GRID to GAUL mapping, level transitions)
* All structural failures

### 6.2 Optional Logging

* Intermediate array shapes during statistical computation (DEBUG)
* Posterior distribution sample counts and summaries (DEBUG)
* Performance metrics for endpoint response times
* Detailed cache key construction and eviction events

---

## 7. Log Structure and Context

Log entries should include:

* Timestamp
* Level
* Module or component name
* Relevant identifiers (endpoint path, GAUL level, forecast period, prediction run_id)

Structured logging (JSON or key-value format) is recommended where possible.

---

## 8. Alerting

Alerting is an operational layer built on logging.

At minimum:

* `ERROR` and `CRITICAL` logs must be alertable.
* `CRITICAL` logs must escalate.
* Alert routing must avoid noise amplification.

Alert configuration (Slack, email, orchestration tools) is operational and may evolve.

---

## 9. Testing Requirements

Logging behavior must be testable where meaningful.

Tests should verify:

* Errors are both logged and raised.
* Log level separation works as expected.
* Alerts trigger on configured severity thresholds.

Logging tests must not rely on manual inspection.

---

## 10. Anti-Patterns (Prohibited)

* Swallowing exceptions without logging
* Logging and continuing after invariant violation
* Downgrading errors to warnings to "keep things running"
* Using `print()` for structural diagnostics
* Logging entire objects without context

---

## 11. Evolution

This document may evolve independently of ADRs.

If logging semantics change in a way that affects system meaning,
ADR-008 must be revisited.
