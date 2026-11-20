# CRAF'd forecast API: changelog

Changes that affect partners: new or removed columns, outcomes or levels, and changes to the
ensemble or the models it combines. Newest first.

## 2026-11-20

- **Levels removed: admin 0 (`gaul0`) and admin 2 (`gaul2`).** The service now has three levels:
  grid cell (`pg`), country (`country`) and admin 1 (`gaul1`). Admin 0 was the same geography as
  country. Requests to `/gaul0/...` and `/gaul2/...` now answer `404`.
