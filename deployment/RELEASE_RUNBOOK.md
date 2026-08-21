# Release runbook — deploying views-crafdapi (co-hosted with faoapi)

*Written to be read by a human. One session, about an hour. Every step says what
you type, what you should see, and how to undo it. crafdapi is deployed as a
**second, independent service on the same box that already runs faoapi** — it
never touches the faoapi service, so if anything looks wrong you can stop at any
point and faoapi keeps serving untouched.*

---

## What this session does, in one paragraph

The box (Hetzner CPX52) already runs the **FAO** API as the `views-faoapi` service
on port **8000**, behind the reverse proxy at `faoapi.viewsforecasting.org`. This
session stands up the **CRAF'd** API alongside it as a *separate* `views-crafdapi`
service on port **8001**, under its **own service account** (`views-crafdapi-deploy`),
its **own credentials file** (`.env.crafdapi`, CRAF'd coordinates + CRAF'd key —
never faoapi's), reached at `crafdapi.viewsforecasting.org`. Every future update is
two lines: write the new version into a file, restart the service. Rollback is the
same two lines with the old version.

**Expect honest 503s at the end of this session — that is success, not failure.**
The CRAF'd Appwrite bucket is still **empty** (the CRAF'd producer has not run its
first delivery yet). So `/ping` and `/version` will pass, but the forecast and
historical endpoints will **fail-visible with 503** until the first delivery lands.
That is the designed behaviour (ADR-033): the API refuses to serve rather than
serve nothing silently. Re-run the smoke test after the producer's first delivery
to see it flip to serving real data.

## Before the session (confirm these first)

- [ ] The release (`development` → `main`) is merged and the tag **`v0.1.0`** exists
      on `github.com/views-platform/views-crafdapi`. *(Prepared by the agent; you
      click approve.)*
- [ ] **DNS:** an `A` record for `crafdapi.viewsforecasting.org` points at the
      **same box IP** as `faoapi.viewsforecasting.org`. *(Registrar action — do this
      first; DNS can take time to propagate. `dig +short crafdapi.viewsforecasting.org`
      should return the box IP before you start Step 5.)*
- [ ] You can SSH into the box and use `sudo`.
- [ ] You have the **CRAF'd** read-scoped serve key in your password manager
      (`CRAFD_CALLER_API_KEY` / the crafd datastore-serve key — issued in S9). It is
      **not** faoapi's key.

## The session

### Step 1 — copy the setup script over, create the account and key  *(~5 min)*

From your **laptop** (`<box>` = the same address you SSH to; deliberately not written here):

```bash
cd ~/Documents/scripts/views_platform/views-crafdapi
scp deployment/bootstrap.sh simon@<box>:/tmp/crafd-bootstrap.sh
```

Then SSH in and run:

```bash
bash /tmp/crafd-bootstrap.sh part1
```

You should see `created user views-crafdapi-deploy` and a **public key** printed.
*(This is a brand-new account, separate from `views-faoapi-deploy`. Nothing about
faoapi changes.)*

### Step 2 — the one browser step  *(~3 min)*

Open `github.com/views-platform/views-crafdapi` → Settings → Deploy keys → *Add
deploy key*. Paste the printed key. **Do not** tick "write access". Save.

*(This lets the box fetch crafd code with its own read-only key — no personal
GitHub credentials, and no sharing of faoapi's deploy key.)*

### Step 3 — build the deployment  *(terminal, ~10 min)*

Coordinates are **read** from the owned registry
(`views-appwrite/docs/ADRs/platform/coordinate_registry.toml` — have a pinned
checkout on the box, or set `APPWRITE_REGISTRY` to its path); the registry supplies
the **CRAF'd** coordinates (`APPWRITE_CRAFD_*`). The **one secret** is supplied by
**you**, in the environment — never from anyone's `.env`:

```bash
export APPWRITE_DATASTORE_API_KEY='<the CRAF'd serve key, from the password manager>'
bash /tmp/crafd-bootstrap.sh part2
```

You should see it clone the repo at `v0.1.0`, install the toolchain, and end with
`deploy-gate: serving tag v0.1.0 (…)` and a line confirming the credentials file
was written (registry CRAF'd coordinates + 1 operator secret). It **fails loud** if
`APPWRITE_DATASTORE_API_KEY` is unset or the registry file is missing. If it aborts:
**stop** — tell the agent; nothing has been switched, and faoapi is untouched.

### Step 4 — install the service definition  *(terminal, ~2 min)*

```bash
bash /tmp/crafd-bootstrap.sh part3
```

You should see `unit installed` for `views-crafdapi` (on port **8001**). There is no
legacy unit to preserve — this is a new service. **No traffic reaches it yet** (the
reverse proxy has no route to it until Step 5).

### Step 5 — add the reverse-proxy route  *(terminal, ~5 min)*

The box already terminates TLS and proxies `faoapi.viewsforecasting.org` → `127.0.0.1:8000`.
Add a sibling vhost for crafd → `127.0.0.1:8001`. Use whichever proxy the box runs:

**If nginx** — create `/etc/nginx/sites-available/crafdapi` (mirror the faoapi vhost,
changing the server_name and port), enable it, and reload:

```nginx
server {
    server_name crafdapi.viewsforecasting.org;
    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    # TLS: reuse certbot for this server_name, e.g.
    #   sudo certbot --nginx -d crafdapi.viewsforecasting.org
}
```

```bash
sudo ln -s /etc/nginx/sites-available/crafdapi /etc/nginx/sites-enabled/crafdapi
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d crafdapi.viewsforecasting.org   # if TLS not already covered
```

**If Caddy** — add a block to the `Caddyfile` and reload:

```
crafdapi.viewsforecasting.org {
    reverse_proxy 127.0.0.1:8001
}
```
```bash
sudo systemctl reload caddy
```

*(Caddy provisions TLS automatically; nginx needs the certbot line. Confirm with the
agent which proxy is on the box if unsure — `systemctl status nginx caddy`.)*

### Step 6 — the switch  *(terminal, ~2 min)*

```bash
sudo systemctl enable --now views-crafdapi
sleep 5
sudo systemctl status views-crafdapi --no-pager | head -12
```

You should see `active (running)` and, in the log lines, `deploy-gate: serving tag v0.1.0`.

### Step 7 — verify together  *(terminal, ~10 min)*

```bash
curl -s https://crafdapi.viewsforecasting.org/ping        # -> {"status":"ok"}
curl -s https://crafdapi.viewsforecasting.org/version     # -> "0.1.0" + the tag
```

Then the smoke test.

> **This step is the record of the 2026-08-02 first stand-up, not a description of today.**
> At that point the CRAF'd bucket was empty, so the forecast/historical coverage checks
> reported an honest **503** and only `ping` and `version` could pass. The producer's first
> delivery landed **2026-07-27**; since then `smoke.py` returns **ALL PASS**, and a 503 from
> the coverage checks is a real outage, not an expected state. Read the version numbers below
> as `v0.1.0`-era history; for any deploy after the first, use the recurring block under
> *"Every future release"* and expect ALL PASS.

```bash
# from the deploy checkout, with the CRAF'd caller/read-scoped key exported
APPWRITE_DATASTORE_API_KEY=<CRAF'd caller key> .venv/bin/python scripts/smoke.py --expect-tag v0.1.0
```

Expect: `ping` PASS, `version` PASS; `historical`/`forecast coverage` reporting the
honest 503. If instead `/ping` fails, `/version` is wrong, or the service crash-loops
(`journalctl -u views-crafdapi -f`), **stop** and tell the agent. **After the
producer's first delivery, re-run this line — it should then read `ALL PASS`** and
the caches warm.

### Step 8 — the alarm  *(browser, ~10 min)*

Register `https://crafdapi.viewsforecasting.org/ping` on the uptime monitor (the same
account faoapi/datafactory use), alert to your email, fire its test alert. `/ping`
stays green even while forecasts 503 — it tracks liveness, not data-readiness.

## If anything went wrong — the undo

crafdapi is a wholly separate service; undo cannot affect faoapi:

```bash
sudo systemctl disable --now views-crafdapi
sudo rm -f /etc/nginx/sites-enabled/crafdapi   # nginx; or remove the Caddy block
sudo systemctl reload nginx                     # or: reload caddy
```

faoapi (`views-faoapi` on :8000) is untouched throughout. Then tell the agent what you saw.

## Every future release, forever after

**Every command in this section runs ON THE BOX.** SSH in first. None of it works from a
laptop — `views-crafdapi-deploy` does not exist there, so the first line fails with
`sudo: unknown user views-crafdapi-deploy`. That failure is safe (nothing is changed) and it
has happened, because the block below labels *which user* and never labelled *which host*.

```bash
# === ON THE BOX (ssh first) — as your own user ===
TAG=vX.Y.Z   # <-- the ONLY line to change. Set it to the tag you are deploying.
echo $TAG | sudo -u views-crafdapi-deploy tee /home/views-crafdapi-deploy/.views-crafdapi-deploy-tag
sudo od -c /home/views-crafdapi-deploy/.views-crafdapi-deploy-tag   # <-- CHECK: exactly your tag, then \n
sudo systemctl restart views-crafdapi
curl -s https://crafdapi.viewsforecasting.org/version     # expect version AND deployed_tag = $TAG

# === still ON THE BOX — now as the deploy user: the checkout is 0750, so `cd` fails for you ===
sudo -iu views-crafdapi-deploy
cd views-crafdapi
read -rsp "caller key: " APPWRITE_DATASTORE_API_KEY; echo; export APPWRITE_DATASTORE_API_KEY
.venv/bin/python scripts/smoke.py --expect-tag "$TAG"   # verify + warm
exit
```

Three things that have each cost a release: the deploy checkout is **not readable by your own
user**, so `cd` into it fails and `sudo cd` cannot work (`cd` is a shell builtin) — use
`sudo -iu`. `read -rs` prints **no prompt**, so it looks like a hang; `-rsp` gives it one. And
paste those two lines **separately** — pasted together, `read` swallows the next line as the key.

**That hazard is not specific to `read`.** Any command in a pasted block that prompts — `read`,
and `sudo` asking for a password — is reading the terminal while the rest of your paste is still
buffered, and can consume it. On 2026-08-21 a pasted release block left the deploy-tag file
containing `sudo systemctl restart views-crafdapi5`, and a later line in the same paste came out
as `sudo cp <src> /etsudo cp <src> /etc/systemd/system/` — a command containing a fragment of
itself. **Paste one line at a time, or run the block over `ssh -t <box> '...'` as a single
command.**

That is why `od -c` on the tag file sits between writing it and restarting. A corrupted tag
usually fails loudly at the deploy gate (`refs/tags/<junk>` does not resolve) — but the case
worth guarding is the quiet one, where the corruption happens to spell a *different real tag*
and the service deploys the wrong version while every check looks fine. That is C-266's failure
with a new cause.

Inside that shell `sudo` prompts for *the service account's* password, which does not exist.
`exit` first for anything needing sudo.

Rollback: the same four lines with the previous tag.

The tag was written out twice here, and the block was still naming `v0.2.0` two releases after
it stopped being current — so pasting it as-is would have silently rolled production back while
looking like a deploy. One variable, changed once, is why it is written this way.

That rewrite reduced the trap without removing it: `TAG=v0.4.0` was still a real, plausible tag
that pasted cleanly and would be stale at the next release exactly as `v0.2.0` had been. It is
now a **placeholder**. An unedited paste fails at the deploy gate — `checkout-deploy-tag.sh`
cannot resolve `refs/tags/vX.Y.Z` and refuses — which is the correct direction: a stale
placeholder fails loudly, a stale real tag succeeds at deploying the wrong version. Register
**C-266**.

**The tag must exist on the remote before the restart** — the service checks out what this file
names, so a tag that is only local leaves the service on the old version while the file claims
otherwise. `git push origin <tag>` first.

`--expect-tag` reads `/version`, so it fails until the restart has actually taken. That is the
check working, not a problem; re-run it a few seconds later.

### Which key, and where it lives

One secret, filed under several names — this is register **C-256**, and it has cost real time
twice. For the smoke test and any `x-api-key` call you want the **caller** key:

| filed as | env var | used by |
|---|---|---|
| `Appwrite caller key — CRAF'd` (password manager) | `APPWRITE_DATASTORE_API_KEY` | `smoke.py`, notebooks, any consumer call — sent as the `X-API-Key` header |

The password manager entry is the source of truth. Verify it before pasting it anywhere, without
printing it:

```bash
read -rsp "paste the key: " K; echo
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -H "x-api-key: $K" https://crafdapi.viewsforecasting.org/health
unset K   # 200 = right key, 401 = wrong entry
```

Both keys expire **2026-11-17** (C-84). Update every home in one change.

Deployed so far: **v0.1.0** (2026-08-02, first stand-up — bucket empty by design, honest 503s),
**v0.2.0** (the first CRAF'd delivery is live and `/provenance/forecast` reports it), **v0.3.0**
(2026-08-17, the #70 fix), **v0.4.0** (ADR-030 S7 — the aggregate path stops round-tripping its
samples through pandas; `/data/forecast/bulk` 501 s → 31 s, peak RSS 13.7 → 6.7 GB, served output
byte-identical), **v0.5.0** (C-263/#98 — the cold-start historical load is streamed into the
value-dir a row group at a time instead of being decoded whole; peak 12.2 → 3.9 GB measured
locally, served output byte-identical. Tagged but **never deployed** — superseded by v0.5.1), **v0.5.1** (the streamed
ingest above, plus a bounded restart loop: the deploy gate's refusals are deterministic and were
being retried forever, which is what the 47-minute incident on 2026-08-15 actually was.
Deployed 2026-08-21: **cold-start peak 16.8 G -> 7.3 G measured**, served
bulk artifact byte-count unchanged at 461,991 B, streamed ingest confirmed in the journal).


## Taking a cold-start memory measurement

`#98`/C-263 needs the peak RSS of a **cold historical load** on the box. Two things make that
harder than "restart and curl", and the first version of this section got both wrong — it is
written out here so the next person does not repeat it.

### It is not enough to restart

A restart does **not** deploy a new version, and it does **not** clear the dataset cache.

**Deploy first.** The service serves whatever tag the deploy-tag file names. Restarting without
writing the file re-deploys the version already running — which is what happened on 2026-08-21,
producing a normal restart window that was briefly mistaken for an outage. Use the `TAG=` block
above, then confirm before going further:

```bash
curl -s https://crafdapi.viewsforecasting.org/version    # version AND deployed_tag must be the new one
```

**Then clear the historical disk cache.** `CrafdDiskCacheManager` persists the historical dataset
as a value-dir with a **3.5-week TTL**, deliberately surviving restarts (C-66). Its validity key
is `CACHE_SCHEMA_VERSION`, derived from the meta layout and `value_format._VALUE_SCHEMA_VERSION`
— so a release that changes neither leaves the existing entry valid, and the ingest path being
measured never runs. `from_value(mmap=True)` returns the cached dataset in about a second, the
process peaks near the steady state, and the number looks wonderful while measuring nothing.

```bash
# ON THE BOX, as the deploy user. These are caches — removing them costs one cold rebuild,
# which is the thing being measured. There is one partition per API key (a salted HMAC of the key hash),
# so clear all of them rather than guessing which key the request will use.
sudo -u views-crafdapi-deploy bash -c \
  'rm -rf /home/views-crafdapi-deploy/views-crafdapi/apis/un_crafd/cache/datasets/*_historical_value \
          /home/views-crafdapi-deploy/views-crafdapi/apis/un_crafd/cache/datasets/*_historical_meta.json'
```

Leave `.partition_salt` alone — deleting it under running workers splits the partition namespace
until they restart (#341).

### The measurement

```bash
# === ON THE BOX ===
sudo systemctl restart views-crafdapi
curl -s -o /dev/null -w '%{http_code} %{size_download} %{time_total}\n' \
     -H "X-API-Key: $APPWRITE_DATASTORE_API_KEY" \
     https://crafdapi.viewsforecasting.org/data/forecast/bulk
systemctl status views-crafdapi | grep Memory     # the peak covers that one request
```

Expect `200`, a few hundred KB, and tens of seconds. A `502` means the restart has not finished
binding the socket — wait and retry rather than concluding anything; see *"A restart is not an
outage"* below.

### Reference points

| | crafd cold | crafd steady | + faoapi 5.9 G | of 22 GiB |
|---|---|---|---|---|
| v0.4.0 | 16.8 G | 6.0 G | 22.7 G | **does not fit** |
| **v0.5.1** (2026-08-21) | **7.3 G** | 3.7 G | **13.2 G** | 8.8 GiB headroom |

Record what you actually see on **C-263** and in the ADR-030 addendum either way — a
disappointing number is the useful one, because it means the fix is not done. v0.5.1 came in
below its own ~11 G prediction, so treat a figure from a local rig as a direction, not a number.

**The clearing step above is not optional.** Skip it and the disk cache serves the previous
value-dir: the request returns in about a second, the peak sits near the steady state, and the
result looks excellent while measuring code that never ran (register **C-282**). Confirm which
path executed before believing any figure:

```bash
sudo journalctl -u views-crafdapi --since "-10 min" --no-pager \
  | grep -iE "Streamed historical|Successfully loaded historical"
```

`Streamed historical value-dir: … (C-263 low-memory path)` is the new path.
`Successfully loaded historical dataframe with 28421738 rows` is the old one.

## A restart is not an outage — and a long 502 is not a slow restart

**A successful restart is about three seconds.** Measured on the box, 2026-08-21:

```
10:06:21  Stopping views-crafdapi.service...
10:06:23  deploy-gate: serving tag v0.4.0 (v0.4.0, 1fb30b6)
10:06:24  Uvicorn running on http://127.0.0.1:8001
```

`ExecStartPre` — `git fetch`, `git checkout`, `uv sync --frozen`, the version probe — took ~2 s,
because `uv sync` is a genuine no-op when the tag's lockfile is already installed. During that
window nginx has no upstream and returns **502 Bad Gateway**. If you curl immediately after
`systemctl restart`, that is what you will see, and it means nothing.

**So a 502 lasting minutes is not a slow start — it is a gate that is failing and retrying.**
`Restart=always` with `RestartSec=5` retries a *deterministic* failure forever: a tag that does
not exist on origin, a tag/version mismatch, a lockfile `uv sync --frozen` rejects. The gate is
right to refuse, but nothing bounds the retry, so the service 502s indefinitely and
`systemctl status` reports `activating`, never `failed`. That is the shape of the 47- and
20-minute incidents on 2026-08-15 (the tag/version drift that produced `v0.2.1`) and the
11-minute one on 2026-08-17.

Diagnosis, in order:

```bash
# === ON THE BOX ===
systemctl status views-crafdapi --no-pager -l      # active(running) + ExecStartPre status=0/SUCCESS?
sudo journalctl -u views-crafdapi -n 60 --no-pager  # `deploy-gate: serving tag ...` or a FATAL line
```

`journalctl` without `sudo` prints "No entries" unless your user is in `adm`/`systemd-journal` —
that is a permissions artefact, not a silent service. The gate prints exactly one line on
success and a `FATAL deploy-gate:` naming the cause on failure; that line is the whole diagnosis.

Rollback is the `TAG=` block with the previous tag.


## Memory ceilings, and removing the temporary one

`deployment/views-crafdapi.service` declares `MemoryHigh=9G` / `MemoryMax=11G`, derived from the
**measured cold start** of 7.3 G (2026-08-21, v0.5.1) — not from the 3.7 G steady state, which is
the distinction C-263 exists for: a ceiling sized from the steady state kills the service on every
restart and looks generous until it does.

A temporary `MemoryMax=14G` was set during that measurement with
`systemctl set-property --runtime`. **It takes precedence over the unit file**, so installing the
new unit is not enough — the runtime drop-in has to go:

```bash
# === ON THE BOX ===
systemctl show views-crafdapi -p MemoryMax -p MemoryHigh        # what is actually in effect
sudo rm -f /run/systemd/system.control/views-crafdapi.service.d/50-MemoryMax.conf
sudo rmdir --ignore-fail-on-non-empty /run/systemd/system.control/views-crafdapi.service.d
sudo systemctl daemon-reload
systemctl show views-crafdapi -p MemoryMax -p MemoryHigh        # expect 11G / 9G from the unit
```

Do **not** use `systemctl revert views-crafdapi` to clear it. `revert` removes overrides in both
`/run` *and* `/etc` — and this unit lives in `/etc/systemd/system/`, so it would delete the unit
file itself.

The values take effect on the next restart. Confirm with `systemctl show` rather than by reading
the unit file, because that is exactly the gap the runtime drop-in created.

**views-faoapi still has no ceiling**, and that is the other half of #99. Its 4.8 G is an
*observed* resident figure, not a measured cold start — the same kind of number crafd's own
steady state was, and crafd's cold start turned out to be 2x it. Take faoapi's cold start with
the procedure above before sizing anything: restart it, clear its cached value-dirs, one heavy
request, read the peak. Sizing it from 4.8 G would repeat C-263 on the neighbour.
