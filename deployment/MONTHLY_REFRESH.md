# The monthly data refresh

*How new fatalities data and new forecasts reach CRAF'd each month, what is automated, what is
not, and how to tell when a month has been missed. Written 2026-08-23, when it became clear this
procedure existed only in people's heads.*

---

## The chain

Four steps. **Only the first is automated.**

| # | what happens | where | automated? |
|---|---|---|---|
| 1 | harvest + compile fatalities | views-datafactory, on the Hetzner box | **yes** — cron `0 0 21 * *` |
| 2 | **Hop A** — run the ensemble → shared `production_forecasts` shelf | views-models | **no** — typed by hand |
| 3 | **Hop B** — `un_crafd` postprocessor → the CRAF'd Appwrite bucket | views-models → views-postprocessing | **no** — typed by hand |
| 4 | crafdapi reads the bucket and serves it | this repo | yes, automatic |

**One Hop B run delivers both artifacts.** The forecast wire run and the historical parquet are
uploaded by the same `_save_contract()` call, behind the same interlock. It is one command, not two.

## When

Historical fatalities lag one month: **July's numbers arrive around 20 August**, and so on.
datafactory's cron then compiles on the **21st**.

So the delivery runs **on or after the 22nd** — and **on or after the 22nd of the month whose data
you want**, which is not the same thing. Running on 25 August delivers July; August's fatalities do
not arrive until ~20 September. If the producer's inferred boundary has already advanced to August
(it declares the month it ran in), the clip will keep an August that nobody has reported yet. See
*Before you run it* below.

> This arrival window is written down here because it is written down **nowhere else**. It is not in
> views-datafactory's source catalog, and its own 21st-of-the-month cron carries no comment
> explaining the date. If the upstream release calendar changes, this file is the thing to correct.

## Running it

Both hops are shell scripts a human runs. Neither is scheduled.

```bash
# Hop A — produce the forecast onto the shared shelf
cd views-models/ensembles/rusty_bucket
bash run.sh --monthly

# Hop B — deliver forecast + historical into the CRAF'd bucket
cd views-models/postprocessors/un_crafd
bash run.sh
```

Hop B needs the Appwrite coordinate registry, `APPWRITE_DATASTORE_API_KEY`, a `GITHUB_TOKEN` for the
pinned install, and a `~/.netrc` entry for the datafactory zarr host.

**There is no `--dry-run` flag.** The `wire_upload_enabled` interlock *is* the dry-run control: with
the delivery declared `paused`, a full run executes, stages everything locally, and makes zero
Appwrite calls.

**Neither hop takes a month argument.** Hop A derives its window from the system clock at run time;
Hop B ships whatever the newest manifested run on the shelf is, and clips the historical leg to the
producer's observed boundary. So *when* you run it determines *what* you get.

## Blockers — CLEARED as of 2026-08-25

Both are done. Verified, not assumed:

1. ~~**views-models#399 is unmerged**~~ — **MERGED.** `deliveries/un_crafd.py` no longer declares
   `paused`, so `wire_upload_enabled` is armed and a run makes real Appwrite calls.
2. ~~**views-models#403 is open**~~ — **CLOSED.** Both launchers are pinned to
   `views-postprocessing 1.1.1`, past the download fail-open that broke the first delivery.

**A run today is a real delivery, not a dry run.** That is the change: with `paused` in place the
interlock made every mistake free. It no longer does.

## Before you run it — check the frontier month

**This is the step that did not exist when this file was written, and it is the one that matters.**

Hop B clips the historical leg to the producer's **observed boundary**, and that boundary is
*inferred from non-zero sums* (views-datafactory **C-355**). A single stray event in a month is
enough to declare that whole month observed. The grid is dense, so the month then ships as 64,742
cells of almost entirely zeros — which reads to a consumer as a real and unusually peaceful month,
not as a missing one.

That is not hypothetical. It is what the 2026-08-14 artifact did, and what CRAF'd is serving now:

| month | cells with any event | `lr_ged_sb` | `lr_ged_ns` | `lr_ged_os` |
|---|---|---|---|---|
| 558 (June) | 572 | 2974.0 | 771.0 | 625.0 |
| **559 (July)** | **6** | **0.0** | **12.0** | **3.0** |

Six cells declared July observed. See register **C-293**.

**The check, before delivering:** read the artifact Hop B stages and count cells carrying **any**
event per month, across **all three** targets — not the sum of one column, which is what produced
the original wrong reading. A drop from several hundred cells to single digits means the newest
month is a frontier artefact, whatever the boundary says.

```python
import pandas as pd
df = pd.read_parquet(STAGED_HISTORICAL_PARQUET).reset_index()
t = ["lr_ged_sb", "lr_ged_ns", "lr_ged_os"]
for m in sorted(df.month_id.unique())[-5:]:
    s = df[df.month_id == m]
    print(m, int((s[t].fillna(0) != 0).any(axis=1).sum()), [float(s[c].sum()) for c in t])
```

**Then decide, deliberately:** ship the sparse month, or hold the delivery until the month is
reported. There is no flag for this — the clip will keep it either way. Record which you chose and
why, because the artifact cannot answer that question afterwards (views-postprocessing#297: nothing
stamps the boundary a run clipped against).

## Verifying afterwards

Same checks that closed #46. `/health` and the data routes need `APPWRITE_DATASTORE_API_KEY` in the `X-API-Key` header; `/version` does not.

```bash
curl -s -H "X-API-Key: $KEY" https://crafdapi.viewsforecasting.org/provenance/forecast | python3 -m json.tool
curl -s -H "X-API-Key: $KEY" https://crafdapi.viewsforecasting.org/health | python3 -m json.tool
```

| what to look for | good |
|---|---|
| `mode` | `"wire"` |
| `source` | present, **not** `"unknown"` |
| `run_id` | the run you just delivered |
| `freshness.is_stale` | `false` |
| `serving_state.degraded` | `false` or absent |
| `/health` `status` | `"healthy"`, not `"degraded"` |

Then, on the box, the fuller check:

```bash
sudo -iu views-crafdapi-deploy
cd views-crafdapi
read -rsp "APPWRITE_DATASTORE_API_KEY: " APPWRITE_DATASTORE_API_KEY; echo; export APPWRITE_DATASTORE_API_KEY
.venv/bin/python scripts/smoke.py --expect-tag "$(curl -s https://crafdapi.viewsforecasting.org/version | python3 -c 'import sys,json; print(json.load(sys.stdin)["deployed_tag"])')"
```

Paste those two lines **separately** — `read` will otherwise swallow the next line, which has cost
this project a release before.

**The first call after a delivery is slow.** It triggers a cold load: last measured **62 s at 7.3 G
peak**. `smoke.py` warms the cache and retries once. A slow first call is not a failure.

## When a month is missed

`.github/workflows/data-freshness.yml` polls `/health` daily and opens one issue titled
**"Served data is stale or degraded"** when the served forecast passes its 45-day SLA. It closes
itself when a fresh delivery lands.

That issue means **a delivery was missed, not that the service is down**. Do not restart anything —
the endpoints are answering, they are just serving something old. Come back to this page and run the
two hops.

Two monitors, two questions, deliberately kept apart:

| monitor | question | cadence |
|---|---|---|
| Better Stack liveness | is the service up? | 3 min, email |
| `data-freshness.yml` | is what it serves still current? | daily, opens an issue |

Neither should alert on the other's failure. A duplicate alarm for one event teaches people to
ignore both.

## What would make this better

Not done, and each is a real piece of work rather than a tweak:

- **Schedule the two hops.** Nothing does today. The platform's convention is GitHub Actions with
  `workflow_dispatch`, and `schedule:` is a one-line addition — but the launcher needs conda,
  several GB of memory, and network reach to both Appwrite and the zarr host. No such runner is
  provisioned.
- **Wire views-models' `tools/liveness`.** It already exists as "the observation side", checking
  whether a `live()` delivery has actually produced anything recently. It is connected to nothing.
  It would catch a missed month at the producer, where this file's monitor only catches it at the
  consumer — 45 days later.
