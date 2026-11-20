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
- **Levels removed: admin 0 (`gaul0`) and admin 2 (`gaul2`).** The service now has three levels:
  grid cell (`pg`), country (`country`) and admin 1 (`gaul1`). Admin 0 was the same geography as
  country. Requests to `/gaul0/...` and `/gaul2/...` now answer `404`.
