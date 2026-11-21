# Re-baseline record: the summary columns of #204 (methodology v4)

ADR-023 requires a recorded before-and-after for every change to how the published numbers are
computed. This is the record for `crafdapi-methodology/4` (#204, 2026-11-21).

**What changed.** The served summary columns, per outcome, were replaced by the set decided
with Simon on 2026-11-17 (`docs/api/data_dictionary.md`):

- **added:** `p_gt0`, `p_gt500`, `mean`, `mean_if_any`, `median_if_any`, `n_draws`;
- **kept:** `p_gt25`, `p_gt100`, `p_gt1000`, `severe_scenario`, `actual`;
- **removed:** `map`, the six `hdi50/90/95` bounds, `bimodality_flag`.

Every summary is now computed on the usable (finite) draws only, and `severe_scenario` is empty
below 20 usable draws. Both rules change nothing on a delivered run, whose draws are all finite
(ingest refuses others, C-72) and number 128 per row.

**Evidence.** Measured 2026-11-21 on the cached delivered run
`rusty_bucket_forecasting_20260727_095355`, first forecast month (559, 2026-07), state-based
violence, all 64,742 grid cells, through `ForecastDataset.summarize` (this change) and
`calculate_hdi_map` (the `development` branch before it).

The kept columns are **identical, value for value**, old against new (`np.array_equal`, largest
difference 0.0): `severe_scenario`, `p_gt25`, `p_gt100`, `p_gt1000`. This was measured at grid-cell
level only. Region summaries were not compared; they go through the same reduction on summed
draws, and the synthetic golden pins them from here on.

The removed columns, as the old code served them (share of cells):

| Column | 0 | Largest |
|---|---|---|
| `map` | 99.9% | 704 |
| `hdi50_lower` / `hdi50_upper` | 99.9% / 99.7% | 433 / 704 |
| `hdi90_lower` / `hdi90_upper` | 100.0% / 98.6% | 38 / 7,174 |
| `hdi95_lower` / `hdi95_upper` | 100.0% / 97.8% | 38 / 8,526 |
| `bimodality_flag` | 100.0% | 1 |

The added columns, as the new code serves them (share of cells):

| Column | Empty | 0 | Largest |
|---|---|---|---|
| `p_gt0` | 0% | 97.8% | 1 |
| `p_gt500` | 0% | 100.0% | 0.5625 |
| `mean` | 0% | 97.8% | 901.1 |
| `mean_if_any` | 97.8% | 0% | 1,802 |
| `median_if_any` | 97.8% | 0% | 679.5 |
| `n_draws` | 0% | 0% | 128 |

`mean_if_any` and `median_if_any` are empty exactly where `p_gt0` is 0: where no draw has any
deaths, "deaths if any" has no answer, and it is left empty rather than shown as 0.

**Reproduce.** Check out the commit before and after this change; on each, load the cached
forecast slot with `ForecastDataset.from_value(..., mmap=True)` and summarise month 559 for
`pred_lr_ged_sb` with `with_metadata=False`.
