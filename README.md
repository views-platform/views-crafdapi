# views-crafdapi: the CRAF'd forecast API

A small web service that gives partner organisations one conflict forecast: the latest good one.
The work is funded by the Complex Risk Analytics Fund (CRAF'd). What the service promises is in
[`docs/SERVICE_SPECIFICATION.md`](docs/SERVICE_SPECIFICATION.md).

## What it serves

Five routes. Every data route needs a key in the `X-API-Key` header.

| Route | What it gives |
|---|---|
| `GET /ping` | Whether the service is up. No key needed. |
| `GET /version` | The version running. No key needed. |
| `GET /health` | Whether a forecast is loaded, which run, and how old it is. |
| `GET /bulk/{level}/{outcome}` | One Parquet file: every place and every month. The main way in. |
| `GET /slice/{level}/{outcome}` | A smaller piece as JSON, by places, months and columns. |

Levels are `grid`, `admin1` and `country`. Outcomes are `sb`, `ns` and `os`. The service serves
summaries of the model's draws (chances, expected deaths, a severe case) and the recorded deaths,
never the draws themselves.

## Where to read

- **Using the API:** [`docs/api/README.md`](docs/api/README.md) (the partner guide),
  [`docs/api/data_dictionary.md`](docs/api/data_dictionary.md) (every column) and
  [`docs/api/CHANGELOG.md`](docs/api/CHANGELOG.md).
- **Worked examples:** [`notebooks/`](notebooks/README.md).
- **Running it:** [`deployment/RELEASE_RUNBOOK.md`](deployment/RELEASE_RUNBOOK.md) (release, restart,
  setting up a server), [`deployment/PARTNER_KEYS.md`](deployment/PARTNER_KEYS.md),
  [`deployment/MONITORING.md`](deployment/MONITORING.md), and what is left before launch in
  [`deployment/CRAFD_LAUNCH_READY_LIST.md`](deployment/CRAFD_LAUNCH_READY_LIST.md).
- **Why it is built this way:** the decision records in [`docs/ADRs/`](docs/ADRs/README.md).

## How it gets its data

Nothing monthly happens in this repository. views-postprocessing delivers each forecast into the
service's own storage (Appwrite). The service checks every five minutes, loads a new delivery by
itself with its own read key, and builds the bulk files. It only reads.

The names of the storage it reads come from the views-appwrite registry, at a fixed edition:
<https://github.com/views-platform/views-appwrite/blob/appwrite-seam-v1.9.0/docs/ADRs/platform/appwrite_seam_contract.md>.
The edition is set in one place, `REGISTRY_PIN_TAG` in `src/views_crafdapi/seam_contract.py`, and a
test keeps this link in step with it.

## Working on it

```bash
uv sync                 # install, including the development tools
uv run pytest           # the tests (those needing a live key are left out by default)
uv run ruff check .     # lint
```

To run the service locally it needs the settings named in `.env.example` and a read key in
`APPWRITE_CRAFDAPI_SERVER_API_KEY`. Tests and their layers are described in [`TESTING.md`](TESTING.md).
