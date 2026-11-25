# CRAF'd forecast API: changelog

Changes that affect partners: new or removed columns, outcomes or levels, and changes to the
ensemble or the models it combines. Newest first.

## 2026-11-25 (release 0.8.0)

- **Only keys for CRAF'd's own project work.** The service now checks every key against CRAF'd's
  own Appwrite project; keys issued earlier stop working, and VIEWS issues new ones.
- **`/health` names the forecast run it serves** (`served_run`), and reports `degraded` also when
  the forecast's age cannot be measured or the store cannot be reached.

## 2026-11-22

- **Countries are added up by their GAUL country code,** the code partners use to name them,
  not by ISO3. Where one ISO3 code covers more than one GAUL country (`AUS`, `PYF`, `xFR`), each
  GAUL country is now its own row. A country without an ISO3 code is served with an empty
  `country_iso3`.

## 2026-11-21

- **No request waits for the forecast to load.** The service loads each new forecast by itself,
  when it starts and within a few minutes of a delivery, and builds the bulk files before it
  serves them. Until the first forecast is loaded after a restart, data requests answer `503`
  ("No forecast is available right now. Try again later.").
- **Every answer says which month the forecast was made in:** `forecast_made_in` in a slice
  (for example `"2026-07"`), and the header `X-Forecast-Made-In` with a bulk file.
- **A deleted key stops working within a minute.**
- **Nine bulk files replace the one admin-1 bulk file.** `GET /bulk/{level}/{outcome}` returns
  one Parquet file per level (`grid`, `admin1`, `country`) and outcome (`sb`, `ns`, `os`). Each
  file holds the same table as a slice with no options: every place, the recorded months and the
  forecast months. `/data/forecast/bulk` now answers `404`. A slice too large to send (`413`) now
  names the bulk file to download instead. Without the recorded deaths a bulk file is refused
  (`503`), as a slice is; it used to be sent with an empty recorded-deaths column.
- **New summary columns.** Each outcome `s` now has `s_p_gt0`, `s_p_gt25`, `s_p_gt100`,
  `s_p_gt500`, `s_p_gt1000` (the chance of more than that many deaths), `s_mean` (the expected
  number), `s_mean_if_any` and `s_median_if_any` (the deaths if there are any),
  `s_severe_scenario` (the average of the worst 5% of draws, empty below 20 draws) and
  `s_actual`; every row also has `n_draws`. Removed: `s_map`, the six `s_hdi50/90/95` bounds and
  `s_bimodality_flag`, which were 0 or empty in almost every row. Every summary is computed from
  the usable draws only, and a row without usable draws is empty, never 0. This applies to the
  slices and to the admin-1 bulk file (`/data/forecast/bulk`, now 39 columns).

## 2026-11-20

- **One slice route replaces the subset and HDI-MAP routes.** A slice is now
  `GET /slice/{level}/{outcome}?places=...&months=...&columns=...`, as the partner guide
  describes: levels `grid`, `admin1` and `country`; months written `2027-01`; countries named by
  their GAUL code. It returns the summary columns and the recorded deaths, never samples. The
  routes `/{pg|country|gaul1}/data/{forecast|historical}/subset` and
  `/{pg|country|gaul1}/analysis/{forecast|historical}/hdi-map` now answer `404`, and their
  options (`time_ids`, `entity_ids`, `features`, `sample_idx`, `with_metadata`, `aggregate`,
  `force_refresh`, `alpha`, `enforce_non_negative`) no longer exist.
- Every slice row carries `month` (`2027-01`) and `month_id`; grid rows also carry `latitude`
  and `longitude`. A place or month named twice comes back once; an option given twice
  (`?places=1&places=2`) answers `422`. Negative GAUL codes (the Azores, -3727 to -3730) can be
  asked for.
- Recorded deaths are served for months before the forecast only; the first forecast month,
  barely reported when the forecast is made, shows none. A slice reaching past months answers
  `503` if the recorded deaths cannot be loaded.
- A slice too large to compute or to send answers `413` before any work, saying to ask for fewer
  places or months. The summary columns are still the earlier set; the set decided on
  2026-11-17 follows (#204).
- **Levels removed: admin 0 (`gaul0`) and admin 2 (`gaul2`).** The service now has three levels:
  grid cell (`pg`), country (`country`) and admin 1 (`gaul1`). Admin 0 was the same geography as
  country. Requests to `/gaul0/...` and `/gaul2/...` now answer `404`.
