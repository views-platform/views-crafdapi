# FAO Forecast API — data dictionary (for analysts)

*What every served column means, in one place. The **authoritative source for the served column set
is the code** — `src/views_crafdapi/forecast/serialize/schema.py`. ADR-024 governs the raw-count
contract; ADR-025 §4 describes the pre-ADR-034 schema and is **no longer current** on the column set
(register C-244). This page is the analyst-facing summary.*

## Read this first

- **The fatality columns are raw counts.** Not rates, not log-counts. `sb_map = 3.0` means an
  estimated **3 fatalities** for that cell-month and violence type (ADR-024).
- **Not every column is a count.** Each column's unit is given in the tables below; three kinds
  appear:
  - **counts** — `s_map`, the six `s_hdi*` bounds, `s_severe_scenario`, `s_actual`
  - **probabilities in `[0, 1]`** — `s_p_gt25`, `s_p_gt100`, `s_p_gt1000`. `sb_p_gt25 = 0.4`
    means *a 40% chance of exceeding 25 fatalities* — **not** 0.4 fatalities (ADR-034 §3)
  - **flags and identifiers** — `s_bimodality_flag` (0/1), and everything in
    *Identity & geography* (`month_id`, coordinates in decimal degrees, ISO3 and GAUL names)
- **Ignore the `lr_` / `ln_` / `pred_` / `ged_` prefixes on data-endpoint column names.** They are
  legacy VIEWS naming and are **not** scale signals — `lr_ged_sb` is a raw count, *not* a log-rate.
  (ADR-024 §1–2: "a column named `ln_ged_sb` is not evidence that the values are in log-space.")
  The **forecast** analysis endpoint drops these prefixes entirely (`sb_map`, not `lr_..._map`).
  > ⚠️ **Do not use `/{level}/analysis/historical/hdi-map`.** At the default `aggregate=false` it
  > returns **HTTP 500** at every level (the reduction is defined for posterior samples; observed
  > data is not). With `aggregate=true` it does respond, but the `sb`/`ns`/`os` rename is applied
  > on the *forecast* response only, so you get internal names carrying the historical target stem
  > — e.g. `lr_ged_sb_p_gt25`. **The prefix-is-not-a-scale-signal rule above does not extend to
  > `*_p_gt*` columns:** `lr_ged_sb_p_gt25 = 1.0` is a *probability of 1.0*, not 1 fatality.
  > For observed values use `/{level}/data/historical/subset`. Tracked as register C-245.
- **Three violence series**, reported separately everywhere:
  `sb` = **state-based** armed conflict, `ns` = **non-state** conflict, `os` = **one-sided** violence.
- **Scope:** these are **conflict-fatality** forecasts — a *driver/input* to food-security analysis,
  **not** a food-security, IPC, or hunger output.

## The three vocabularies for the same quantity

The same underlying quantity is named differently depending on where you get it. This is the single
biggest source of confusion; here is the map (shown for state-based `sb`; `ns`/`os` are identical):

| Concept | Data endpoint (`fetch_subset`) | Analysis endpoint (`hdi_map`) | Offline demo (`03`) |
|---|---|---|---|
| the modelled quantity | `lr_ged_sb` (historical) / `pred_lr_ged_sb` (forecast) | — (collapsed below) | `pred_lr_ged_sb` |
| point estimate (MAP) | — | `sb_map` | `pred_lr_ged_sb_map` |
| 90% credible interval | — | `sb_hdi90_lower` / `sb_hdi90_upper` | `pred_lr_ged_sb_hdi90_lower/upper` |

`fetch_subset` returns the **raw posterior draws** (forecast) or the **observed count** (historical);
`hdi_map` on the **forecast** route returns them **collapsed** to a point estimate + credible
intervals with the clean `sb/ns/os` names. Same numbers, two representations. (The historical
`hdi_map` route is a different matter — see the warning above; use `fetch_subset` for observed
values.)

## Analysis columns (`client.hdi_map`, forecast) — per series `s ∈ {sb, ns, os}`

| Column | Meaning |
|---|---|
| `s_map` | **Count.** The posterior point estimate, in fatalities — a *tower point* estimator (`views_frames_summarize.tower_point`), which is neither the arithmetic mean nor a histogram mode. For the zero-inflated count posteriors served here those three differ materially, so **do not sum `s_map` across cells to get a regional total** — request the aggregate level instead, which sums the draws. |
| `s_hdi50_lower` / `s_hdi50_upper` | **Counts.** 50% highest-density interval — the narrowest band, covering half the posterior mass. |
| `s_hdi90_lower` / `s_hdi90_upper` | **Counts.** 90% highest-density interval. |
| `s_hdi95_lower` / `s_hdi95_upper` | **Counts.** 95% highest-density interval — the widest band, covering 95% of the posterior mass. |
| `s_severe_scenario` | **Count.** Mean of the worst 5% of posterior draws — a defensible "how bad could it plausibly get." **Not** the raw sample maximum. ⚠️ But if you subset draws with `sample_idx`, the worst-5% is `max(1, ceil(0.05 × S))` draws — so selecting **20 or fewer** collapses it to the single worst draw *of your subset*, which is typically **lower** and much noisier than the true worst-5% mean. It reads as reassuring while under-stating severity. `k` first reaches 2 at S=21 (register C-247). |
| `s_bimodality_flag` | **Flag**, not a count: `0.0` / `1.0` as float64, and **`NaN`** where the posterior is entirely missing. `1` = a clearly separated secondary mode, so a single MAP/HDI should be read **with care**. The detector is deliberately conservative (ADR-025 §A.3), so `0` means *none was detected* — not that the posterior is unimodal. `df.sb_bimodality_flag == 0` silently drops the NaN rows; `.astype(int)` raises on them. |
| `s_p_gt25` | **A probability in `[0, 1]`, not a count.** The posterior probability that `s` **strictly exceeds** 25 fatalities in that unit-month — the fraction of posterior draws above the threshold. `NaN` where the posterior is entirely missing (never a spurious `0`). |
| `s_p_gt100` | Same, for **100** fatalities. |
| `s_p_gt1000` | Same, for **1000** fatalities. |

Exceedance is an empirical fraction over the draws, so its resolution is **1/S**: `0.0` means *no
draw exceeded the threshold*, **not** "impossible". Subsetting with `sample_idx` coarsens it
further — down to `{0, 1}` for a single draw.

Credible levels are currently **50 / 90 / 95** (ADR-025 §4; CRAF'd confirmation is still an open
item in ADR-034). A *higher* level gives a **wider** interval — wider means *more* uncertainty about
where the value lies, not a better estimate. Use `hdi50` for the tightest plausible range and
`hdi95` for the most cautious one; `hdi95` is not "more reliable" than `hdi50`.

`enforce_non_negative` is a query parameter that **defaults to `false`**; when enabled it clips
**only the MAP**, leaving the HDI bounds and `s_severe_scenario` untouched. In practice you should
never see a negative served value regardless: negative posterior draws are **refused at ingest**
with an error, so they cannot reach the API (ADR-024 §4 / register C-72).

> **The exceedance thresholds are provisional.** `25 / 100 / 1000` are placeholders chosen by the
> operator pending CRAF'd sign-off (ADR-034 §3, still **Proposed**). If CRAF'd confirms different
> cutpoints, **the column names change with them** (`sb_p_gt25` → `sb_p_gt10`, and so on) — the
> threshold is part of the name by design, so a value change can never hide inside an unchanged
> column. **Derive these column names rather than hard-coding them.**

## Identity & geography columns

These are the geographic-context columns at **PRIO-GRID grain** (`with_metadata=True`).

> **The set shrinks when you pass `aggregate=true`.** An aggregated response drops `priogrid_id`,
> `pg_xcoord` and `pg_ycoord`, and also drops the **parent GAUL *codes*** while keeping their
> names — so at `gaul1` you get `admin1_gaul0_name` but **not** `admin1_gaul0_code`, and a
> country-code join has no key. Served counts under `aggregate=true`: `pg` 11 · `gaul2` 6 ·
> `gaul1` 5 · `gaul0` 4 · `country` **2**. (`with_metadata=false` is ignored on the aggregate
> path — you get metadata anyway.) Do not write one metadata-joining helper against the full
> table and point it at country rows.

| Column | Meaning |
|---|---|
| `month_id` | VIEWS integer month id. `month_id 1 = January 1980`; `month_id = (year−1980)×12 + month`. Use `views_crafdapi.time` helpers. |
| `priogrid_id` | PRIO-GRID cell id (a fixed 0.5°×0.5° global grid, 720×360 cells). |
| `pg_xcoord` / `pg_ycoord` | Longitude / latitude of the cell **centre** (quarter-degrees, e.g. `41.25`). |
| `country_iso_a3` | ISO-3166 alpha-3 country code (from GAUL admin-0). |
| `admin1_gaul0_code` / `admin1_gaul0_name` | GAUL admin-0 (country) code / name. |
| `admin1_gaul1_code` / `admin1_gaul1_name` | GAUL admin-1 (province/state) code / name. |
| `admin2_gaul2_code` / `admin2_gaul2_name` | GAUL admin-2 (district) code / name. |

GAUL = FAO's own **Global Administrative Unit Layers**. Country codes are GAUL admin-0, **not** UN M49.

> The separate **admin-1 bulk parquet** product (`GET /data/forecast/bulk`) renames the identity
> block to `month_id`, `admin1_code`, `admin1_name`, `country_code`, `country_name`,
> `country_iso3` (6) and carries all **13** per-series columns including `s_actual`, for
> 6 + 3 × 13 = **45 columns**. The **forecast** JSON `hdi_map` response carries 12 of the 13
> (no `s_actual`).
>
> **The authority for this layout is the code**, `forecast/serialize/schema.py::bulk_columns()`.
> ADR-025 §4 still describes the pre-ADR-034 36-column schema and ADR-034's own served-column
> plan lists a different 36; neither is current (register C-244).
>
> **The 13th column, bulk-only:** `s_actual` — the historical **observed** value for that
> unit-month, a count. It is `NaN` wherever no observation exists, which is **every forecast
> month** — so an all-`NaN` `s_actual` column is the *normal* state of a forward-looking
> download, not a fault.
>
> One caveat: if the historical join fails server-side the column is also all-null, and the
> download still returns HTTP 200. The discriminator is the **dtype** — a real join yields a
> numeric column (`float32`), a failed one yields `object`. Note `.sum()` on the failed column
> returns `0`, not `NaN` (register C-248).

## Aggregation levels

`level ∈ {pg, country, gaul0, gaul1, gaul2}`. When aggregating forecasts to a higher level, the service
**sums aligned posterior draws** across constituent cells *before* collapsing — so the aggregate's
uncertainty is honest (the HDI of the sum, **not** the sum of the HDIs).

> **`level` alone does not aggregate.** `aggregate` defaults to **`false`**, so naming a level
> without it returns **cell-level rows** indexed by `priogrid_id` — while the response envelope
> still reports `"level": "gaul1"`. Reading one such row as a province value, or summing point
> estimates across them, is exactly the "sum of the HDIs" error this section warns against. Pass
> `aggregate=true`.

> **Exceedance is not comparable across levels.** The `s_p_gt{c}` thresholds are absolute counts
> applied to whatever posterior you asked for, so at an aggregated level they answer
> *"P(the whole province exceeds 25)"*, not *"P(a cell exceeds 25)"*. The same posterior can give
> `sb_p_gt25 = 0.0` at every cell and `1.0` at country level. Filtering `sb_p_gt25 > 0.5` across
> levels compares different questions.
