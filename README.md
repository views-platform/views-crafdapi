# CRAF'd Forecast API (views-crafdapi)

FastAPI service that serves partner organisations one forecast, the latest good one, from data
stored in Appwrite (docs/SERVICE_SPECIFICATION.md). It provides:
- A bulk download of the forecast
- Slices of one outcome at one level (grid cell, admin 1, country), by places, months and columns
- Summary columns (chances of more than 0, 25, 100, 500 and 1,000 deaths; the mean; the mean and median if any; a severe case; the number of draws) and the recorded deaths; never samples
- Admin-1 and country values from the cells' samples summed first, then summarised

This repository is part of the views-platform and focuses on exposing CRAF'd predictions via HTTP.

## Platform seam contract (pinned)

This API is a consumer on the shared Appwrite seam and conforms to **The Appwrite Seam Contract**, referenced **by URL at a published tag — never `main`** (`joining_the_seam.md §1`):

<https://github.com/views-platform/views-appwrite/blob/appwrite-seam-v1.5.2/docs/ADRs/platform/appwrite_seam_contract.md>

The pin is `appwrite-seam-v1.5.2`, the same tag `seam_contract.REGISTRY_PIN_TAG` binds in CI (below). Moving to a newer version is a deliberate act: read the diff, accept it, repoint. Coordinates are read from the pinned registry, never copied; this API writes its **own** thin Appwrite client (WET-before-DRY — it must not import `views_pipeline_core.modules.{appwrite,datastore}`).

**Served-document-name binding (ADR-017 D2).** The store-document `name` this API filters every query on is declared once in `src/views_crafdapi/seam_contract.py` (`CONSUMER_DOCUMENT_NAME = "un_crafd"`) and bound in CI against the registry's `[contract.UNCRAFD_CONSUMER_DOCUMENT_NAME]` declaration, read at the **immutable tag `appwrite-seam-v1.5.2`** (the pin lives in one place — `REGISTRY_PIN_TAG`; the UNCRAFD row exists only from v1.5.2). This closes the invisible-delivery failure (a producer/consumer name drift returns empty with no error — ADR-017 §1). Changing the name is a **contract amendment**: bump the registry edition and re-pin *both* sides, never one alone. Consumer half of views-postprocessing ADR-017 §5; mirrors views-faoapi#379.

## Features

- Per-API-key Appwrite client and data-cache
- Robust file parsing (parquet, CSV, JSON, pickle, feather)
- Posterior analysis: MAP + empirical HDIs via `PosteriorDistributionAnalyzer`
- Priogrid-to-country/GAUL mapping and metadata joins
- Subset and aggregate results at three levels: grid cell (`pg`), country and admin 1 (`gaul1`)

## Requirements

- Python 3.11
- Appwrite backend with:
  - Project ID
  - Buckets and database/collection for metadata
  - API keys per user
- MacOS or Linux recommended

## Installation

```bash
# From repository root (views-crafdapi)
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

## Configuration

The API reads its configuration from the environment, loaded in `CrafdApiManager.__init__`. **Where that comes from differs between running locally and running deployed** — the original text here named a third location (`views-models`) that is neither, which is #28:

| | Coordinates (`APPWRITE_*`) | The one secret (`APPWRITE_DATASTORE_API_KEY`) |
|---|---|---|
| **Local dev / notebooks** | `.env` at the root of **this** repo, resolved via `pyprojroot.here()` (`managers/model.py`); copy `.env.example` | same `.env` |
| **Deployed** (`deployment/RELEASE_RUNBOOK.md`) | the **views-appwrite registry** (`APPWRITE_REGISTRY`), written into `.env.crafdapi` | **exported into the environment by the operator** — *"never from anyone's `.env`"* |

Creating, sending, renewing and revoking a partner's key: `deployment/PARTNER_KEYS.md`.

`views-models/.env` holds the key the **postprocessor** delivers with. It is a different consumer of the same secret, not this API's config source. Set the following for local work:

```bash
# .env
APPWRITE_ENDPOINT=https://fra.cloud.appwrite.io/v1
APPWRITE_CRAFD_PROJECT_ID=your-project-id
APPWRITE_CRAFD_BUCKET_ID=predictions-bucket-id
APPWRITE_CRAFD_BUCKET_NAME=predictions-bucket-name
APPWRITE_CRAFD_COLLECTION_ID=file-metadata-collection-id
APPWRITE_CRAFD_COLLECTION_NAME=file-metadata-collection-name
APPWRITE_METADATA_DATABASE_ID=metadata-db-id
APPWRITE_METADATA_DATABASE_NAME=metadata-db-name
```

Notes:
- `historical_targets` is read by `ForecastDataset(...)` when category is historical.
- Keep shapefiles in `src/views_crafdapi/shapefiles` (already included).
- The API requires `X-API-Key` header in each request.

## Running the API

From the project root:

```bash
source .venv/bin/activate
uvicorn views_crafdapi.managers.api:create_app --factory --host 0.0.0.0 --port 8000
```

See ADR-012 for the factory pattern rationale. The API requires `APPWRITE_*` environment variables (see `.env.example` or ADR-013).

You should see uvicorn logs. Visit:
- http://localhost:8000/
- http://localhost:8000/docs

To stop:
- Ctrl+C or send SIGINT/SIGTERM.

## Authentication

Every endpoint requires `X-API-Key: <your appwrite key>`. The server validates keys per request and caches the Appwrite client by a hash of the key.

## Data Model

- Prediction files are fetched from Appwrite and parsed into pandas DataFrames.
- `ForecastDataset` expects geospatial metadata columns:
  - pg_xcoord, pg_ycoord, country_iso_a3,
  - admin1_gaul1_code, admin1_gaul1_name,
  - admin1_gaul0_code, admin1_gaul0_name,
  - admin2_gaul2_code, admin2_gaul2_name
- MultiIndex: (month_id, priogrid_id)

## API Overview

> **Full API reference:** see [`docs/api/README.md`](docs/api/README.md) for the consolidated endpoint catalogue, query parameters, response envelope, and examples — and the governing ADRs ([026 surface](docs/ADRs/active/026_api_surface_and_resource_model.md), [027 auth](docs/ADRs/active/027_authentication_and_per_key_isolation.md), [028 data source](docs/ADRs/active/028_terminal_consumer_boundary.md)). Interactive docs are served at `/docs` (Swagger) and `/redoc`.

Health
- GET `/health` — checks the store connection and the age of the served forecast

Bulk download
- GET `/data/forecast/bulk` — the whole forecast at admin 1, one Parquet file (to become one file per outcome and level, #203)

Slice
- GET `/slice/{level}/{outcome}?places=...&months=...&columns=...`
- `level`: `grid`, `admin1` or `country`. `outcome`: `sb`, `ns` or `os` today.
- `places`: place ids at that level, comma-separated (PRIO-GRID cell ids, GAUL admin-1 codes, GAUL country codes). `months`: year-month, such as `2027-01`. `columns`: summary column names. Each one left out means all.
- Each row is one place in one month: the place's id and names (grid rows also `latitude`, `longitude`), `month`, `month_id`, then the outcome's summary columns. Forecast months carry the summaries; months before the forecast carry the recorded deaths in `{outcome}_actual` (the first forecast month never does: it is barely reported yet, register C-293). Never samples.
- No other options exist (docs/SERVICE_SPECIFICATION.md). An option given twice (`?places=1&places=2`) is refused; write it once, comma-separated.

> **History.** A slice reaching months before the forecast needs the recorded deaths. If they
> cannot be loaded, it answers `503` rather than an empty `{outcome}_actual` column. A slice of
> forecast months only does not read the history. A refused key stays `401`.

> **Size limit.** Before any work the slice estimates two sizes, its answer (rows x columns) and
> its work (the cell-months it reads, with every forecast draw), and returns `413` if either is
> above 512 MiB, saying to ask for fewer places or months. Override with
> `CRAFDAPI_MAX_RESPONSE_BYTES`.

Response
```json
{
  "level": "country",
  "outcome": "sb",
  "columns": ["country_code", "country_name", "country_iso3", "month", "month_id", "sb_p_gt25"],
  "rows": [{"country_code": 161, "country_name": "Sudan", "country_iso3": "SDN", "month": "2027-01", "month_id": 565, "sb_p_gt25": 0.84}]
}
```

Example
```bash
curl -H "X-API-Key: $CRAFD_KEY" \
  "http://localhost:8080/slice/country/sb?places=161,160&months=2027-01,2027-02&columns=sb_p_gt25"
```

## Caching

- Managers per API key are cached (`_manager_cache`)
- Latest dataframe per API key and category cached for 4 hours (`_dataframe_cache`, `TTLCache(ttl=4*3600)`)
- Global file bytes cache keyed by Appwrite file_id (`_file_cache`)
- Caches are cleared by restarting the service. There is no clearing endpoint: the file cache is shared by every key, so no single caller may empty it (C-269)

## Geospatial Mapping

- Shapefiles under `src/views_crafdapi/shapefiles`:
  - Natural Earth countries
  - PRIO-GRID
  - GAUL Level 1/2
- `ForecastDataset` joins/aggregates results using metadata columns and supports:
  - level: `country|gaul1`
  - aggregation: sum for pred_* targets, first for constant metadata

## Development

- Code lives under `src/views_crafdapi`.
- Key modules:
  - `managers/api.py` — FastAPI routes and lifecycle
  - `managers/appwrite.py` — Appwrite file access
  - `managers/prediction.py` — Prediction store logic
  - `data/handlers.py` — Dataset handling and posterior stats
  - `data/statistics.py` — PosteriorDistributionAnalyzer
  - `mapping/mapping.py` — Priogrid and admin mapping

Logging config in `configs/logging.yaml`.

## Deployment Notes

**Branch mismatch in views-models (C-17):** The `views-models/apis/un_crafd/run.sh` script (line 27) installs views-crafdapi from `@main`, but `requirements.txt` in the same directory pins `@development`. This means `run.sh` and `pip install -r requirements.txt` install different versions. The fix lives in the views-models repo — align both to the same branch (recommended: `@main` for production, `@development` for staging).

## Testing

Run all non-integration tests (default):
```bash
pytest -v
```

Run by layer:

| Layer | What it proves | Command |
|-------|---------------|---------|
| 1 — Storage I/O | Appwrite round-trips work | `pytest tests/ -m layer1_storage -v` (needs creds) |
| 2 — Data Processing | Aggregation, statistics, format detection correct | `pytest tests/ -m layer2_data -v` |
| 3 — HTTP Contract | Endpoints return correct status/shape/params | `pytest tests/ -m "layer3_http and not integration" -v` |
| 4 — Infrastructure | SDK compat, caches bounded | `pytest tests/ -m layer4_infra -v` |
| 5 — Audit | Prior bugs stay fixed, known gaps tracked | `pytest tests/ -m layer5_audit -v` |

Integration tests require `APPWRITE_*` environment variables and are excluded by default.

See `TESTING.md` for the full test architecture rationale.

## Troubleshooting

- 401 Invalid API key: verify `X-API-Key` and Appwrite project permissions.
- 404 No prediction files: check bucket ID and `category` property on stored files.
- 500 Failed to parse file: confirm file format (parquet recommended) and schema.
- Geospatial errors: ensure metadata columns exist and shapefiles are present.
- Index errors: data must use MultiIndex `(month_id, priogrid_id)`.

