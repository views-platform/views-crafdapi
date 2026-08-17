# First CRAF'd delivery — the run behind every number the API served in August 2026

Recorded for epic #40 acceptance criterion 9: *the delivered run is reproducible — run id,
command and staging path recorded.* Every figure below is read from the staged run's manifest or
from the live API, not from memory.

## The run

| | |
|---|---|
| **Run id** | `rusty_bucket_forecasting_20260727_095355` |
| **Ensemble** | `rusty_bucket` — **8 × `ConflictologyModel` baselines**, not a trained ML model |
| **Contract version** | `1.5` (ADR-013 Sampled-Forecast Wire Contract) |
| **Coverage** | `land_gaul` — **64,742** cells, global land |
| **Window** | months **559–594** (2026-07 … 2029-06), n=36 |
| **Targets** | `lr_ged_sb`, `lr_ged_ns`, `lr_ged_os` |
| **Draws** | S=128 per cell-month |
| **Artifacts** | 108 shards + 1 sidecar + 1 manifest; 443 MB staged, plus a 164 MB historical parquet |
| **Delivered** | 2026-08-14 (manifest `$createdAt` `2026-08-14T18:35:54.962+00:00`) |
| **Visible from** | `v0.2.1`, deployed 2026-08-15 |

**This is a placeholder ensemble.** `rusty_bucket` is eight clones of the `heavy_strider`
datafactory baseline. It exists to prove the delivery path end to end; the numbers are not a
forecast anyone should cite. Replacing it with trained models requires no change to this path —
the same launcher delivers them.

## Reproducing it

The producer reads the **shared Hop-A shelf** (`production_forecasts`) and emits CRAF'd's own
Hop-B delivery. It does not depend on views-faoapi: delete that repo and this is unaffected.

```bash
cd views-models/postprocessors/un_crafd
bash run.sh
```

What that resolves to:

- `deliveries/un_crafd.py` declares `intent`, from which `wire_upload_enabled` is **derived**
  (views-models ADR-021). With `intent = paused(...)` the run stages and makes **zero store
  calls**; `intent = live(...)` arms it. Never edit `product.py`'s `UPLOAD_ENABLED`.
- `VIEWS_POSTPROCESSING_PIN="1.1.0"` — a released tag, not `@main` (views-models#294).
- Staged to `postprocessors/un_crafd/data/generated/wire_contract/<run_id>/`.

Verify a staged run before arming, from this repo:

```bash
.venv/bin/python scripts/preflight_run.py <staging_dir>   # 10 gates; ~59 s at this scale
```

## Verified after delivery

```
GET /provenance/forecast   mode: wire · source: rusty_bucket
                           run_id: rusty_bucket_forecasting_20260727_095355
                           freshness.is_stale: false · serving_state.degraded: false
GET /provenance/historical 200 · 64,742/64,742 cells · 0 unmapped · land_gaul@f74d3b2b
GET /health                healthy · appwrite_connected: true

scripts/smoke.py --expect-tag v0.2.1   ALL PASS
  forecast coverage[IDN]   1,030 cells   <- non-African: the global-scope proof
  historical coverage[IDN] 1,030 cells

notebooks 01, 02, 03       Run-All clean against the live API
```

**Known gap:** `GET /data/forecast/bulk` returns **504 after 300 s** (nginx `proxy_read_timeout`)
on a warm dataset — the bulk product cannot currently be downloaded. Tracked as **#79**; it is
the one unticked criterion on #46.

## What went wrong on the way, and what caught it

Recorded because each was caught by a gate that already existed, and the next delivery will meet
the same ones.

| Failure | Caught by |
|---|---|
| `views-datafactory` uninstallable — env is Python 3.11, the package needs ≥3.12 | the install error, ignored by a launcher with no `set -e` (views-models#392) |
| The pin was not applied — pip skips a rebuild when the version string is unchanged | `ModuleNotFoundError` on the consumer module (views-models#385) |
| Memory: the first armed attempt died mid-shard-download | rerun after freeing memory; producer-side issues filed as views-postprocessing#268/#269 |
| `v0.2.0` tagged without bumping `pyproject.toml` | **the deploy gate**, which refused to serve a mislabelled build |
| The re-cut `v0.2.0` never reached the box | `git fetch --tags` refuses to clobber and exits 1; `--quiet` hid it. Fixed with `--force`; released as `v0.2.1` |

The deploy gate refusing a version/tag mismatch is the one worth keeping in mind: it did exactly
its job, and it did so on the production box because nothing in the repo compared the two. That
check now exists (`tests/test_version.py`).
