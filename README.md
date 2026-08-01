# FAO Forecast API (views-faoapi)

FastAPI service for retrieving and analyzing FAO prediction data stored in Appwrite. It provides:
- Retrieval of latest historical and forecast prediction datasets
- Flexible subsetting by time, entity, features, and samples
- Computation of Highest Density Intervals (HDI) and MAP estimates
- Aggregation to country and GAUL administrative levels
- Appwrite file browsing and cache management
- Geospatial metadata enrichment based on PRIO-GRID

This repository is part of the views-platform and focuses on exposing FAO predictions via HTTP.

## Platform seam contract (pinned)

This API is a consumer on the shared Appwrite seam and conforms to **The Appwrite Seam Contract**, referenced **by URL at a published tag — never `main`** (`joining_the_seam.md §1`):

<https://github.com/views-platform/views-appwrite/blob/platform-001-v1.2.0/docs/ADRs/platform/appwrite_seam_contract.md>

`platform-001-v1.2.0` is the newest *published* tag; `main` carries the v1.3.0 rename (untagged until the operator cuts it — views-appwrite#21). Moving to a newer version is a deliberate act: read the diff, accept it, repoint. Coordinates are read from the pinned registry, never copied; this API writes its **own** thin Appwrite client (WET-before-DRY — it must not import `views_pipeline_core.modules.{appwrite,datastore}`).

## Features

- Per-API-key Appwrite client and data-cache
- Robust file parsing (parquet, CSV, JSON, pickle, feather)
- Posterior analysis: MAP + empirical HDIs via `PosteriorDistributionAnalyzer`
- Priogrid-to-country/GAUL mapping and metadata joins
- Subset and aggregate results at levels: pg, country, gaul1, gaul2

## Requirements

- Python 3.11
- Appwrite backend with:
  - Project ID
  - Buckets and database/collection for metadata
  - API keys per user
- MacOS or Linux recommended

## Installation

```bash
# From repository root (views-faoapi)
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

## Configuration

The API reads environment variables via `.env` at runtime (loaded in `CrafdApiManager.__init__` and located in `views-models`). Set the following:

```bash
# .env
APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1
APPWRITE_DATASTORE_PROJECT_ID=your-project-id
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

Root
- GET `/` — lists endpoints and usage note

Health
- GET `/health` — checks Appwrite connectivity and cache stats

Latest data
- GET `/data/historical/latest`
- GET `/data/forecast/latest`

Subset (by level)
- GET `/{pg|country|gaul1|gaul2}/data/historical/subset`
- GET `/{pg|country|gaul1|gaul2}/data/forecast/subset`

HDI-MAP (by level)
- GET `/{pg|country|gaul1|gaul2}/analysis/historical/hdi-map`
- GET `/{pg|country|gaul1|gaul2}/analysis/forecast/hdi-map`

Files
- GET `/files/{bucket_id}`
- GET `/files/{bucket_id}/{file_id}/info`
- GET `/files/{bucket_id}/{file_id}/download?download=true|false&use_cache=true|false`
- GET `/files/{bucket_id}/{file_id}/cached`

Cache
- GET `/cache/stats`
- DELETE `/cache?bucket_id=&older_than_hours=`

## Query Parameters and Headers

Common
- Header: `X-API-Key` (required)
- `force_refresh` (bool) — bypass per-key cache and re-fetch latest

Subset/HDI-MAP parameters
- `time_ids` — comma-separated integers (e.g., `252,253`)
- `features` — comma-separated names (e.g., `pred_lr_ged_sb`)
- `sample_idx` — comma-separated integers (e.g., `0,1,2`)
- `entity_ids` — country level: ISO3 strings (e.g., `USA,FRA`); others: integers
- `with_metadata` — bool (default true)
- `aggregate` — bool (default false)
- `level` — only for programmatic calls; REST path already encodes level
- `alpha` — float in (0,1) for HDI (default 0.9)
- `enforce_non_negative` — bool for MAP floor at 0

Files
- `limit`, `offset`, `search` for listing
- `use_cache`, `download` for file download
- `bucket_id`, `file_id` path params

## Response Shapes

Latest endpoints
```json
{
  "success": true,
  "data": {
    "dataframe": [ { "...": "..." } ],
    "shape": [rows, cols],
    "columns": ["col1", "col2", "..."],
    "file_id": "appwrite-file-id",
    "timestamp": 1732630000.123,
    "category": "historical"
  }
}
```

Subset endpoints
```json
{
  "success": true,
  "data": {
    "dataframe": [ { "...": "..." } ],
    "shape": [rows, cols],
    "columns": ["..."],
    "category": "forecast",
    "level": "country",
    "parameters": {
      "time_ids": [252,253],
      "features": ["pred_lr_ged_sb"],
      "sample_idx": [0,1],
      "entity_ids": ["USA","FRA"],
      "with_metadata": true,
      "aggregate": true
    }
  }
}
```

HDI-MAP endpoints
- Return combined HDI lower/upper and MAP columns per requested variable, optionally joined with metadata.

## Examples

Curl (macOS)
```bash
# Historical latest (force refresh)
curl -H "X-API-Key: $APPWRITE_API_KEY" "http://localhost:8080/data/historical/latest?force_refresh=true"

# Forecast subset at PG level for specific months and features
curl -H "X-API-Key: $APPWRITE_API_KEY" "http://localhost:8080/pg/data/forecast/subset?time_ids=252,253&features=pred_lr_ged_sb&sample_idx=0,1&with_metadata=true"

# Country-level aggregation for HDI/MAP
curl -H "X-API-Key: $APPWRITE_API_KEY" "http://localhost:8080/country/analysis/forecast/hdi-map?alpha=0.9&entity_ids=USA,FRA&aggregate=true"

# List files in a bucket
curl -H "X-API-Key: $APPWRITE_API_KEY" "http://localhost:8080/files/$APPWRITE_CRAFD_BUCKET_ID?limit=50&search=forecast"

# Download a file inline
curl -H "X-API-Key: $APPWRITE_API_KEY" "http://localhost:8080/files/$APPWRITE_CRAFD_BUCKET_ID/<file_id>/download?download=false"
```

Python (requests)
```python
import requests

API = "http://localhost:8080"
headers = {"X-API-Key": "your-key"}

r = requests.get(f"{API}/country/data/historical/subset", headers=headers, params={
  "time_ids": "252,253",
  "features": "pred_lr_ged_sb",
  "entity_ids": "USA,FRA",
  "aggregate": "true",
  "with_metadata": "true"
})
print(r.json())
```

## Caching

- Managers per API key are cached (`_manager_cache`)
- Latest dataframe per API key and category cached for 4 hours (`_dataframe_cache`, `TTLCache(ttl=4*3600)`)
- Global file bytes cache keyed by Appwrite file_id (`_file_cache`)
- Clear caches via DELETE `/cache`

## Geospatial Mapping

- Shapefiles under `src/views_crafdapi/shapefiles`:
  - Natural Earth countries
  - PRIO-GRID
  - GAUL Level 1/2
- `ForecastDataset` joins/aggregates results using metadata columns and supports:
  - level: `country|gaul1|gaul2`
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

**Branch mismatch in views-models (C-17):** The `views-models/apis/un_crafd/run.sh` script (line 27) installs views-faoapi from `@main`, but `requirements.txt` in the same directory pins `@development`. This means `run.sh` and `pip install -r requirements.txt` install different versions. The fix lives in the views-models repo — align both to the same branch (recommended: `@main` for production, `@development` for staging).

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

