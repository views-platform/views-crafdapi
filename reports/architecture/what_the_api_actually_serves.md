# What the API actually serves

*Written 2026-08-23. Every route below was called against the **real** cached artifacts —
28,421,738 historical rows and 2,330,712 forecast cells at 128 draws — through the real FastAPI
app with real route registration, offline. Where a payload carries values, those values were
re-derived independently from the sample store and compared. Nothing was run on the production box;
the artifacts were fingerprinted before and after and are unchanged.*

*The question being answered: **of the 23 served surfaces, how many actually deliver correct data,
and how many have never been looked inside?***

---

## The short version

**The repo is not wholesale non-functional, and the hypothesis that it might be is refuted.** The
forecast analysis path — the mathematically hardest thing here — is provably correct: every HDI
invariant holds and an independent recomputation from the raw draws matches the served numbers to
`max|diff| = 0`.

But four things are wrong, and the worst of them is on the one route the consumer actually uses.

| # | finding | severity |
|---|---|---|
| 1 | **`sb_actual` in the bulk artifact is structurally always zero** | the consumer-facing one |
| 2 | Both `/latest` routes serve rows with no values — confirmed on real data | C-232, now demonstrated |
| 3 | `/pg/analysis/historical/hdi-map` returns **500** | unhandled `ValueError` |
| 4 | Historical `hdi-map` serves zero-width "HDI" and 0/1 "probabilities" at four levels | semantically misleading |

---

## The matrix

| route | status | bytes | cols | verdict |
|---|---|---|---|---|
| `/data/forecast/latest` | 200 | **88,357,820** | **`[]`** | **SERVES HOLLOW** |
| `/data/historical/latest` | — | *not serialised* | **`[]`** | **SERVES HOLLOW** (frame layer) |
| `/pg/data/forecast/subset` | 200 | 9,998 | 12 | **SERVES CORRECT** |
| `/{country,gaul0,gaul1,gaul2}/data/forecast/subset` | 200 | 0.3–27.5 MB | 3–7 | values present |
| `/pg/data/historical/subset` | 200 | 2,260 | 12 | values present |
| `/{country,gaul0,gaul1,gaul2}/data/historical/subset` | 200 | 18 KB–3.4 MB | 3–7 | values present |
| `/{pg,country,gaul0,gaul1,gaul2}/analysis/forecast/hdi-map` | 200 | 6 KB–13.9 MB | 36–45 | **SERVES CORRECT** |
| `/pg/analysis/historical/hdi-map` | **500** | 73 | — | **CANNOT COMPLETE** |
| `/{country,gaul0,gaul1,gaul2}/analysis/historical/hdi-map` | 200 | 0.2–17.8 MB | 36–40 | **MISLEADING** |
| `/data/forecast/bulk` | built | 45 cols × 87,552 | 45 | **correct except `*_actual`** |

All probes used bounded parameters (one month, five cells, or one month aggregated). The unbounded
behaviour is already measured under C-284 at 34.5 GB and was deliberately not re-run.

---

## 1. The bulk `actual` columns are always zero

This is the finding that matters, because `/data/forecast/bulk` is the route CRAF'd consumes.

`build_bulk_table` produces 45 columns across 87,552 rows. Three of them are the historical
comparison columns:

```
sb_actual: non-null 2,432 / 87,552   nonzero 0
ns_actual: non-null 2,432 / 87,552   nonzero 2
os_actual: non-null 2,432 / 87,552   nonzero 3
```

**Why 2,432 of 87,552.** The artifact spans 36 forecast months × 2,432 admin-1 units. Historical
covers months 121–559; the forecast covers 559–594. **The overlap is exactly one month:**

```
historical months 121..559 (n=439)
forecast   months 559..594 (n=36)
overlap: [559]
```

So by construction **at most 1 of 36 months — 2.8% of rows — can ever carry an `actual`.** That is
structural, not a bug in itself.

**Why zero.** That single overlap month is empty. Total `lr_ged_sb` per month at the tail of the
historical series:

```
month 550: sum=13383   cells=64742
month 551: sum= 4001   cells=64742
...
month 558: sum= 2974   cells=64742
month 559: sum=    0   cells=64742
```

Months 550–558 each carry thousands of fatalities. Month 559 — the *only* month that reaches the
bulk artifact — is exactly zero across all 64,742 cells.

**The consumer-visible consequence.** The `actual` columns are served as `0.0`, not `null`. A
consumer plotting forecast-versus-actual gets a flat zero line and **no signal that the data is
absent rather than genuinely zero**. This is the "healthy-looking wrong answer" class again, on the
delivery path, unflagged.

What this does *not* establish: whether month 559 is legitimately not-yet-observed (an intentional
placeholder) or whether the historical artifact's last month is truncated. Both produce the same
consumer experience; the distinction matters for the fix and is upstream of this repo.

## 2. Both `/latest` routes are hollow — now demonstrated on real data

C-232 was verified on the repo's own toy fixture. It is now confirmed at production scale:

```
forecast     served frame: shape=(2330712, 0) columns=[]
historical   served frame: shape=(28421738, 0) columns=[]
```

`/data/forecast/latest` returns **HTTP 200 with 88,357,820 bytes** in 9.04 s — 88 MB of rows
carrying nothing but `month_id` and `priogrid_id`. The envelope reports `"columns": []`, which is
the field any test could have asserted at any point.

`/data/historical/latest` was not serialised: C-284 measured it at 12.9 GB and re-running it would
take the dev machine down. Its frame layer was probed directly and is equally empty.

## 3. `/pg/analysis/historical/hdi-map` returns 500

```
ValueError: HDI and MAP calculation only valid for prediction dataframes
  grid_dataset.py:1255, via forecast_dataset.py:685
```

Historical data is point observations — `is_prediction=False`, `sample_size=None`. There is no
posterior to summarise, so refusing is right. Returning an unhandled `ValueError` as a **500** is
not: a 500 says "the server broke", when the truthful answer is "this request is not meaningful for
this data", which is a 4xx.

## 4. The same request at four other levels does not refuse at all

The identical semantic invalidity, routed through `aggregate=True`, **succeeds**:

```
                 map   hdi50_lo  hdi50_hi  hdi90_lo  hdi90_hi  hdi95_lo  hdi95_hi  severe   p_gt25
UKR (month 540) 2268.0   2268.0    2268.0    2268.0    2268.0    2268.0    2268.0   2268.0    1.0
PSE              718.0    718.0     718.0     718.0     718.0     718.0     718.0    718.0    1.0
SDN              664.0    664.0     664.0     664.0     664.0     664.0     664.0    664.0    1.0
```

Measured across all 33 countries with non-zero values at month 540:

- **HDI90 and HDI95 width: max = 0.** Every interval is degenerate.
- `MAP == hdi90_lower == hdi90_upper` — exactly.
- `severe_scenario == map`.
- `p_gt25` and `p_gt100` take only `0.0` or `1.0`.

**To be precise, and this matters: the numbers are not fabricated.** MAP equals the observed value
exactly — `max|diff| = 0` against an independent aggregation of the source. Nothing is invented.

What is wrong is the *contract*. A consumer reading `hdi90_lower`/`hdi90_upper` reasonably expects a
credible interval and receives a point value repeated; one reading `p_gt25 = 1.0` reads "certain"
where the truth is "the single observed value exceeded 25". Five routes for the same data disagree
about whether the request is even answerable — one 500s, four answer with degenerate fields.

## 5. What is provably correct

**The forecast analysis path.** At pg level, month 559, 64,742 cells, 128 draws:

| invariant | result |
|---|---|
| `hdi_lower ≤ hdi_upper` | ✅ |
| `hdi90_lower ≤ MAP ≤ hdi90_upper` | ✅ |
| HDI95 contains HDI90 | ✅ |
| HDI90 contains HDI50 | ✅ |
| MAP non-negative | ✅ |
| HDI90 width | max 2,766, mean 179.6 — real posteriors |

And independently recomputed straight from `_sample_store` — sort the draws, take the narrowest
window covering 90% — the served numbers match exactly:

```
hdi90_lower match: True   max|d| = 0
hdi90_upper match: True   max|d| = 0
```

**Subset draws are exact too.** Served posterior draws versus the sample store: `(5, 128)` against
`(5, 128)`, `identical = True`.

**The bulk table's forecast half is sound** — 45 columns, zero all-null columns, `sb_map` non-zero
on 2,131 of 87,552 rows with a maximum of 2,275.

So the compute core does its job. The defects are all at the serving boundary.

## 6. A number that settles an earlier question

`build_bulk_table` on the real artifacts: **38.7 s, peak RSS 10.47 GB.**

That is above the `MemoryMax=9G` committed to `deployment/views-crafdapi.service` and deliberately
not installed. It confirms directly what was flagged when the ceiling was sized: **installing 9G
would kill the one endpoint CRAF'd uses.** The decision to hold was correct, and this is the
measurement that was missing. It matches ADR-030's recorded local figure of 10.5 GB.

## 7. Why none of this was caught

The `app_client` fixture (`tests/test_api_endpoints.py:21-72`) stores `"data": dataset.dataframe` —
**the index-only frame**. The harness therefore reproduces C-232 exactly, and the endpoint tests
pass anyway, because they assert `status_code == 200` and nothing else.

The one guard that would have caught real-data drift is dormant:

```
SKIPPED tests/forecast/test_served_output_golden_real.py:71:
  real forecast artifact absent (appwrite_cache/unfao_bucket/forecast_dataset_20260310_114703.parquet)
```

It points at an **`unfao_bucket`** path inherited from the ancestor repo. What exists here is
`crafd_bucket`. The test is designed to skip in CI by intent — but it skips locally too, where it is
supposed to be the guard. It has never run in this repository.

---

## What this report does not claim

- **Not that the repo is non-functional.** That hypothesis is refuted. The compute core is correct
  where it was checked, exactly.
- **Not that the subset routes are fully verified.** Forecast pg-level draws were matched to the
  store exactly; the aggregate levels were checked for presence and plausibility, not re-derived.
- **Not that month 559 is a bug.** It may be a legitimately unobserved month. What is established is
  that the `actual` columns reaching the consumer are all zero and carry no signal either way.
- **Not that any of this reproduces in production.** Everything here is local, warm, on the
  operator's key partition — the exact vantage point C-287 says proves nothing about the consumer.
- **Not that the unbounded routes are safe.** They were bounded on purpose; C-284 stands.

## Method, so any verdict can be re-derived

Probe scripts are in the session scratchpad: `probe.py` (all routes), `fc_check.py` (forecast
invariants + recomputation), `hdi_check.py` (historical intervals), `bulk_subset.py` (bulk +
subset), `tail.py` (the month-559 finding). Each loads real datasets via
`ForecastDataset.from_value(..., mmap=True)` from
`apis/un_crafd/cache/datasets/ba85fb086a35c37e_{forecast,historical}_value`, then drives the real
app built the way `app_client` builds it.

125 artifact files were md5-fingerprinted before the run and re-checked after.

## Cross-references

**C-232** (hollow `/latest` — now demonstrated at scale) · **C-284** (unbounded routes) ·
**C-287** (the ceiling, and warm-partition verification) · **C-244** (45-vs-36 columns — the count
turns out to be level-dependent: pg serves 45, country 36) · `reports/architecture/measured_against_the_siblings.md` ·
`reports/ops/same_defect_next_door.md`
