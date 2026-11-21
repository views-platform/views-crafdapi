# Release runbook: running views-crafdapi on its server

*For the maintainer. crafdapi runs alone on its own server, as the `views-crafdapi` service on
`127.0.0.1:8001`, behind nginx, at `https://crafdapi.viewsforecasting.org` (ADR-039).*

This page covers five things:
1. releasing a new version;
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

**Paste one line at a time.** The hazard is not specific to `read`: any line that prompts,
including `sudo` asking for your password, reads the rest of a pasted block as its answer. That has
corrupted the deploy-tag file before.

## 1. Release a new version (~10 min)

The tag must already exist on GitHub (`git push origin vX.Y.Z`). Replace `vX.Y.Z` with it
everywhere below, by hand. An unedited `vX.Y.Z` fails safely: no such tag exists.

**ON THE BOX** (`ssh` first), as your own user. Write the version to deploy:

```bash
echo vX.Y.Z | sudo -u views-crafdapi-deploy tee /home/views-crafdapi-deploy/.views-crafdapi-deploy-tag
```

Check what landed in the file. Expect exactly your tag, then `\n`. If it shows anything else, stop.

```bash
sudo od -c /home/views-crafdapi-deploy/.views-crafdapi-deploy-tag
```

Restart, then check the version. Both `version` and `deployed_tag` should name your tag. The
service may answer `502` for a few seconds while it restarts. After a restart it loads the forecast
by itself before answering data requests; until then those answer `503`. From the disk cache this
takes under a minute; after a new delivery, a few minutes.

```bash
sudo systemctl restart views-crafdapi
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

This line prompts for our own reader key (Bitwarden: `partner-views-...`, ADR-040). Paste it alone:

```bash
read -rsp "reader key: " APPWRITE_DATASTORE_API_KEY; echo; export APPWRITE_DATASTORE_API_KEY
```

```bash
.venv/bin/python scripts/smoke.py --expect-tag vX.Y.Z
```

Expect `ALL PASS`. Then `exit`.

**To roll back,** do the same steps with the previous tag.

## 2. The API is down

A `502` for a few seconds after a restart is normal. A `502` that lasts minutes means the service
refuses to start and keeps retrying. **ON THE BOX**, this line names the cause:

```bash
sudo journalctl -u views-crafdapi -n 60 --no-pager | grep -E "deploy-gate|FATAL|oom"
```

If it prints nothing, the service has not started recently, so the cause is elsewhere: read the
last lines in full with `sudo journalctl -u views-crafdapi -n 60 --no-pager`.

`deploy-gate: serving tag ...` means it started. A line with `oom` means it ran out of memory and
was stopped; systemd restarts it on its own. A `FATAL deploy-gate:` line names what is wrong,
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

You need a fresh Ubuntu server you can `ssh` into with `sudo`, and our own reader key from
Bitwarden.

1. **The domain.** Ask the administrator of `viewsforecasting.org` to point the `A` record for
   `crafdapi.viewsforecasting.org` at the new server's IP address. Check with
   `dig +short crafdapi.viewsforecasting.org`.
2. **The service account.** From your laptop, copy the setup script and the views-appwrite
   registry over (the registry holds the Appwrite project, bucket and table ids):
   ```bash
   scp deployment/bootstrap.sh <you>@<server>:/tmp/crafd-bootstrap.sh
   ```
   ```bash
   scp ../views-appwrite/docs/ADRs/platform/coordinate_registry.toml <you>@<server>:/tmp/coordinate_registry.toml
   ```
   Then, **ON THE BOX**, run `bash /tmp/crafd-bootstrap.sh part1`. It prints a public key.
3. **The deploy key.** On GitHub, open views-crafdapi → Settings → Deploy keys → Add deploy key.
   Paste the printed key. Leave "write access" unticked.
4. **The code and credentials.** **ON THE BOX**, put our reader key in the environment (the line
   prompts; paste it alone), then build. The setup script reads the registry from step 2 and
   refuses to run without the key. Then remove the key from your shell.
   ```bash
   read -rsp "reader key: " APPWRITE_DATASTORE_API_KEY; echo; export APPWRITE_DATASTORE_API_KEY
   ```
   ```bash
   RELEASE_TAG=vX.Y.Z bash /tmp/crafd-bootstrap.sh part2
   ```
   ```bash
   unset APPWRITE_DATASTORE_API_KEY
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

The service reads the forecast with a key of its own, `crafdapi-server` (ADR-044). Partner keys
only decide who gets in. Without the server key the service starts but serves nothing: every data
request answers `503`.

1. **Create it** in the Appwrite console, in CRAF'd's project: *Overview → API keys → Create API
   key* (as in `PARTNER_KEYS.md`, section 1). Name: `crafdapi-server`. Expiry: one year, and not on 2027-10-29 (the day FAO's
   server key expires), so the two never need renewing on the same day. Scopes: `files.read` and
   `rows.read`, nothing else. Save it in Bitwarden under the same name.
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
   download fails, it may lack `files.read`.
   ```bash
   sudo journalctl -u views-crafdapi -n 100 | grep -E "store listing|downloaded"
   ```
   Then run the full check as in section 1.

**Renewing it:** create a new key as in step 1, put it on the server as in step 2, restart, check,
and only then delete the old key in the console.

