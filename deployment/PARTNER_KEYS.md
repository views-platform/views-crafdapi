# Partner keys — issue, check, warm, send, revoke

Each CRAF'd partner organisation gets **its own Appwrite API key**, and every partner sees **the same
forecast**. A key lives in CRAF'd's own Appwrite project, `crafdapi`, and nowhere else. That project
holds only CRAF'd's data, so a partner key cannot reach FAO's data or the shelf (views-crafdapi ADR-027
§4 amendment, þing 04 D1/D7).

**Before you start, check views-crafdapi#184.** It lists the work deliberately parked until something
happens. Withdrawing a forecast from partners is one of those things.

This repository is public. **Never write a key, any part of a key, or which organisations hold keys
into it.** That record lives in the private key register (step 2).

## Before the first partner key

- **The server reads CRAF'd's project.** Its env file has `APPWRITE_CRAFD_PROJECT_ID` (the change
  is views-crafdapi a6ed22f). Step 3's check refuses to pass otherwise, and an older server answers a
  `crafdapi` key with 401.
- **The server's own key is a `crafdapi`-project key named `crafdapi-probe`,** with the three scopes
  below. It is the key `bootstrap.sh part2` asks for, the `data-freshness.yml` secret and the key
  `smoke.py` runs with (`RELEASE_RUNBOOK.md`). The name is kept from the old project so the runbooks
  stay right. It has a partner's scopes on purpose: a monitor that sees what a partner sees
  (views-faoapi#472).
- **Disk:** each key gets its own ~29 GB copy of the forecast on the server. Check with
  `df -h /home` on the box. **Above 6 partners,** views-crafdapi#183 (one shared copy for every key)
  must land first.

## 1. Create the key (Appwrite console, ~2 min)

1. Open project **`crafdapi`**, then *Overview → API keys → Create API key*.
2. **Name:** `partner-<org>-<yyyy-mm>`, the organisation plus the month it was issued. The date
   keeps an old and a new key apart during a rotation.
3. **Expiration:** 1 year.
4. **Scopes:** exactly these three, nothing else (ADR-038):
   - `buckets.read`
   - `documents.read`
   - `files.read`
5. Create, and copy the key into the password manager entry you will share from in step 5. Nowhere
   else.

## 2. Record it (private key register)

Add one row to `views_platform/reports/crafd_partner_key_register.md`. That file lives on the
operator's laptop and is in no repository; keep a copy in the team's private drive. Never add the key
itself. The partition label comes from step 3:

| Key name | Organisation | Contact | Created | Expires | Partition label | Revoked |
|---|---|---|---|---|---|---|

## 3. Check it is fenced, before anyone else has it (~1 min)

On the server, paste the key when it pauses (nothing is shown). The check reads the endpoint, the
project and the bucket from the running service's env file:

```bash
read -rs K && printf '%s' "$K" | sudo -u views-crafdapi-deploy \
  /home/views-crafdapi-deploy/views-crafdapi/.venv/bin/python \
  /home/views-crafdapi-deploy/views-crafdapi/deployment/partner_key_check.py; unset K
```

- `PASS: fenced, the key reads only 'crafd_bucket'.` followed by `Partition label: <label>`: write
  the label into the register. If the label is `UNAVAILABLE`, the service has not served yet on this
  box; run the check again after step 4.
- Anything starting `FAIL`: **do not send the key.**
  - `NOT FENCED`: revoke the key (step 6) and stop. The project holds something it should not.
  - `HTTP 401`: the key was mistyped, or it was created in another project.

## 4. Warm it, so the partner never waits (~5–10 min)

The first request with a new key loads the forecast for that key (~5 min). Make it yourself. From a
views-crafdapi checkout on the laptop, paste the key when it pauses:

```bash
read -rs APPWRITE_DATASTORE_API_KEY && export APPWRITE_DATASTORE_API_KEY
.venv/bin/python scripts/smoke.py --expect-tag "$(curl -s https://crafdapi.viewsforecasting.org/version | python3 -c 'import sys,json; print(json.load(sys.stdin)["deployed_tag"])')"
unset APPWRITE_DATASTORE_API_KEY
```

It must end `ALL PASS`. One known exception: the script checks forecast coverage at a fixed month,
559 (July 2026), and that month falls out of the window as forecasts advance. If forecast coverage is
the **only** FAIL, rerun with `--forecast-month <month_id>` set to a month inside the current
forecast. The key is warm either way: the forecast was loaded to answer the check.

## 5. Send it

- **The key:** a password-manager share or a one-time link. Never an email body or a chat thread.
- **With it:**
  - the address `https://crafdapi.viewsforecasting.org`;
  - the header `X-API-Key: <key>`;
  - a link to `docs/api/README.md`;
  - the expiry date.

## 6. Revoke a key (~5 min)

1. Console, project `crafdapi`: delete the key by its **full** name, `partner-<org>-<yyyy-mm>`. From
   that moment it is refused, and its **next request gets 401** on every route (ADR-027 §4). No
   restart is needed.
2. Free its disk copy on the server, with the label from the register:
   ```bash
   sudo -u views-crafdapi-deploy bash -c 'cd /home/views-crafdapi-deploy/views-crafdapi/apis/un_crafd/cache/datasets && ls -d <label>_* && rm -rf <label>_*'
   ```
   The `ls` shows what is about to go. Only that key's files carry its label.
3. Mark the register row revoked, with the date.

**Rotation** (an expiring or leaked key): issue the new key (steps 1–5), then revoke the old one by
its own dated name (step 6).
- **Leaked:** revoke the old key first, then issue. The partner is without access for the few minutes
  in between.
- **Expiring:** both keys work until step 6. Agree a switch date with the partner.

## What this does not do

- It does not withdraw a forecast from partners. Stopping serving one run exists
  (`APPWRITE_CRAFD_QUARANTINED_FILE_IDS`); deleting it and proving it is gone is parked in #184.
- It cannot see what a partner downloads directly from Appwrite with their key. crafdapi's own logs
  see only requests to crafdapi.
