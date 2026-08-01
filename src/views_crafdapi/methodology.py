"""Methodology version for faoapi's published-forecast computation.

ADR-023 (governance gate for re-baselining published forecasts) requires a methodology
version that is bumped whenever a change to how faoapi computes the *published* numbers
(estimator, aggregation, fill semantics) ships to production. It is surfaced alongside the
served-artifact provenance (C-86) so "which method produced these numbers" is auditable
from served output and logs.

Bump this string when a re-baselining change reaches `main` (see ADR-023). History:
- v1 — original hand-rolled MAP/HDI (`PosteriorDistributionAnalyzer`).
- v2 — views-frames tower estimator (M1 swap, register C-81); the first re-baseline.
- v3 — ADR-025 output-schema re-baseline (epic #222): MAP + fixed 50/90/95 HDIs +
  `severe_scenario` (mean of the worst 5% of draws) + the per-series `bimodality_flag` (0/1
  secondary-mode flag); raw min/max dropped; consumer `sb/ns/os` names applied at the API
  boundary; served schema is 36 columns. No retained value moved (MAP + 90% band byte-identical,
  the flag is a new additive column — see `reports/adr025_output_schema/rebaseline_diff.md`);
  a schema enrichment, not a value shift.
"""

METHODOLOGY_VERSION = "crafdapi-methodology/3"
"""Monotonic identifier for the published-forecast methodology (ADR-023)."""
