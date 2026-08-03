# ADR-021: Dense Grid Fill Value Semantics

**Status:** Accepted  
**Date:** 2026-05-31  
**Deciders:** Simon (PRIO), Claude Code  
**Consulted:** views-datafactory ADR-024 (compilation grid invariants), ADR-035 (V-Dem as democracy source)  
**Informed:** CRAF'd API consumers  

---

## Context

`_ViewsDataset._preprocess_dataframe()` fills missing grid cell/month combinations with a scalar value to guarantee a dense grid — every entity that exists in the last month has a row for every month, even if the upstream data is sparse. The fill value was hardcoded as `0`.

This matches the upstream convention in views-datafactory, which uses `fill_value=0.0` by default (ADR-024) and enforces a dense `[T, H, W, C]` tensor grid. The datafactory consumer guide explicitly calls `df.fillna(0.0)` as a final step.

The concern (risk register D-04 / C-41) was that zero-fill is semantically loaded: for fatality predictions, a zero sample array and a missing-data cell are meaningfully different. If the API silently replaces missing data with zeros, downstream consumers cannot distinguish "model predicts zero fatalities" from "model had no data for this cell."

However, in the VIEWS pipeline's current design, this distinction does not exist at the point where data reaches the API. The postprocessor in views-models produces a grid that is intended to be dense, and any missing cells are holes in the grid rather than meaningful NaN signals. The datafactory's V-Dem exception (using NaN for missing democracy scores) demonstrates that when the distinction matters, it is handled upstream — not at the API layer.

A decision was needed because the API is expanding to global coverage and adding a new global forecast model. Hardcoding `0` without documentation or configurability would risk silent semantic errors if a future model uses NaN to signal missing data.

---

## Decision

1. **Zero-fill is the correct default.** Missing grid cells are filled with `0` to produce a dense grid, matching the views-datafactory convention.

2. **Fill value is configurable.** `_ViewsDataset` accepts a `fill_value` parameter (default `0`) that flows through to `_preprocess_dataframe()`. `ForecastDataset` exposes the same parameter. `CrafdApiManager` can pass it through at dataset construction time.

3. **This decision mirrors views-datafactory.** If views-datafactory changes its fill convention (e.g., adopts NaN for a new data source or model), this repo should follow. The coupling is intentional: both repos serve the same pipeline and must agree on grid semantics.

---

## Rationale

- **Consistency with upstream:** views-datafactory uses `fill_value=0.0` by default (ADR-024). Diverging would create a semantic mismatch between the data production and data serving layers.
- **Dense grid invariant:** Downstream consumers (FAO analysts, aggregation code, HDI-map computation) assume a complete grid. Sparse grids with NaN would require defensive null-handling throughout the API.
- **Configurability over hardcoding:** Making the value configurable costs almost nothing but prevents a future breaking change if a model requires NaN semantics. views-datafactory already does this — we follow the same pattern.
- **Not the API's job to reinterpret:** If a model produces NaN to mean "no prediction," that semantic should be preserved by setting `fill_value=float('nan')` at construction time, not by changing the default.

---

## Considered Alternatives

### Alternative A: Always use NaN for missing cells
- **Pros:** Preserves missing-data semantics; downstream can distinguish "no data" from "zero prediction"
- **Cons:** Breaks all existing aggregation and HDI-map code that assumes dense grids; diverges from datafactory convention; every consumer must handle NaN
- **Reason for rejection:** The current pipeline does not produce meaningful NaN — missing cells are grid holes, not data signals

### Alternative B: Keep hardcoded zero, no configurability
- **Pros:** Simplest; no new parameters
- **Cons:** Cannot accommodate future models that use NaN semantically; blocks alignment with datafactory if it changes
- **Reason for rejection:** Minimal cost to make configurable; high cost if we need to change later under global coverage

---

## Consequences

### Positive
- Dense grid invariant is now an explicit, documented decision rather than an implicit hardcoded assumption
- Configurable fill_value allows future models or data sources to override the default without code changes
- Alignment with views-datafactory is explicit and traceable

### Negative
- Adds a constructor parameter to `_ViewsDataset` and `ForecastDataset` (minor API surface increase)
- If views-datafactory changes its convention, this repo must follow — the coupling is explicit but real

---

## Implementation Notes

- `_ViewsDataset.__init__()` gains `fill_value: float = 0` parameter, stored as `self._fill_value`
- `_ViewsDataset._preprocess_dataframe()` uses `self._fill_value` instead of hardcoded `0`
- `ForecastDataset.__init__()` passes `fill_value` through to `super().__init__()`
- `CrafdApiManager._get_latest_dataframe()` can pass `fill_value` from config at dataset construction time (not wired yet — current default is sufficient)
- No changes to aggregation, subsetting, or HDI-map code — they continue to receive a dense grid

---

## Validation & Monitoring

- Existing tests continue to pass (they use the default `fill_value=0`)
- A test should verify that `fill_value=float('nan')` produces NaN in filled cells
- If the API ever switches to NaN fill, aggregation tests will surface failures immediately (NaN propagates through mean/sum)

---

## Open Questions

- If views-datafactory adopts per-variable fill values (e.g., 0 for fatalities, NaN for democracy), should this API support per-column fill? Currently out of scope — the parameter is a single scalar.

---

## References

- Risk register: D-04 (disagreement on zero-fill semantics), C-41 (zero-fill concern)
- views-datafactory ADR-024: Compilation grid invariants (dense grid, configurable fill_value)
- views-datafactory ADR-035: V-Dem as democracy source (NaN exception)
- views-datafactory `compilation_config.py:71`: `fill_value=0.0` default
- views-datafactory `consumer_data_guide.md:79`: `df.fillna(0.0)` in consumer guide
