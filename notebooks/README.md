# `views-crafdapi` notebooks

Worked examples for the CRAF'd Forecast API — how to **call the service**, **visualize** what it
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
uv run --with jupyterlab jupyter lab notebooks/
```

`jupyterlab` is deliberately **not** a dev dependency — `--with` pulls it in for this command
only, so the default dev install stays lean. `uv sync` installs `ipykernel`, so the kernel also
works from VS Code or any Jupyter you already have.

For the live-API notebooks (`01`, `02`), copy the credential template and add your key:

```bash
cp .env.example .env        # then set APPWRITE_DATASTORE_API_KEY (request one from the VIEWS team)
```

`02` additionally needs `geopandas` for the border overlay (installed by `uv sync`).
`03_offline_demo.ipynb` needs no `.env` and no network.

### Outputs are committed on purpose

`01` and `02` are stored **with their executed outputs**, matching views-faoapi. Someone
browsing this repo on GitHub sees the real numbers and the six maps without needing a key —
which is the point, since these notebooks are the worked example an external analyst is pointed
at. `03_offline_demo.ipynb` stays stripped (also matching faoapi): it is deterministic and runs
in seconds, so stored output adds nothing.

The cost is honest: `02` is ~1.1 MB of embedded PNGs, and re-executing it produces a large diff
even when nothing meaningful changed. Two consequences:

* **Do not strip the outputs** to make a diff smaller. That silently removes the only thing a
  browsing reader can see.
* **Re-execute both when the served run changes** — the outputs name a specific run
  (`rusty_bucket_forecasting_20260727_095355`) and a specific window, so after a new delivery
  they describe something that is no longer live:

  ```bash
  .venv/bin/python -m pytest --nbmake notebooks/01_quickstart.ipynb notebooks/02_visualization.ipynb
  # then, to refresh the stored outputs:
  .venv/bin/jupyter nbconvert --to notebook --execute --inplace \
      notebooks/01_quickstart.ipynb notebooks/02_visualization.ipynb
  ```

Outputs are checked for secrets before commit: the auth cell prints `API key : set`, never the
value, and no long token appears in any stored output.

### The forecast window moves

A forecast run covers a **fixed 36-month window**, and that window advances every time a new
run is delivered. `01` and `02` hard-code `FORECAST_MONTH = date_to_month_id(2026, 7)`, which
is the first month of the run live at the time of writing (`rusty_bucket`, months 559–594 =
2026-07 … 2029-06).

**If a later run has superseded it, that month is outside the window and every forecast cell
returns an empty frame.** Both notebooks now open with a preflight cell that catches this and
tells you what to change; without it the failure surfaces as a bare `KeyError` several cells
later. To see the window the live run actually covers:

```python
client.provenance("forecast")     # run_id, created_at, methodology_version
```

## Access, updates & citation

- **Getting a key:** request an `APPWRITE_DATASTORE_API_KEY` from the VIEWS team
  (<https://viewsforecasting.org> / your CRAF'd–VIEWS point of contact). Base URL:
  `https://crafdapi.viewsforecasting.org`.
- **Which run am I looking at?** Call `GET /provenance/{forecast|historical}` (or, in a notebook,
  `client.provenance("forecast")`) — it returns the run id, creation time, and methodology version.
  A new forecast run is published periodically and supersedes the previous one, so **record the run
  id/vintage alongside any number or figure you cite** (forecasts are not reproducible across runs).
- **Methodology, skill & responsible use:** VIEWS (<https://viewsforecasting.org>).

## See also

- **[Data dictionary](../docs/api/data_dictionary.md)** — every served column, defined.
- API reference: [`docs/api/README.md`](../docs/api/README.md)
- Authentication & data source: ADR-027, ADR-028
