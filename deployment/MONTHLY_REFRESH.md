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

So the delivery runs **on or after the 22nd**.

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

## Blockers as of 2026-08-23

Both are in views-models, and both must be cleared before a real delivery.

1. **views-models#399 is unmerged.** `deliveries/un_crafd.py` on `development` still declares
   `intent = paused(...)`, with a reason that is now false — *"crafdapi's first delivery has not
   been executed"*, when it has. `paused` is a real interlock, not a comment: it derives
   `wire_upload_enabled=False` and the manager makes no store calls. **A run today would be a dry
   run.**
2. **views-models#403 is open.** Both launchers are still pinned to `views-postprocessing 1.1.0`,
   which carries the download fail-open that broke the first CRAF'd delivery on 2026-08-13. `1.1.1`
   fixes it.

## Verifying afterwards

Same checks that closed #46. `/health` and the data routes need the caller key; `/version` does not.

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
read -rsp "caller key: " APPWRITE_DATASTORE_API_KEY; echo; export APPWRITE_DATASTORE_API_KEY
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
