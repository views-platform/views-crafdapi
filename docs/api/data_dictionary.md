# FAO Forecast API — data dictionary (for analysts)

*What every served column means, in one place. Authoritative source: ADR-024 (raw-count contract),
ADR-025 §4 (output schema), and `src/views_crafdapi/forecast/serialize/schema.py`. This page is the
analyst-facing summary.*

## Read this first

- **All values are raw fatality counts.** Not rates, not log-counts, not probabilities. A value of
  `3.0` means an expected/estimated **3 fatalities** for that cell-month and violence type (ADR-024).
- **Ignore the `lr_` / `ln_` / `pred_` / `ged_` prefixes on data-endpoint column names.** They are
  legacy VIEWS naming and are **not** scale signals — `lr_ged_sb` is a raw count, *not* a log-rate.
  (ADR-024 §1–2: "a column named `ln_ged_sb` is not evidence that the values are in log-space.")
  The analysis endpoint drops these prefixes entirely (`sb_map`, not `lr_..._map`).
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
`hdi_map` returns them **collapsed** to a point estimate + credible intervals with the clean `sb/ns/os`
names. Same numbers, two representations.

## Analysis columns (`client.hdi_map`, forecast) — per series `s ∈ {sb, ns, os}`

| Column | Meaning |
|---|---|
| `s_map` | **MAP** point estimate — the most-probable value (mode) of the posterior, in fatalities. |
| `s_hdi50_lower` / `s_hdi50_upper` | 50% highest-density interval (narrowest, least confident). |
| `s_hdi90_lower` / `s_hdi90_upper` | 90% highest-density interval. |
| `s_hdi95_lower` / `s_hdi95_upper` | 95% highest-density interval (widest, most confident). |
| `s_severe_scenario` | Mean of the worst 5% of posterior draws — a defensible "how bad could it plausibly get." **Not** the raw sample maximum. |
| `s_bimodality_flag` | 0/1. `1` = the posterior has a clearly separated secondary mode, so a single MAP/HDI should be read **with care** (the distribution is not a simple bump). |
| `s_actual` | The historical **observed** count for that unit-month; `NaN` on the forecast horizon (no observation yet). |

Credible levels are fixed at **50 / 90 / 95** (signed-off, per ADR-025 §4). A *higher* level gives a
*wider* interval. `enforce_non_negative` clips the MAP at 0 — posterior draws can be slightly negative
and are clipped, since fatality counts cannot be below zero.

## Identity & geography columns

`fetch_subset` and `hdi_map` (JSON) return these geographic-context columns (`with_metadata=True`):

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

> The separate **admin-1 bulk parquet** product (ADR-025 §4) renames the identity block to
> `country_iso3`, `country_code`, `country_name`, `admin1_code`, `admin1_name` (6 identity + 3 series ×
> 10 = 36 columns). See `docs/api/README.md` and ADR-025 for that layout.

## Aggregation levels

`level ∈ {pg, country, gaul0, gaul1, gaul2}`. When aggregating forecasts to a higher level, the service
**sums aligned posterior draws** across constituent cells *before* collapsing — so the aggregate's
uncertainty is honest (the HDI of the sum, **not** the sum of the HDIs).
