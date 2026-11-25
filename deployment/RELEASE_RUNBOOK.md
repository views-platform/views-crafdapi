# Release runbook: running views-crafdapi on its server

*For the maintainer. crafdapi runs alone on its own server, as the `views-crafdapi` service on
`127.0.0.1:8001`, behind nginx, at `https://crafdapi.viewsforecasting.org` (ADR-039).*

This page covers six things:

0. cutting a release (choosing the version, moving it to `main`, tagging it), on your own computer;
1. releasing a new version onto the server;
2. what to do when the API is down;
3. what to do when it serves an old forecast;
4. setting up a new server;
5. adding or renewing the server's own read key.

Nothing monthly is done by hand here. (A daily automated check, `data-freshness.yml`, opens a
reminder when a monthly delivery is due.) The forecast reaches this API from upstream:
views-datafactory supplies the data, views-models runs the chain, the models are built on
views-pipeline-core and each lives in its own repository (such as views-baseline or
views-hydranet), and views-postprocessing (which may be renamed views-delivery) delivers the result
into this API's storage. (As of 17 November 2026 that last step is not live yet: the delivery into
CRAF'd's own Appwrite project is still being built in views-postprocessing.)

**Before the first partner key is handed out,** work through `deployment/CRAFD_LAUNCH_READY_LIST.md`.

**Paste one line at a time.** The hazard is not specific to `read`: any line that prompts,
including `sudo` asking for your password, reads the rest of a pasted block as its answer. That has
corrupted the deploy-tag file before.

## 0. Cut a release (~30 min, on your own computer)

A release is a version number that names one exact state of the code. Everything here happens on
GitHub and on your own computer; nothing touches the server until section 1. Do the steps in
order. If any check fails, stop. Before step 5, nothing has changed on `main` and the release can
simply be abandoned. If step 6 fails after the merge in step 5, do not tag: `main` then holds an
untagged merge, which is harmless (the server only ever runs tags). Do step 7 (merge `main` back
into `development`) anyway, then fix the cause in `development` and cut the next PATCH number from
step 1.

How the branches work: all changes are merged into `development` first. `main` holds only what
has been released. A release moves `development` into `main` in one merge, the tag is put on that
merge, and then `main` is merged back into `development` so the two stay in step. Never commit to
`main` directly, and merge into `main` only from `development`. Nothing on GitHub enforces this:
`main` has no branch protection, so it rests on following these steps. (v0.7.0 went through a
separate release branch; releases since use `development` itself.) An urgent fix takes the same
path: a pull request into `development`, then a PATCH release.

### Step 1. Check that `development` is ready

- Every pull request meant for this release is merged into `development`, and the tests passed
  on GitHub for the last of them (the green tick on the commit).
- No pull request in the release is still a draft or waiting for a decision.
- List what has changed since the last release; you need it in steps 2 and 4:

```bash
git fetch origin --tags
git log --oneline --first-parent vPREV..origin/development
```

(`vPREV` is the newest existing tag; `git tag --sort=-v:refname | head -1` prints it.)

### Step 2. Choose the version number

The number is `0.MINOR.PATCH` (for example `0.7.0`).

- **Raise MINOR, set PATCH to 0** (`0.7.0` → `0.8.0`) when a partner can see a difference: a route,
  column, level or outcome added, removed or renamed, a change in what a column means, or a change
  to the key a partner needs.
- **Raise PATCH** (`0.7.0` → `0.7.1`) when partners see no difference: fixes, documentation,
  monitoring, internal changes.
- When unsure, raise MINOR. A number that promises too little breaks a partner by surprise.

Never reuse a number that has been tagged before, even for a release that was abandoned.

### Step 3. Bump the version, through a pull request into `development`

On a new branch from `development`, change the version in two files:

```bash
git switch -c release/vX.Y.Z origin/development
```

Edit `pyproject.toml`: `version = "X.Y.Z"`. Then update the lock file to match, and check that
only the two version lines changed:

```bash
uv lock
git diff --stat
```

`git diff --stat` must list `pyproject.toml` and `uv.lock`, each with `2 +-` (one line removed, one added). If `uv.lock` shows
more, `uv lock` changed other packages too: stop, undo it with
`git checkout -- uv.lock pyproject.toml`, and ask before going on.

Partner-visible changes are added to `docs/api/CHANGELOG.md` as they are merged. Check now that
every one from step 1 is there, and add any that is missing; then `git diff --stat` also lists
`docs/api/CHANGELOG.md`, and nothing else. Commit and push:

```bash
git commit -am "release(vX.Y.Z): bump version"
git push -u origin release/vX.Y.Z
```

Open a pull request from `release/vX.Y.Z` into `development`. Merge it when its tests pass.

### Step 4. Open the release pull request: `development` into `main`

On GitHub, open a pull request with **base `main`** and **compare `development`**. Title:
`release: vX.Y.Z to main`. In its description, write for your future self:

- the list from step 1, in plain words;
- whether partners see a difference (and so whether `docs/api/CHANGELOG.md` was updated);
- anything the server needs for this release beyond section 1: a new key, a new setting, a
  changed unit file, a first start that takes longer than usual.

Read the pull request's list of changed files once. Everything in it should be something you
recognise from step 1. Wait for the tests to pass.

### Step 5. Merge it with a merge commit

Merge with **"Create a merge commit"**. Do not choose "Squash and merge" or "Rebase and merge":
both rewrite the commits, and then `main` and `development` disagree about history from that point
on. Do not delete the `development` branch when GitHub offers it.

### Step 6. Tag the merge on `main` and push the tag

```bash
git fetch origin
git log --oneline -1 origin/main
git show origin/main:pyproject.toml | grep '^version'
```

The first line must be the merge from step 5 (`Merge pull request #NNN from
views-platform/development`). The second must print `version = "X.Y.Z"`.

Make the tag on your own computer first, and check that the tagged code declares the same version
as the tag, the way the server will:

```bash
git tag -a vX.Y.Z -m "vX.Y.Z" origin/main
git switch --detach vX.Y.Z
uv run pytest tests/test_version.py -q
git switch -
```

The test must pass (not "skipped"). If it fails, delete the local tag (`git tag -d vX.Y.Z`), do
not push it, and follow the failure path at the top of this section: step 7, then a new PATCH
number. (The test's own failure text says to re-cut the tag; do not.) Only if it passes, push the
tag:

```bash
git push origin vX.Y.Z
```

**Never move or re-create a tag once it is pushed.** The server may already have fetched it, and a
re-made tag with the same name has caused a failed release before (the 2026-08-15 incident
described in `scripts/checkout-deploy-tag.sh`). If a
tagged release is wrong, release a new number. (The server's error message for a mismatch still
suggests re-cutting the tag; do not.)

### Step 7. Merge `main` back into `development`

The merge from step 5 exists only on `main`. Bring it into `development` through a pull request,
or the next release's pull request will fail its "behind" check:

```bash
git switch -c sync/main-after-vX.Y.Z origin/development
git merge --no-ff --no-edit origin/main
git push -u origin sync/main-after-vX.Y.Z
```

The merge must finish without conflicts (it only adds the release merge). Open a pull request from
`sync/main-after-vX.Y.Z` into `development`, and merge it with a merge commit when its tests pass.

### Step 8. Check that nothing was lost between the branches

```bash
git fetch origin
git merge-base --is-ancestor origin/main origin/development && echo "main is inside development: good"
```

If it does not print the second half, stop and ask: `main` has a change that `development` lacks.

The release is cut. Section 1 puts it on the server.

## 1. Release a new version (~10 min)

The tag must already exist on GitHub (section 0). Replace `vX.Y.Z` with it everywhere below, by
hand. An unedited `vX.Y.Z` fails safely: no such tag exists.

**ON THE BOX** (`ssh` first), as your own user. Write the version to deploy:

```bash
echo vX.Y.Z | sudo -u views-crafdapi-deploy tee /home/views-crafdapi-deploy/.views-crafdapi-deploy-tag
```

Check what landed in the file. Expect exactly your tag, then `\n`. If it shows anything else, stop.

```bash
sudo od -c /home/views-crafdapi-deploy/.views-crafdapi-deploy-tag
```

**Before the restart: the settings file.** It must be rebuilt when the release pins a newer
registry edition (`REGISTRY_PIN_TAG` in `src/views_crafdapi/seam_contract.py`) or needs a setting
the server lacks, and whenever it still holds a key: since #263 it holds coordinates only. This
prints how many key lines it holds (a number, never a value); anything but `0` means rebuild:

```bash
sudo grep -c 'API_KEY=' /home/views-crafdapi-deploy/.env.crafdapi
```

To rebuild, copy the setup script and the pinned registry to the server's `/tmp`, exactly as in
section 4, step 2 (the script cannot be run from the service account's home, which your user
cannot read). Then **ON THE BOX**, with the same tag you wrote above:

```bash
RELEASE_TAG=vX.Y.Z bash /tmp/crafd-bootstrap.sh part2
```

It writes the tag file again, checks the tag out and syncs its packages in the service's own
checkout (as the restart below would anyway), and replaces the settings file only once the new one
is complete. Then check that the key-line count above prints `0`.

Restart, then check the version. Both `version` and `deployed_tag` should name your tag. The
service may answer `502` for a few seconds while it restarts. After a restart it loads the forecast
by itself before answering data requests; until then those answer `503`. From the disk cache this
takes under a minute; after a new delivery, a few minutes.

```bash
sudo systemctl restart views-crafdapi
```

The restart checked out the new tag. Check whether the tag changed the service's unit file (its
start command or stop limits, #236). This shows the difference, or nothing if there is none:

```bash
sudo diff /etc/systemd/system/views-crafdapi.service /home/views-crafdapi-deploy/views-crafdapi/deployment/views-crafdapi.service
```

Only if it showed a difference: install it and restart once more. The forecast loads again, so
data requests answer `503` for up to a minute. (To see why the file changed: `git log -p
vOLD..vX.Y.Z -- deployment/views-crafdapi.service` in the checkout, where vOLD is the tag served
before.)

```bash
sudo cp /home/views-crafdapi-deploy/views-crafdapi/deployment/views-crafdapi.service /etc/systemd/system/views-crafdapi.service && sudo systemctl daemon-reload && sudo systemctl restart views-crafdapi
```

```bash
curl -s https://crafdapi.viewsforecasting.org/version
```

Then run the full check, **ON THE BOX**, as the service account:

```bash
sudo -iu views-crafdapi-deploy
```

```bash
cd views-crafdapi
```

This line prompts for our own reader key from the `crafdapi` project (Bitwarden:
`partner-views-...`, ADR-040). Paste it alone:

```bash
read -rsp "reader key: " CRAFD_KEY; echo; export CRAFD_KEY
```

Then the smoke test. Replace `RUN_ID` with the forecast run that should be served (the delivery's
name, for example `chrome_box_forecasting_20261112_210150`); it checks that this run is served and
that the data is right (months, summaries, and totals adding up from grid cells to countries):

```bash
.venv/bin/python scripts/smoke.py --expect-tag vX.Y.Z --expect-run RUN_ID --expect-draws 1024
```

Expect `ALL PASS`. Then `exit`.

**To roll back,** do the same steps with the previous tag. If the unit-file check then shows a
difference, keep the installed unit file unless the release you are undoing changed it and that
change is the problem. (The #236 unit file works with every tag from v0.5.1 on: all pin uvicorn
0.37.0, which accepts its flags.)

## 2. The API is down

A `502` for a few seconds after a restart is normal. A `502` that lasts minutes means the service
refuses to start and keeps retrying. **ON THE BOX**, this line names the cause:

```bash
sudo journalctl -u views-crafdapi -n 60 --no-pager | grep -E "deploy-gate|FATAL|oom"
```

If it prints nothing, the service has not started recently, so the cause is elsewhere: read the
last lines in full with `sudo journalctl -u views-crafdapi -n 60 --no-pager`.

`deploy-gate: serving tag ...` means it started. If that line contains `origin NOT checked`, the
fetch from GitHub failed at the start (a `WARN deploy-gate:` line above it says how), and the server
runs the tag it already had. That is right unless the tag was re-made on GitHub meanwhile: then
restart once the fetch works again. The lines starting `deploy-gate: git:` are git's own words. If
they name a `.lock` file under `.git`: the gate removes the locks a fetch takes (under `refs/`,
`packed-refs.lock`, `shallow.lock`, `FETCH_HEAD.lock`) once they are a minute old; any other lock,
such as `index.lock`, delete by hand if no git is running. A line with `oom` means it ran out of
memory and was stopped; systemd restarts it on its own. A `FATAL deploy-gate:` line names what is wrong,
usually a tag that does not exist on GitHub. Fix it (write a good tag, section 1), or restart:

```bash
sudo systemctl restart views-crafdapi
```

If the restart is refused because the service is `failed` (it gave up after 30 tries), clear that
first, then restart:

```bash
sudo systemctl reset-failed views-crafdapi
```

## 3. The API serves an old forecast

crafdapi serves the latest forecast it finds in storage. If it is old, the new one has not been
delivered. Deliveries are started from views-models (views-postprocessing only carries them here):
ask there. Nothing in this repository needs to
change.

## 4. Set up a new server (once per server, ~1 hour)

You need a fresh Ubuntu server you can `ssh` into with `sudo`. The server's own read key comes in
section 5; our reader key (Bitwarden, `partner-views-...`) is needed only for the smoke test.

1. **The domain.** Ask the administrator of `viewsforecasting.org` to point the `A` record for
   `crafdapi.viewsforecasting.org` at the new server's IP address. Check with
   `dig +short crafdapi.viewsforecasting.org`.
2. **The service account.** From your laptop, copy the setup script and the views-appwrite
   registry over (the registry holds the Appwrite project, bucket and table ids):
   ```bash
   scp deployment/bootstrap.sh <you>@<server>:/tmp/crafd-bootstrap.sh
   ```
   The registry must be the edition this repository pins (`REGISTRY_PIN_TAG` in
   `src/views_crafdapi/seam_contract.py`, today `appwrite-seam-v1.9.0`), not whatever your
   views-appwrite checkout happens to hold. Take it from the tag:
   ```bash
   git -C ../views-appwrite fetch --tags --quiet && git -C ../views-appwrite show appwrite-seam-v1.9.0:docs/ADRs/platform/coordinate_registry.toml > /tmp/coordinate_registry.toml
   ```
   ```bash
   scp /tmp/coordinate_registry.toml <you>@<server>:/tmp/coordinate_registry.toml
   ```
   Then, **ON THE BOX**, run `bash /tmp/crafd-bootstrap.sh part1`. It prints a public key.
3. **The deploy key.** On GitHub, open views-crafdapi → Settings → Deploy keys → Add deploy key.
   Paste the printed key. Leave "write access" unticked.
4. **The code and settings.** **ON THE BOX**, build. The setup script reads the registry from
   step 2 and writes the settings file (no key goes into it).
   ```bash
   RELEASE_TAG=vX.Y.Z bash /tmp/crafd-bootstrap.sh part2
   ```
5. **The service.** `bash /tmp/crafd-bootstrap.sh part3` installs it. It does not start it yet.
6. **nginx.** Install nginx and certbot:
   ```bash
   sudo apt install -y nginx certbot python3-certbot-nginx
   ```
   Then create `/etc/nginx/sites-available/crafdapi`:
   ```nginx
   server {
       server_name crafdapi.viewsforecasting.org;
       location / {
           proxy_pass http://127.0.0.1:8001;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
       }
   }
   ```
   Then enable it:
   ```bash
   sudo ln -s /etc/nginx/sites-available/crafdapi /etc/nginx/sites-enabled/crafdapi
   ```
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```
7. **The certificate,** only once step 1 works, and the server's firewall lets in ports 80 and
   443. certbot also sets up its own renewal:
   ```bash
   sudo certbot --nginx -d crafdapi.viewsforecasting.org
   ```
8. **The server's read key,** as in section 5.
9. **Start it,** then check it as in section 1:
   ```bash
   sudo systemctl enable --now views-crafdapi
   ```

## 5. The server's own read key (once per server, and once a year)

The service reads the forecast with a key of its own (ADR-044, which calls it `crafdapi-server`; its
console name is `server-` plus the month it was made, today `server-2026-11`). Partner keys
only decide who gets in. Without the server key the service starts but serves nothing: every data
request answers `503`.

1. **Create it** in the Appwrite console, in CRAF'd's project: *Overview → API keys → Create API
   key* (as in `PARTNER_KEYS.md`, section 1). Name: `server-` plus the year and month, for example
   `server-2026-11`. Expiry: one year, and not on 2027-10-29 (the day FAO's
   server key expires), so the two never need renewing on the same day. Scopes: `files.read` and
   `rows.read`, nothing else. Save it in Bitwarden under the same name. (`crafdapi-server` is this
   key's role, the name ADR-040, ADR-044, the code and the registry use; `server-YYYY-MM` is the
   console's name for the current one.)
2. **Put it on the server.** **ON THE BOX**, copy the setup script over if it is not there
   (section 4, step 2). This line prompts for the key; paste it alone:
   ```bash
   read -rsp "server key: " APPWRITE_CRAFDAPI_SERVER_API_KEY; echo; export APPWRITE_CRAFDAPI_SERVER_API_KEY
   ```
   ```bash
   bash /tmp/crafd-bootstrap.sh serverkey
   ```
   ```bash
   unset APPWRITE_CRAFDAPI_SERVER_API_KEY
   ```
3. **Restart and check:**
   ```bash
   sudo systemctl restart views-crafdapi
   ```
   Then read the service log. It must show the two requests the key's two scopes are for: a line
   `loader: store listing: newest forecast manifest ...` naming a manifest (not `NONE`), and
   `downloaded ...: N bytes` lines. If the listing says `NONE`, the key may lack `rows.read`; if a
   download fails, it may lack `files.read`. Before the project's first delivery `NONE` is
   expected, so the two scopes can only be proven once a delivery has landed (ready list, B3).
   ```bash
   sudo journalctl -u views-crafdapi -n 100 | grep -E "store listing|downloaded"
   ```
   Then run the full check as in section 1 once a forecast has been delivered; before that every
   data request answers `503` and the smoke test fails, which says nothing about the key.

**Renewing it:** create a new key as in step 1, put it on the server as in step 2, restart, check,
and only then delete the old key in the console.

