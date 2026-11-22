# `views-crafdapi` notebooks

Worked examples for the CRAF'd Forecast API — how to **call the service**, **visualize** what it
returns, and **reproduce its analytics offline** on synthetic data.

The API serves a **global** forecast (every land PRIO-GRID cell worldwide) and global
recorded deaths. `02` (section 2) maps the whole world for one month.

## What the forecasts are (and how to read them)

- These are **conflict-fatality** forecasts — an *input/driver* to food-security analysis, **not**
  a food-security, IPC, or hunger output.
- **Every number of deaths is a count** (ADR-024). The `{outcome}_p_gt{c}` columns are chances
  between 0 and 1, and `n_draws` counts draws.
- Three violence series, reported separately: `sb` state-based, `ns` non-state, `os` one-sided.
- Every served column is defined in the **[data dictionary](../docs/api/data_dictionary.md)**: start
  there if a column name is unclear.
- Method & skill: these forecasts come from **VIEWS** (<https://viewsforecasting.org>); consult VIEWS
  for methodology, validation, and appropriate-use guidance. Treat forecasts as probabilistic
  estimates with genuine uncertainty: read the chances and the severe case, not only the expected
  number.

## The three notebooks

> **`01` and `02` were rewritten for the bulk files and slices (#223)** and are stored without
> outputs until they are run against the live service after the next release. They were run end to
> end against a local copy of the new service on 2026-11-22.

| Notebook | Shows | Needs an API key? |
|---|---|---|
| [`01_quickstart.ipynb`](01_quickstart.ipynb) | The API end to end: the bulk download (`/bulk/{level}/{outcome}`), recorded versus forecast months, the summary columns, slices with `CrafdApiClient.slice` (places by GAUL code, months written `2027-01`), and the three levels (grid, admin 1, country). | **Yes** (live API) |
| [`02_visualization.ipynb`](02_visualization.ipynb) | PRIO-GRID pixel maps via `views_crafdapi.plotting`: the whole world for one month, one country (expected deaths and the chance of more than 25), several months on one scale, the three outcomes side by side, recorded deaths next to the forecast. | **Yes** (live API) |
| [`03_offline_demo.ipynb`](03_offline_demo.ipynb) | The analytics the service performs (subsetting, the summaries of the draws, aggregation across levels, a map) run **in-process on synthetic data** (`_synthetic.py`). | **No** |

## Conventions

- **Public surface only**: the API client (`CrafdApiClient`), the plotting helpers
  (`views_crafdapi.plotting`), the dataset class for the offline demo, and plain HTTP (`requests`)
  for the bulk download, which is a file, not JSON.
- **No secrets committed**: the live-API notebooks read the key from `.env` and print only whether
  it is set; stored outputs are checked for keys before a commit; `03` uses synthetic data with **fictional geography** (a toy
  lattice, fixed seed) so anyone can *Run All* with zero credentials.
- **`01`/`02` need a key to run;** once their outputs are stored (after the next release) they can
  be read without one. `03` always runs.

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

### Outputs are committed on purpose (from the next release)

`01` and `02` are stored **with their executed outputs** once they have been run against the live
service (at the moment they are stored without: see above), matching views-faoapi. Someone
browsing this repo on GitHub sees the real numbers and the maps without needing a key —
which is the point, since these notebooks are the worked example an external analyst is pointed
at. `03_offline_demo.ipynb` stays stripped (also matching faoapi): it is deterministic and runs
in seconds, so stored output adds nothing.

The cost is honest: `02` stores its maps as embedded PNGs (about 1 MB before the rewrite), and re-executing it produces a large diff
even when nothing meaningful changed. Two consequences:

* **Do not strip the outputs** to make a diff smaller. That silently removes the only thing a
  browsing reader can see.
* **Re-execute both when the served run changes**: the outputs show a specific forecast and its
  months, so after a new delivery they describe something that is no longer live:

  ```bash
  .venv/bin/python -m pytest --nbmake notebooks/01_quickstart.ipynb notebooks/02_visualization.ipynb
  # then, to refresh the stored outputs:
  uv run --with nbconvert jupyter nbconvert --to notebook --execute --inplace \
      notebooks/01_quickstart.ipynb notebooks/02_visualization.ipynb
  ```

`--with nbconvert`, for the same reason as `--with jupyterlab` above: neither is in the dev group, and `.venv/bin/jupyter nbconvert` *looks* available — `--version` prints `7.16.6` — while actually running it raises `ModuleNotFoundError: No module named 'nbconvert'`. The command above was executed end to end before being written here.

Outputs are checked for secrets before commit: the auth cell prints `API key : set`, never the
value, and no long token appears in any stored output.

### The forecast months move

A forecast covers a window of months that advances with every delivery. `01` and `02` hard-code no
month: they read the forecast and recorded months from the data (forecast months
from the month in the `X-Forecast-Made-In` header on, recorded months before it),
so they keep working after a new delivery.

## Access, updates & citation

- **Getting a key:** request an `APPWRITE_DATASTORE_API_KEY` from the VIEWS team
  (<https://viewsforecasting.org> / your CRAF'd–VIEWS point of contact). Base URL:
  `https://crafdapi.viewsforecasting.org`.
- **Which forecast am I looking at?** The API serves one forecast, the latest good one. A new one
  replaces it each month, so **record the date you fetched alongside any number or figure you
  cite** (forecasts are not reproducible across runs).
- **Methodology, skill & responsible use:** VIEWS (<https://viewsforecasting.org>).

## See also

- **[Data dictionary](../docs/api/data_dictionary.md)** — every served column, defined.
- API reference: [`docs/api/README.md`](../docs/api/README.md)
- Authentication & data source: ADR-027, ADR-028
