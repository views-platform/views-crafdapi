> **⚠ INHERITED FROM views-faoapi — NOT YET RETARGETED. DO NOT RUN AGAINST CRAF'd.**
> Every command below (`cd ~/…/views-faoapi`, `systemctl restart views-faoapi`,
> `~/.views-faoapi-deploy-tag`, `curl faoapi.viewsforecasting.org`) targets the **live
> production FAO API** — running it from this repo would disrupt a different, UN-facing
> service. A crafd-specific release runbook is written in **S11** (needs the real crafd
> host/DNS/service, which do not exist yet). Until then this file is reference only.

# Release runbook — bringing production up to date, properly

*Written to be read by a human. One session, about an hour. Every step says what
you type, what you should see, and how to undo it. If anything looks different
from what's written here: stop, don't improvise — the old setup stays intact
until the very last step, so stopping is always safe.*

---

## What this session does, in one paragraph

The server currently runs old code from a personal account, updated by hand.
After this session it runs the current release (`v1.0.0`) from its **own service
account**, and every future update becomes two lines: write the new version
number into a file, restart the service. Rolling back is the same two lines with
the old version number. This is the same setup views-datafactory has used
smoothly for months.

## Before the session (all prepared already — just confirm)

- [ ] The release request (`development` → `main`) is approved and merged, and
      the tag `v1.0.0` exists. *(Prepared by the agent; you click approve.)*
- [ ] You can SSH into the server and use `sudo`.

*(FAO communication: sent the same evening, **after** verification succeeds — it is
part of the maintainer's larger documentation package; FAO receives it the next
working morning. The maintainer owns its content and timing.)*

## The session

### Step 1 — copy the setup script over, create the account and key  *(~5 min)*

First, from your **laptop** (your server address where `<server>` is — the same one
you SSH to; it is deliberately not written in this file):

```bash
cd ~/Documents/scripts/views_platform/views-faoapi
scp deployment/bootstrap.sh simon@<server>:/tmp/
```

*(Why scp: your account on the server has no GitHub access — only the new service
account will get its own read-only key, in the next step. Your laptop's SSH access
is all that's needed to deliver the one setup file.)*

Then SSH in and run:

```bash
bash /tmp/bootstrap.sh part1
```

You should see: `created user views-faoapi-deploy` and then a **public key**
printed, with instructions.

### Step 2 — the one browser step  *(~3 min)*

Open `github.com/views-platform/views-faoapi` → Settings → Deploy keys → *Add
deploy key*. Paste the printed key. **Do not** tick "write access". Save.

*(This is what lets the server fetch code without anyone's personal GitHub
credentials — the bus-factor fix.)*

### Step 3 — build the new deployment  *(terminal, ~10 min)*

**Credentials now follow PLATFORM-001 (þing-01 #275), not a personal `.env`:** coordinates are
**read** from the owned registry (`views-appwrite/docs/ADRs/platform/coordinate_registry.toml` —
have a pinned checkout on the box, or set `APPWRITE_REGISTRY` to its path), and the **one secret**
is supplied by **you, the operator**, in the environment — never sourced from anyone's `.env`:

```bash
export APPWRITE_DATASTORE_API_KEY='<the key, from the password manager>'   # operator secret slot
bash bootstrap.sh part2
```

You should see it clone the repo, install the toolchain, end with
`deploy-gate: serving tag vX.Y.Z (…)` and
`credentials file written (N APPWRITE_ lines: registry coordinates + 1 operator secret … expected >= 9)`.
It **fails loud** if `APPWRITE_DATASTORE_API_KEY` is unset or the registry file is missing (that is
the point — no silent copy-chain). If N is less than 9, or it aborts: **stop** — tell the agent;
nothing has been switched.

### Step 4 — install the new service definition  *(terminal, ~2 min)*

```bash
bash bootstrap.sh part3
```

You should see: `old unit preserved as views-faoapi-legacy.service` and
`unit installed`. **Traffic has still not moved.**

### Step 5 — the switch  *(terminal, ~2 min)*

```bash
sudo systemctl restart views-faoapi
sleep 5
sudo systemctl status views-faoapi --no-pager | head -12
```

You should see `active (running)` and, in the log lines,
`deploy-gate: serving tag v1.0.0`.

### Step 6 — verify together  *(terminal + browser, ~10 min)*

```bash
curl -s https://faoapi.viewsforecasting.org/ping        # -> {"status":"ok"}
curl -s https://faoapi.viewsforecasting.org/version     # -> "1.0.0" + the tag
sudo systemctl kill views-faoapi && sleep 8
curl -s -o /dev/null -w '%{http_code}\n' https://faoapi.viewsforecasting.org/ping   # -> 200 (it healed itself)
```

Then run the **post-deploy smoke test** — it verifies the live service (build,
Appwrite, and that historical **and** forecast serve with global coverage) *and*
warms the caches, so the first real consumer call is fast instead of timing out:

```bash
# from the deploy checkout, with a caller/read-scoped key exported
APPWRITE_DATASTORE_API_KEY=<caller key> .venv/bin/python scripts/smoke.py --expect-tag v1.0.0
```

Expect `ALL PASS`. The two coverage calls do the one-time cache rebuild (~2-3 min
on a cold server); the `warm check` line confirms the second call is fast. A `FAIL`
on `historical`/`forecast coverage` means a scope regression (e.g. a regional
historical); a `version` FAIL means the build didn't switch. (It warms only the
key it runs with — per-key cache; pass the caller key you want warmed.)

### Step 7 — the alarm  *(browser, ~10 min)*

Register `https://faoapi.viewsforecasting.org/ping` on the uptime monitor
(same account datafactory uses), alert to your email, and fire its test alert.
From today, if the API ever goes down, **you get told** — nobody discovers it
from a broken notebook again.

## If anything went wrong — the undo

```bash
sudo cp /etc/systemd/system/views-faoapi-legacy.service /etc/systemd/system/views-faoapi.service
sudo systemctl daemon-reload && sudo systemctl restart views-faoapi
```

That is the exact pre-session setup, byte for byte. Then tell the agent what
you saw.

## After the session (agent, solo — nothing from you)

Close out the deployment epic paperwork, tell the other repos the guard is live
(which un-freezes the forecast-delivery work everywhere), and retire the old
personal-account deployment leftovers at the next opportunity.

## Every future release, forever after

```bash
echo v1.0.1 | sudo -u views-faoapi-deploy tee /home/views-faoapi-deploy/.views-faoapi-deploy-tag
sudo systemctl restart views-faoapi
APPWRITE_DATASTORE_API_KEY=<caller key> .venv/bin/python scripts/smoke.py --expect-tag v1.0.1  # verify + warm
```

Rollback: same three lines, previous number.
