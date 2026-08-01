# ADR-025: FAO Output Schema and Column Naming

**Status:** Accepted
**Date:** 2026-06-26
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** ADR-024 (raw-count serving contract), ADR-023 (re-baselining governance), ADR-003 (declarations over inference), views-models ADR-012 (target scale/prefix), views-datafactory (`ged_*_best` naming), views-frames-summarize (`hdi_tower`, `tower_point`, `expected_shortfall`)
**Informed:** UN FAO API consumers

---

## Context

faoapi hands FAO its forecasts two ways: an interactive JSON API, and a **bulk artifact** (a single file FAO loads in pandas/polars to get everything at once — much faster than paging JSON when you want the whole picture). The bulk artifact is the focus here.

Two gaps forced this decision:
1. **No output-schema / column-naming convention exists anywhere in faoapi.** The current served columns are an undocumented, hand-rolled set (`{var}_hdi_lower`, `{var}_hdi_upper`, `{var}_map`, `{var}_min`, `{var}_max`) — one HDI, plus a raw `min`/`max`. There is no ADR governing what FAO receives or how columns are named.
2. **The upstream prefix conventions disagree and mean nothing to FAO.** views-datafactory names raw count series `ged_sb_best`; views-models ADR-012 uses the `lr_` linear prefix (`lr_sb_best`); faoapi's legacy data carries the deprecated `ln_` prefix (`pred_ln_sb_best`, register C-142). `ged_`/`lr_`/`ln_`/`pred_` are internal VIEWS plumbing semantics — an FAO analyst loading a parquet should not have to decode them.

All served values are **raw fatality counts** (ADR-024). The estimators are already available in views-frames-summarize: `tower_point` (MAP), `hdi_tower` (nested HDIs), `expected_shortfall` (tail-mean worst case).

---

## Decision

### 1. The FAO bulk artifact is a parquet at admin-1 grain, wide format

One row per `(month_id, admin1_code)`, with the country (GAUL admin-0) denormalized onto the row for convenient filtering, covering both forecast and historical months. Forecast columns are populated for forecast months; the historical column is populated for months with observed actuals and `NaN` otherwise. (Per-cell PRIO-GRID access — which additionally carries `priogrid_id`, `lat`, `lon` — and other aggregation levels remain via the JSON API; this ADR governs the bulk admin-1 product and the naming convention used everywhere.)

### 2. Three violence series: `sb`, `ns`, `os`

State-based (`sb`), non-state (`ns`), one-sided (`os`) — each forecast and reported independently.

### 3. Column names are consumer-facing — internal prefixes are not carried into output

No `ged_`, `lr_`, `ln_`, or `pred_` prefix appears in FAO output. Series stems are the bare `sb` / `ns` / `os`. This applies to the JSON output columns as well, going forward. (Internal input column names, whatever the producer sends, are a separate concern — faoapi consumes them but does not propagate their prefixes to FAO; ADR-024 / C-142.)

### 4. Canonical column schema (36 columns)

| Group | Columns |
|-------|---------|
| Identity (6) | `month_id`, `admin1_code`, `admin1_name`, `country_code`, `country_name`, `country_iso3` |
| Per series `s ∈ {sb, ns, os}` (10 × 3 = 30) | `s_map` · `s_hdi50_lower` · `s_hdi50_upper` · `s_hdi90_lower` · `s_hdi90_upper` · `s_hdi95_lower` · `s_hdi95_upper` · `s_severe_scenario` · `s_bimodality_flag` · `s_actual` |

> **`s_bimodality_flag`** — the conservative 0/1 secondary-mode flag (§A.3 of FAO Pre-Release Note 06). Delivered as a per-series column (epic #222) — surfaced from the views-frames `bimodality` detector via `estimator.collapse`, so a materially-populated second mode is never silently collapsed to one number. This makes the served schema **36 columns** (the earlier 33-column draft omitted it as an open A.3 item; it is now built and testable).

**Identity columns are grounded in the real upstream GAUL schema** (`views-postprocessing/unfao/gaul_schema.py`), not UN M49:
- `admin1_code` / `admin1_name` — GAUL admin-1 (`gaul1_code` / `gaul1_name` upstream, renamed `admin1_gaul1_*` in the 9-column metadata contract).
- `country_code` / `country_name` — **GAUL admin-0** (`gaul0_code` / `gaul0_name`; this is FAO's GAUL country, *not* UN M49).
- `country_iso3` — ISO-3166 alpha-3 (`iso3_code` / `country_iso_a3`), provided alongside for interoperability.
- Consumer-facing names are presented to FAO (`admin1_code`, not the awkward internal `admin1_gaul1_code`); the upstream→output mapping is one-to-one.
- **`lat` / `lon`** are the PRIO-GRID cell-centre coordinates (`pg_xcoord` = longitude ∈ [−180, 180], `pg_ycoord` = latitude ∈ [−90, 90]; genuine WGS84 degrees). They apply **only at the PRIO-GRID (pgm) grain** (the JSON pg endpoints); the admin-1 bulk parquet, being an aggregate, carries no single point and therefore no `lat`/`lon`.

Definitions (all in **raw counts**, ADR-024):
- **`s_map`** — point estimate: the tower MAP (`views_frames_summarize.tower_point`).
- **`s_hdi{50,90,95}_{lower,upper}`** — the three nested highest-density intervals at masses **0.50 / 0.90 / 0.95**, from `hdi_tower(masses=(0.50, 0.90, 0.95))`. The mass is encoded as a percent in the name.

  > **Credible levels are 50 / 90 / 95, per the signed LoA (Output 1 / Activity 4)** and FAO Pre-Release Note 06 (Topic C, locked 2026-07-24). An earlier draft of this ADR said `50/90/99`; the `99` was an engineering-schema preference with **no contractual basis** — it bled over from **QS99** (a model-scoring guardrail in Release Note 05), not a reporting level. Corrected to 50/90/95 in epic #100/#222 (register [C-168-adjacent]; `views-faoapi` memory `project_fao_credible_levels`). A **50/95/99** set is available *only* as an optional heavier-tail alternative if FSFC requests it (the heavy tail is already summarised by `s_severe_scenario`); do not adopt it without an FSFC ask.
- **`s_severe_scenario`** — the **mean of the worst 5% of posterior draws** (`expected_shortfall(tails=(0.05,))`) — a coherent, reproducible worst-case severity. Deliberately **not** the raw sample maximum (which is high-variance and non-reproducible; views-frames-summarize refuses to offer `max` for this reason). Named `severe_scenario` for honesty (it is a tail *mean*, not an absolute ceiling) and interpretability.
- **`s_actual`** — the historical observed count for that series, month, and unit; `NaN` for months with no actuals (i.e. the forecast horizon).

### 5. Deliberate omissions

- **No `low` / best-case column.** For non-negative counts a low estimate bottoms out at ~0 and is already bounded by `s_hdi95_lower`; views-frames-summarize has no best-case shortfall to give it a distinct meaning. Omitted; re-add only if a concrete decision use emerges (register: revisit).
- **No raw `min` / `max`.** The raw sample max is volatile and non-reproducible (replaced by `s_severe_scenario`); the raw min is the same problem on the floor side and is covered by `s_hdi95_lower`.

### 6. Scale and governance

All values are raw counts (ADR-024). The bulk parquet's published numbers are subject to the re-baselining governance in ADR-023 (a change to the estimator/aggregation that moves these values is a re-baseline).

### 7. This amends FAO Release Note 01 (a consumer-facing change)

Release Note 01 locked the API payload format (**Custom JSON**, with parquet/arrow *explicitly out of scope*) and the field naming (UN M49 country, GAUL `ADM1_CODE`/`ADM1_NAME`, `month_id`, `lat`/`lon`, and model-output fields "unchanged"), and FAO confirmed alignment. This schema **amends Release Note 01** on three points, each of which must be communicated to FSFC (RN01 permits schema updates "in future phases, subject to mutual agreement"):
1. **Format addition** — a bulk **parquet** is added alongside the JSON API (RN01 had excluded it). This is an addition, not a breaking change.
2. **Country identifier — GAUL admin-0 + ISO-3 delivered; UN M49 raised with FAO as an open item.** RN01 named UN M49 as the default country identifier, and the signed LoA (Output 5) names UN M49 country codes as a deliverable — both FAO-confirmed. The API delivers **GAUL admin-0** (code + name) **and ISO-3**, *both of which FAO also confirmed as acceptable in RN01*. M49 is **not** served — and this is not a silent drop: M49 is absent from the upstream GAUL/postprocessing chain, and, unlike ISO-3 and GAUL, the M49 standard does not assign codes to every territory in the forecast grid, so delivering it would first require a territory-representation decision plus an ISO-3→M49 crosswalk build across the producer repos. This is raised with FAO as an explicit open question (deliverable-mapping mail, 2026-07-24): either reflect ISO-3 + GAUL in a short LoA update so the contract matches delivery, or align on building M49. Resolution folds into this RN01 amendment. *(An earlier draft of this ADR framed M49 as a "correction" and claimed RN01's M49 "never matched the pipeline"; that was inaccurate — M49 is a genuine, FAO-confirmed contract commitment we are addressing openly, not a mistake to wave away.)*
3. **Value-column naming + shape change** — the forecast value columns move to clean, prefix-free names and the richer 3-HDI + `severe_scenario` + `actual` shape (replacing the single HDI + raw `min`/`max`). The same naming applies to the JSON output, so JSON consumers built against the earlier columns are affected. FSFC has a live API and may have built tooling against the old columns; the change is coordinated, with an old→new mapping, before it becomes the default.

---

## Rationale

- **Name for the consumer, not the pipeline.** FAO is an external humanitarian consumer; `sb_hdi90_upper` is self-describing, `lr_sb_best_hdi90_upper` is not. The internal prefix wars (`ged_` vs `lr_` vs `ln_`) are irrelevant to FAO and actively confusing (C-142).
- **Three HDIs + MAP + a robust worst case is the right decision surface.** Planners need a central estimate (`map`), an uncertainty band at a few credible levels (50/90/95), and a defensible "how bad could it plausibly get" (`severe_scenario`) — without the volatility of a raw max.
- **Reuse the validated estimators.** `tower_point`, `hdi_tower`, `expected_shortfall` are already built, tested, and conformance-gated in views-frames-summarize; faoapi composes them, it does not re-implement.
- **Honesty in the worst-case name.** `severe_scenario` does not overclaim (unlike `worst_case`/`max`) and is interpretable without statistics training.

---

## Considered Alternatives

### A: Mirror the upstream `ged_*_best` (or `lr_*`) names in output
- **Pros:** direct column-lineage to the producer.
- **Cons:** leaks internal source/scale semantics to FAO; forces a choice between two disagreeing upstream conventions; verbose.
- **Rejected** in favour of clean consumer names (Decision 3).

### B: Keep the current `{var}_{hdi_lower,hdi_upper,map,min,max}` (one HDI + raw min/max)
- **Pros:** zero change.
- **Cons:** one HDI is a thin uncertainty surface; raw `min`/`max` are volatile and non-reproducible; undocumented.
- **Rejected.**

### C: Include a `low` / best-case column
- **Pros:** symmetry with `severe_scenario`; matches some stakeholders' expectation.
- **Cons:** no distinct meaning for non-negative counts (≈0 or = `hdi95_lower`); no estimator exists for it.
- **Rejected now**, with the door left open if a concrete use appears.

---

## Consequences

### Positive
- FAO gets a documented, self-describing, raw-count schema with a richer, more honest uncertainty surface (3 HDIs + severe scenario) than today.
- A single naming convention now governs both the bulk parquet and the JSON output.

### Negative / work implied
- **✅ DELIVERED in epic #222 (S1–S6, 2026-07-24).** The items below described the work when this
  ADR was written; it is now built on `main`: the 3-HDI + severe-scenario columns
  (`forecast/summarize/estimator.collapse`, `serialize/json_contract.series_value_dataframe`), the
  `sb/ns/os`/identity naming (`serialize/schema`, `to_consumer_columns`), and the admin-1 bulk
  parquet with `actual` (`serialize/bulk_parquet`). Production cutover (deploy) is the ADR-023
  gate (methodology v3 + `reports/adr025_output_schema/rebaseline_diff.md` + maintainer sign-off +
  FSFC comms).
- ~~This is a new capability, not the current behaviour.~~ (historical) Today faoapi emits 5 columns/var (1 HDI + `map` + raw `min`/`max`) as JSON and passes the raw upstream parquet through untouched. Delivering this schema requires: building the 3-HDI + severe-scenario + actual columns in `forecast/serialize/json_contract.py`, and a bulk-parquet writer. That is downstream implementation work (its own phase), gated by ADR-023 for the value change.
- Dropping `min`/`max` and changing column names is a **consumer-visible contract change** — coordinate with FAO and record under ADR-023 before it reaches `main`.

---

## Implementation Notes

- `forecast/summarize/estimator.py` already wraps `tower_point` + `hdi_tower`; extend it (or `serialize/json_contract.py`) to also call `hdi_tower(masses=(0.50,0.90,0.95))` and `expected_shortfall(tails=(0.05,))`, returning index-aligned numpy (views-frames ADR-017) that `serialize/json_contract.py` assembles into the named columns.
- The bulk parquet writer should write via `pyarrow` (or views-frames' own arrow IO) directly — pandas is **not** required for the write (consistent with the "pandas out of the engine" goal); a thin DataFrame→parquet is acceptable but optional.
- `s_actual` is joined from the historical series at the same admin-1 grain; align on `(month_id, admin1_code)`.
- The identity columns are derived from the upstream 9-column GAUL metadata contract (`views-postprocessing/unfao/gaul_schema.py`): `admin1_code`/`admin1_name` ← `admin1_gaul1_code`/`_name`; `country_code`/`country_name` ← `admin1_gaul0_code`/`_name`; `country_iso3` ← `country_iso_a3`. The pgm JSON additionally exposes `lat` ← `pg_ycoord`, `lon` ← `pg_xcoord`.

---

## Open Questions

- **The external FAO contract's exact label strings** — this ADR defines clean names (`sb_map`, …); if the signed FAO contract specifies different strings, reconcile (and update here). **Still open** (maintainer-verification pending): to be checked against the signed contract; a label-string mapping is offered to FSFC (C-2 ask). Not blocking — served names are the consumer-facing ones.
- ~~**Whether the JSON endpoints adopt this same schema**~~ — **RESOLVED (epic #222 / S4):** the served path adopts the schema; `calculate_hdi_map` emits var-keyed quantity columns, consumer-renamed (`sb/ns/os`) at the API boundary (`json_contract.to_consumer_columns`). No legacy shape retained.
- **The severe-scenario tail level** — **fixed at the worst 5% (`t=0.05`), shipped (#222 S2)** in `severe.expected_shortfall`; revisit only with evidence.
- **Identity naming is resolved** (Decision 4): GAUL admin-1 (`admin1_code`/`_name`), GAUL admin-0 country (`country_code`/`country_name`) + `country_iso3`, grounded in `views-postprocessing/unfao/gaul_schema.py`. **Implemented** (`schema.IDENTITY_SOURCE`, applied by the bulk writer). Open only insofar as the external FAO contract may dictate specific label strings (above).
- **Credible levels are 50/90/95** (signed LoA, Output 1). An earlier draft said `50/90/99` — the `99` had no contractual basis (QS99 spillover); corrected in #222 (see §4 note). A `50/95/99` set stays available only as an **optional** heavier-tail alternative on FSFC request.
- **The RN01 amendment** (Decision 7) — **maintainer-owned, in flight (S7):** communicate to FSFC + reflect in a Release Note / pre-release Note 06 before the deploy makes the new schema the served default. This is ADR-023 cutover artifact #4.

---

## References

- faoapi **ADR-024** — Raw Count Serving Contract
- faoapi **ADR-023** — Re-baselining Governance
- views-models **ADR-012** — Target Scale and Prefix Convention (`lr_` linear; `ln_` deprecated)
- views-datafactory — `ged_sb_best` / `ged_ns_best` / `ged_os_best` raw-count naming
- views-frames-summarize — `tower_point`, `hdi_tower` (ADR-019), `expected_shortfall` (ADR-022, PR #122)
- Risk register — **C-142** (deprecated `ln_` prefix), **C-86** (provenance/methodology surface)
