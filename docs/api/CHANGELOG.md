# CRAF'd forecast API: changelog

Changes that affect partners: new or removed columns, outcomes or levels, and changes to the
ensemble or the models it combines. Newest first.

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
