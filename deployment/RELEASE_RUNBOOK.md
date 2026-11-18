# Release runbook: running views-crafdapi on its server

*For the maintainer. crafdapi runs alone on its own server, as the `views-crafdapi` service on
`127.0.0.1:8001`, behind nginx, at `https://crafdapi.viewsforecasting.org` (ADR-039).*

This page covers four things:
1. releasing a new version;
2. what to do when the API is down;
3. what to do when it serves an old forecast;
4. setting up a new server.

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
service may answer `502` for a few seconds while it restarts.

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
sudo journalctl -u views-crafdapi -n 60 --no-pager | grep -E "deploy-gate|FATAL"
```

If it prints nothing, the service has not started recently, so the cause is elsewhere: read the
last lines in full with `sudo journalctl -u views-crafdapi -n 60 --no-pager`.

`deploy-gate: serving tag ...` means it started. A `FATAL deploy-gate:` line names what is wrong,
usually a tag that does not exist on GitHub. Fix it (write a good tag, section 1), or restart:

```bash
sudo systemctl restart views-crafdapi
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
2. **The service account.** From your laptop, copy the setup script over:
   ```bash
   scp deployment/bootstrap.sh <you>@<server>:/tmp/crafd-bootstrap.sh
   ```
   Then, **ON THE BOX**, run `bash /tmp/crafd-bootstrap.sh part1`. It prints a public key.
3. **The deploy key.** On GitHub, open views-crafdapi → Settings → Deploy keys → Add deploy key.
   Paste the printed key. Leave "write access" unticked.
4. **The code and credentials.** **ON THE BOX**, put our reader key in the environment (the line
   prompts; paste it alone), then build. The setup script reads the Appwrite coordinates from the
   views-appwrite registry and refuses to run without the key.
   ```bash
   read -rsp "reader key: " APPWRITE_DATASTORE_API_KEY; echo; export APPWRITE_DATASTORE_API_KEY
   ```
   ```bash
   RELEASE_TAG=vX.Y.Z bash /tmp/crafd-bootstrap.sh part2
   ```
5. **The service.** `bash /tmp/crafd-bootstrap.sh part3` installs it. It does not start it yet.
6. **nginx.** Create `/etc/nginx/sites-available/crafdapi`:
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
7. **The certificate,** only once step 1 works. certbot also sets up its own renewal:
   ```bash
   sudo certbot --nginx -d crafdapi.viewsforecasting.org
   ```
8. **Start it,** then check it as in section 1:
   ```bash
   sudo systemctl enable --now views-crafdapi
   ```
