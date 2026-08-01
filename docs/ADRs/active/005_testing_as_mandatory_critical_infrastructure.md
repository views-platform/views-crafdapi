# ADR-005: Testing as Mandatory Critical Infrastructure

**Status:** Accepted  
**Date:** 2026-05-27  
**Deciders:** Project maintainers  

---

## Context

This repository is a FastAPI service that delivers VIEWS conflict forecast data to the United Nations Food and Agriculture Organization. Its outputs -- including MAP estimates, HDI credible intervals, posterior distribution summaries, and geographically aggregated fatality predictions -- directly inform humanitarian decision-making under uncertainty.

In such systems, failure is not limited to crashes or exceptions.
Failures may also include:
- silent semantic drift (e.g., HDI nesting violations that go undetected, geographic aggregation that silently drops PRIO-GRID cells),
- misuse by well-intentioned users (e.g., interpreting MAP estimates as point predictions rather than posterior modes, confusing HDI bounds with frequentist confidence intervals),
- over-trust or under-trust in outputs (e.g., treating aggregated country-level forecasts as precise when they represent element-wise sums of distributional samples),
- brittle behavior under realistic conditions (e.g., NaN propagation through posterior analysis, cache staleness across Appwrite worker restarts).

Given this, testing is not a convenience or a quality signal.
It is **critical infrastructure**.

The absence of rigorous, multi-perspective testing constitutes unacceptable risk.

---

## Decision

This repository treats **testing as mandatory critical infrastructure**.

All non-trivial functionality **must be covered by tests**.

Testing is not limited to correctness under ideal conditions, but must explicitly address:
- adversarial behavior,
- realistic human use,
- and system robustness under expected operation.

To achieve this, tests are explicitly divided into **three complementary categories**:

- 🟥 **Red team tests** (adversarial)
- 🟫 **Beige team tests** (realistic, neutral misuse)
- 🟩 **Green team tests** (supportive, resilience-oriented)

Each category serves a distinct purpose and **none may substitute for another**.

---

## Test Taxonomy

### 🟥 Red Team Tests -- Adversarial Testing

Red team tests deliberately attempt to **break, exploit, or misuse the system** by assuming hostile or worst-case behavior.

- **Goal:** expose failure modes, vulnerabilities, unsafe behaviors
- **Mindset:** *"How could this go wrong?"*
- **Typical focus:**
  - Security exploits
  - Model misuse or abuse
  - Safety failures
  - Stress-testing assumptions
  - Boundary and out-of-distribution behavior

In views-faoapi, red team tests should include:
- injecting NaN and Inf values into prediction arrays and verifying that `PosteriorDistributionAnalyzer` raises or handles them explicitly,
- constructing malformed MultiIndex DataFrames (wrong level names, duplicate indices, missing entity IDs) and verifying that `ForecastDataset` rejects them,
- submitting out-of-range `month_id` values or nonexistent `priogrid_id` values through the API,
- requesting HDI credible intervals with adversarial alpha values (0.0, 1.0, negative, greater than 1),
- sending malformed or missing API keys to the authentication boundary,
- submitting requests with entity IDs that cross geographic aggregation boundaries (e.g., GAUL codes that do not correspond to any PRIO-GRID cells).

Red team tests are expected to fail the system until weaknesses are addressed.

---

### 🟫 Beige Team Tests -- Realistic, Neutral Usage

Beige team tests focus on **boring, realistic, non-adversarial usage patterns** that are neither friendly nor hostile -- but still dangerous if mishandled.

- **Goal:** catch failures caused by normal human behavior
- **Mindset:** *"What will regular users actually do?"*
- **Typical focus:**
  - Ambiguous inputs
  - Misinterpretation of outputs
  - Over-trust or under-trust
  - Workflow and integration issues

In decision-support systems, beige failures are often the most damaging.

In views-faoapi, beige team tests should include:
- users misinterpreting MAP estimates as deterministic point predictions rather than posterior modes,
- users confusing HDI bounds with frequentist confidence intervals and drawing incorrect coverage conclusions,
- users consuming aggregated country-level data without understanding that it represents element-wise summation of sample distributions across constituent PRIO-GRID cells,
- users assuming that `{var}_min` and `{var}_max` in HDI-MAP responses represent hard bounds on possible outcomes rather than sample extrema,
- users copy-pasting JSON API responses into reports without noting the credibility level (alpha), time period, or aggregation method,
- users requesting forecasts at GAUL Level 2 granularity and treating small-area estimates as equally reliable to country-level aggregations.

Beige team tests are mandatory for any user-facing or decision-facing component.

---

### 🟩 Green Team Tests -- Supportive, Resilience-Oriented Testing

Green team tests focus on **ensuring the system works as intended** under expected conditions and degrades safely.

- **Goal:** ensure reliability, robustness, and trustworthiness
- **Mindset:** *"How do we make this solid?"*
- **Typical focus:**
  - Correctness and performance validation
  - Calibration and consistency checks
  - Monitoring and observability
  - Drift detection
  - Guardrails and fallback behavior

In views-faoapi, green team tests should include:
- HDI nesting enforcement: verifying that for any set of credible masses, the narrower interval is always contained within the wider one,
- tensor reshape roundtrip correctness: converting a `ForecastDataset` to tensor and back to DataFrame and confirming exact equality,
- geographic aggregation consistency: verifying that element-wise summation of cell-level distributions produces the same result regardless of cell ordering,
- cache TTL validation: confirming that the 3.5-week disk cache TTL expires correctly and that stale data is never served after expiry,
- metadata completeness: ensuring all required `_METADATA_COLS` are present and correctly aligned after `_preprocess_dataframe` fills missing combinations,
- API response schema stability: verifying that endpoint responses maintain their declared structure (shape, columns, parameters) across data refreshes,
- Appwrite configuration validation: confirming that all 8 `APPWRITE_*` environment variables are present and correctly typed at startup.

Green team tests are expected to pass continuously and form the backbone of CI.

---

## Relationship to Other ADRs

This ADR reinforces and operationalizes:

- **ADR-001 (Ontology):** tests must respect declared concepts and stability expectations
- **ADR-002 (Topology):** tests must not bypass architectural boundaries
- **ADR-003 (Authority & Semantics):** tests must fail loudly on semantic ambiguity
- **ADR-004 (Deferred):** future evolution rules must account for test coverage obligations

Testing is a primary mechanism by which these ADRs are enforced.

---

## Enforcement Rules

- Code that meaningfully affects behavior **must not be merged without tests**
- Tests that only cover happy paths are insufficient
- Warning-only behavior in tests is unacceptable for decision-relevant semantics
- If a failure mode is known and untested, it is considered technical debt and must be tracked explicitly

The absence of appropriate tests is valid grounds for blocking a change.

---

## Implementation: Layer-Based Test Markers

The red/beige/green taxonomy above defines the conceptual framework. In practice, tests are organized using a **5-layer numeric marker system** registered in `pyproject.toml`:

| Marker | Layer | Maps to | Description |
|--------|-------|---------|-------------|
| `layer1_storage` | Storage | Beige/Red | Appwrite SDK compatibility, file operations |
| `layer2_data` | Data | Green/Beige | Dataset construction, tensor operations, format cascade |
| `layer3_http` | HTTP | Beige/Red | API endpoint routing, response format, authentication |
| `layer4_infra` | Infrastructure | Green/Beige | Cache isolation, staleness detection, disk cache versioning |
| `layer5_audit` | Audit | Red | Falsification probes, invariant verification |

Tests are marked with `@pytest.mark.layerN_name` and can be selected via `pytest -m layer2_data`. The layer taxonomy complements the team taxonomy: each layer contains tests from multiple team categories, and each team category spans multiple layers.

---

## Consequences

### Positive
- Reduced risk of silent failure in forecast data delivered to the UN FAO
- Earlier detection of misuse and misunderstanding of statistical outputs
- Increased trustworthiness of HDI, MAP, and aggregation results
- Clearer system boundaries and guarantees across the Appwrite, API, and analysis layers

### Negative
- Higher upfront development cost
- Slower iteration if tests are neglected
- Requires cultural discipline and reviewer enforcement

These costs are accepted intentionally.

---

## Notes

Testing in this repository is not merely about correctness.

It is about **preventing harm, misunderstanding, and overconfidence**  
in systems that operate under uncertainty and pressure.
