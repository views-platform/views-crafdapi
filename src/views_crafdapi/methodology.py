"""Methodology version for crafdapi's published-forecast computation.

ADR-023 (governance gate for re-baselining published forecasts) requires a methodology
version that is bumped whenever a change to how crafdapi computes the *published* numbers
(estimator, aggregation, fill semantics) ships to production. It is surfaced alongside the
served-artifact provenance (C-86) so "which method produced these numbers" is auditable
from served output and logs.

Bump this string when a re-baselining change reaches `main` (see ADR-023). History:
- v1 — original hand-rolled MAP/HDI (`PosteriorDistributionAnalyzer`).
- v2 — views-frames tower estimator (M1 swap, register C-81); the first re-baseline.
- v3 — ADR-025 output-schema re-baseline (epic #222): MAP + fixed 50/90/95 HDIs +
  `severe_scenario` (mean of the worst 5% of draws) + the per-series `bimodality_flag` (0/1
  secondary-mode flag); raw min/max dropped; consumer `sb/ns/os` names applied at the API
  boundary. No retained value moved (MAP + 90% band byte-identical, the flag is a new additive
  column); a schema enrichment, not a value shift.

  Two corrections to this entry, 2026-08-15:

  * It said "served schema is 36 columns". It is **45** — ADR-034 §3 later added three
    exceedance columns per series (`s_p_gt25/100/1000`), live since `449bc13`.
    `forecast/serialize/schema.py::bulk_columns()` is the authority (register C-244).
  * It cited `reports/adr025_output_schema/rebaseline_diff.md` for the before/after evidence.
    **That file has never existed** — `git log --all` over that path returns nothing, and
    `reports/` is not ignored, so it was not written rather than lost. ADR-023 requires a
    recorded diff for every re-baseline; for v3 that requirement was not met. Stated here
    rather than quietly dropping the citation, because a missing governance artifact and a
    broken link look identical once the link is deleted.
"""

METHODOLOGY_VERSION = "crafdapi-methodology/3"
"""Monotonic identifier for the published-forecast methodology (ADR-023)."""
