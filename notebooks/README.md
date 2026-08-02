# `views-faoapi` notebooks

Worked examples for the FAO Forecast API — how to **call the service**, **visualize** what it
returns, and **reproduce its analytics offline** on synthetic data.

The API serves a **global** forecast (every land PRIO-GRID cell worldwide) and global
historical — not the earlier Africa/Middle-East scope. `01` (§2.5) and `02` (§0) confirm that
coverage live for historical **and** forecast.

## What the forecasts are (and how to read them)

- These are **conflict-fatality** forecasts — an *input/driver* to food-security analysis, **not**
  a food-security, IPC, or hunger output.
- **All values are raw fatality counts** (not rates or log-counts). The `lr_`/`ln_`/`pred_` prefixes
  on data-endpoint column names are legacy VIEWS naming and carry **no** meaning — `lr_ged_sb` is a
  raw count, not a log-rate (ADR-024).
- Three violence series, reported separately: `sb` state-based, `ns` non-state, `os` one-sided.
- Every served column is defined in the **[data dictionary](../docs/api/data_dictionary.md)** — start
  there if a column name is unclear (it also maps the three naming conventions across the endpoints).
- Method & skill: these forecasts come from **VIEWS** (<https://viewsforecasting.org>); consult VIEWS
  for methodology, validation, and appropriate-use guidance. Treat forecasts as probabilistic
  estimates with genuine uncertainty — always read the credible intervals, not just the MAP.

## The three notebooks

| Notebook | Shows | Needs an API key? |
|---|---|---|
| [`01_quickstart.ipynb`](01_quickstart.ipynb) | The HTTP API end to end — authentication, a global-coverage check, historical + forecast subsetting (`pg`/`country`/`gaul0-2`), posterior samples, and HDI/MAP uncertainty (cell-level + aggregated). Data via `CrafdApiClient`; `sample_idx` and `/analysis` shown as raw HTTP with an endpoint reference. | **Yes** (live API) |
| [`02_visualization.ipynb`](02_visualization.ipynb) | PRIO-GRID pixel maps via `views_crafdapi.plotting` — **global coverage maps (historical & forecast)**, single-month, shared-scale multi-month panels, all three features, regional GAUL-1 zooms, cross-country comparison. | **Yes** (live API) |
| [`03_offline_demo.ipynb`](03_offline_demo.ipynb) | The analytics the service performs — subsetting, HDI/MAP, geographic aggregation, a map — run **in-process on synthetic data** (`_synthetic.py`). | **No** |

## Conventions

- **Public surface only** — the API client (`CrafdApiClient`), the published helpers
  (`views_crafdapi.time`, `views_crafdapi.plotting`), and the dataset class for the offline demo.
- **No secrets, no real data committed** — cell outputs are stripped; the live-API notebooks
  read credentials from `.env`; `03` uses synthetic data with **fictional geography** (a toy
  lattice, fixed seed) so anyone can *Run All* with zero credentials.
- **`01`/`02` are read-to-learn without a key, run-with-a-key.** `03` always runs.

## Prerequisites & running

```bash
# from the repo root
uv sync
uv run jupyter lab notebooks/
```

For the live-API notebooks (`01`, `02`), copy the credential template and add your key:

```bash
cp .env.example .env        # then set APPWRITE_DATASTORE_API_KEY (request one from the VIEWS team)
```

`02` additionally needs `geopandas` for the border overlay (installed by `uv sync`).
`03_offline_demo.ipynb` needs no `.env` and no network.

## Access, updates & citation

- **Getting a key:** request an `APPWRITE_DATASTORE_API_KEY` from the VIEWS team
  (<https://viewsforecasting.org> / your FAO–VIEWS point of contact). Base URL:
  `https://faoapi.viewsforecasting.org`.
- **Which run am I looking at?** Call `GET /provenance/{forecast|historical}` (or, in a notebook,
  `client.provenance("forecast")`) — it returns the run id, creation time, and methodology version.
  A new forecast run is published periodically and supersedes the previous one, so **record the run
  id/vintage alongside any number or figure you cite** (forecasts are not reproducible across runs).
- **Methodology, skill & responsible use:** VIEWS (<https://viewsforecasting.org>).

## See also

- **[Data dictionary](../docs/api/data_dictionary.md)** — every served column, defined.
- API reference: [`docs/api/README.md`](../docs/api/README.md)
- Authentication & data source: ADR-027, ADR-028
