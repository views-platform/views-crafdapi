# Class Intent Contract: bulk-parquet writer (`forecast/serialize/bulk_parquet.py`)

**Status:** Active
**Owner:** Project maintainers
**Last reviewed:** 2026-07-24
**Related ADRs:** ADR-025 (FAO output schema — the 36-column admin-1 bulk artifact (33 base + bimodality_flag)), ADR-024 (raw counts), ADR-006 (this contract), views-postprocessing ADR-013 (the wire run this consumes)
**Source:** epic #222 / S6 (#228)

---

## 1. Purpose

Produce the **ADR-025 admin-1 bulk artifact**: one wide parquet carrying the full forecast table
in a single file, so FAO can load everything at once instead of paging the JSON API. It is the
**same served numbers** as the API, aggregated to GAUL admin-1 grain — one row per
`(month_id, admin1_code)`, `schema.bulk_columns()` (6 identity + per series `sb/ns/os` the 9
ADR-025 columns; 33 base + per-series bimodality_flag = **36**). `build_bulk_table(forecast_ds, historical_ds)` assembles the table;
`write_bulk_parquet(path, forecast_ds, historical_ds)` persists it. Served by
`GET /data/forecast/bulk`.

## 2. Non-Goals

- Does **not** compute the statistics — it composes `ForecastDataset.calculate_hdi_map`
  (aggregate, gaul1) for the forecast quantities and sums historical observed counts for `actual`.
- Does **not** own the column names — those come from `serialize/schema.py` (single source).
- Does **not** load or cache datasets, authenticate, or manage the HTTP surface (the endpoint in
  `managers/api.py` loads the forecast + historical datasets and returns the file).
- Does **not** apply target transforms (ADR-024 — all raw counts).

## 3. Responsibilities and Guarantees

- **Exact schema:** the returned table's columns are `schema.bulk_columns()` in order (33), always.
- **Consumer naming:** forecast value columns are `sb/ns/os`-renamed via
  `json_contract.to_consumer_columns`; identity columns are the consumer GAUL names via
  `schema.IDENTITY_SOURCE` (admin-0 country, **not** M49).
- **`s_actual`:** the historical observed count summed to admin-1 (counts are additive), joined on
  `(month_id, admin1_code)`; `NaN` where there is no observed value (the forecast horizon) or when
  `historical_ds` is absent.
- **Fail-loud (ADR-008):** two forecast targets resolving to the same series (a `_best`+`_prob`
  pair) raise rather than silently colliding; an admin-1 code mapping to more than one country
  (broken hierarchy) raises.

## 4. Inputs and Assumptions

- `forecast_ds`: a prediction `ForecastDataset` (per-cell posterior samples; targets resolve to
  `sb/ns/os` via `schema.series_of`).
- `historical_ds` (optional): a `ForecastDataset` of observed counts (one value per cell); absence
  yields `NaN` `actual`.
- Both carry the 9-column GAUL metadata (`geo_metadata`).

## 5. Outputs and Side Effects

- `build_bulk_table` → a `pandas.DataFrame` (36 columns). `write_bulk_parquet` → writes a parquet
  file, returns its `Path`. No caching, no network.

## 6. Failure Modes and Loudness

- Non-conforming target names / series collisions / broken admin hierarchy → `ValueError` (loud).
- The endpoint wraps these as HTTP 500; a missing historical artifact degrades to `NaN` `actual`
  (logged), not a failure.

## 7. Boundaries and Interactions

- **Composed by** `CrafdApiManager` (`GET /data/forecast/bulk`).
- **Depends on** `ForecastDataset.calculate_hdi_map`, `serialize.schema`, `serialize.json_contract`.

## 8. Test Alignment

- `tests/forecast/test_bulk_parquet.py` — 36-column schema (incl. bimodality_flag), one row per `(month_id, admin1_code)`,
  consumer identity, `actual` == admin-1 sum, `NaN` without history, nested HDIs, parquet
  round-trip, both fail-loud guards, and the `GET /data/forecast/bulk` endpoint.
