# The Hardened Protocol: Contributor Governance

This document defines the mandatory engineering and mathematical standards for the `views-faoapi` repository. Adherence to this protocol is required for all contributions to guarantee absolute scientific integrity and reproducibility.

---

## 1. Core Principles

### A. The Authority of Declarations (ADR-003)
**"Never infer; only trust declarations."**
All meaningful semantics (statistical methods, aggregation strategies, geographic mappings, configuration parameters) must be explicitly declared in configuration files.
- **Prohibited:** Filename-based logic, directory-structure inference, or shape-based guessing.
- **Requirement:** If a parameter affects forecast identity or statistical output, it must be explicitly declared and validated at system boundaries.

### B. The Fail-Loud Mandate (ADR-008)
**"A crash is a successful defense of scientific integrity."**
Silent failures, implicit fallbacks, and "best-effort" corrections are forbidden.
- **Requirement:** Violations of statistical, geographic, or configuration invariants must raise an explicit error immediately.
- **Prohibited:** Using `nan_to_num`, silent clipping, or "sensible defaults" for critical parameters such as HDI credible interval widths, MAP estimation bounds, or posterior distribution shapes.

### C. The Numerical Airlock
All data entering the system must pass through a numerical airlock.
- **Requirement:** Validate numerical inputs at every boundary (API request entry, prediction data loading, statistical computation output).
- **Requirement:** Detect and raise errors on NaNs or Infs at every boundary (data ingestion, posterior distribution computation, aggregation output).

### D. Physical Symmetrical Architecture
**"Clarity over cleverness."**
Organizational clarity is a requirement for maintainability.
- **Requirement:** Every non-trivial class should have a clear, singular responsibility.
- **Requirement:** Heterogeneous logic (utilities, helpers, exceptions) should be consolidated into well-defined modules rather than scattered across the codebase.

---

## 2. Contributor Requirements

### Adding a New Component (Endpoint, Analyzer, Transform)
1.  **Define the Configuration:** Register mandatory parameters in the appropriate configuration file.
2.  **Create the Module:** Follow existing project conventions for module organization.
3.  **Create Specs/CICs:** Write the **Class Intent Contract (CIC)** for any non-trivial class.
4.  **Register in Router:** Add endpoint registration to the appropriate FastAPI router.

### Adding a New Statistical Method
1.  **Declare Parameters:** All parameters (e.g., HDI credible interval width, number of posterior samples, aggregation weights) must be explicit configuration, not hardcoded.
2.  **Validate Inputs:** Numerical inputs must be checked for NaN, Inf, and domain validity before computation.
3.  **Document Assumptions:** Statistical assumptions (e.g., distributional form, independence, stationarity) must be documented in the CIC or inline.
4.  **Test Exhaustively:** Include green team (correctness), beige team (edge cases with real-world data shapes), and red team (adversarial inputs like all-NaN arrays, zero-variance distributions) tests.

---

## 3. Mandatory Testing Taxonomy (ADR-005)

Every Pull Request must include tests covering the following three perspectives:

### Green Team (Stability & Correctness)
*   **Goal:** Ensure the system works as intended and remains stable.
*   **Examples:** HDI computation against known analytical solutions, MAP estimation on symmetric distributions, geographic aggregation checksum verification, PRIO-GRID to GAUL mapping integrity.

### Beige Team (Configuration & Human Error)
*   **Goal:** Catch failures caused by common configuration mistakes or missing parameters.
*   **Examples:** Missing GAUL level specification, invalid date ranges in forecast requests, malformed geographic identifiers, empty prediction store responses.

### Red Team (Adversarial)
*   **Goal:** Expose failure modes by deliberately trying to make the system lie or fail.
*   **Examples:** All-NaN posterior samples, zero-length time series, PRIO-GRID cells with no GAUL mapping, concurrent cache invalidation during aggregation, degenerate distributions (zero variance, infinite support).

---

## 4. Operational Invariants

- **Statistical Reproducibility:** Given identical inputs and configuration, all statistical computations (HDI, MAP, posterior summaries) must produce identical outputs.
- **Geographic Integrity:** PRIO-GRID to GAUL mappings must be validated at load time. Missing or ambiguous mappings must raise errors, not produce partial results.
- **Cache Consistency:** Cached results must be invalidated when upstream data or configuration changes. Stale cache hits are a correctness violation.
- **API Contract Stability:** Endpoint response schemas must not change without explicit versioning.

---
