# ADR-010: Technical Risk Register

| Field | Value |
|-------|-------|
| Status | Active |
| Date | 2026-05-27 |
| Decision | Maintain a technical risk register as a first-class governance artifact |

## Context

The views-faoapi repository serves critical conflict forecast data to the UN FAO. Structural risks identified during code review, assimilation, and audits need a persistent, tracked home so they are not lost between conversations or contributors.

## Decision

We adopt a technical risk register at `reports/technical_risk_register.md` with tiered severity (1-4), actionable triggers, and deduplication rules. Risks are registered via the `register-risk` skill and reviewed/prioritized via the `review-rr` skill.

## Consequences

- All audit outputs (repo-assimilation, expert-review, test-review, falsification-audit) feed into a single register.
- Risks are tracked until resolved; resolved entries are preserved for institutional memory.
- The register is manually maintained; header counts must be updated on each modification.
