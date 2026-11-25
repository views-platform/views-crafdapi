# Ready list: launching crafdapi for CRAF'd's partners

*For the maintainer. What must be true before the first partner key is handed out (#257). It
follows the definition of done agreed on 2026-11-25. Each item says when it can be done, who
does it, how to check it, and its status on the date shown. Re-check an item before ticking it.*

**The rule:** CRAF'd runs from its own server (159.69.0.79) and reads only the `crafdapi` Appwrite
project (`6af4d7fa00255ed9cc67`). Anything of CRAF'd's elsewhere (the shared project, the FAO
server) is an error to remove, never a fallback.

**Why a written list:** "You might not think you need to maintain a ready list if you are a solo
SA or the only person on the project. The opposite is true" (Limoncelli et al. 2017, *The
Practice of System and Network Administration*, ch. 19). "The first release is usually the one
that carries the highest risk; it needs careful planning" (Humble and Farley 2010, *Continuous
Delivery*, ch. 10).

**Who:** **S** = Simon (needs logins or a decision). **C** = the crafdapi session. **PC / PP / VM**
= the views-pipeline-core, views-postprocessing, views-models sessions.

**When:** **now**, **after A** (the forecast is delivered), **after B1** (the new version is on the
server), **before keys** (before the first partner key goes out).

**Status (2026-11-25):** *done*, *not done*, or *unknown*.

## The keys (in the `crafdapi` project, created by S on 2026-11-25)

| Console name | Scopes | Used by |
|---|---|---|
| `server-2026-11` | `files.read`, `rows.read` | the service itself (`.env.crafdapi-server-key`, ADR-044) |
| `publisher-2026-11` | `tables.read`, `tables.write`, `rows.read`, `rows.write`, `buckets.read`, `files.read`, `files.write` | the delivery (views-postprocessing) |
| `partner-views-2026-11` | `buckets.read`, `files.read`, `rows.read` | us: the smoke test, the notebooks, the forecast-health monitor |
| two external partners' keys | same | the first two partner organisations (which ones: Bitwarden only; this repository is public, `PARTNER_KEYS.md`) |

All five expire in November 2027 (one year). The holder of the old `crafd-caller-read` gets a sixth key at E4, created then by the same procedure. Renewal of all of them is item E5.

## A. The forecast reaches the `crafdapi` project

| # | What must be true | When | Who | How to check | Status |
|---|---|---|---|---|---|
| A1 | The `crafd` table in `file_metadata` and the bucket `crafd_bucket` exist in the `crafdapi` project with every declared column. | — | C, S typed the key | a read-only listing with `publisher-2026-11` | done (2026-11-25: 13 of 13 columns, 3 indexes, 0 rows, bucket exists) |
| A2 | views-pipeline-core moves the delivery's calls to the new Appwrite API (#603, #604) and releases. | now | PC | the release exists | not done |
| A3 | views-postprocessing writes into the `crafdapi` project with `publisher-2026-11` (#327). | after A2 | PP | its tests and a dry run | not done |
| A4 | views-models delivers `chrome_box_forecasting_20261112_210150` (1,024 draws, views-datafactory 1.18.1) into the `crafdapi` project, together with the recorded deaths built at delivery from views-datafactory ≥ 1.18.1, and re-arms `deliveries/un_crafd.py` (#721). | after A3 | VM | a forecast manifest row and a `historical` row in `crafd` | not done |

## B. The new version serves it

| # | What must be true | When | Who | How to check | Status |
|---|---|---|---|---|---|
| B0 | The holder of `crafd-caller-read` (see Bitwarden) knows it stops working at B1, when the service starts accepting only `crafdapi` keys, and that their new key comes with E4, once the data is proved (from B1 until then they have no access). | before B1 | S, C drafts the message | they have been told | not done |
| B1 | The four stories this release needs are merged (#262 data checks, #263 old key removed, #264 notebooks, #265 served-vs-delivered check). Then the release is cut (`RELEASE_RUNBOOK.md` section 0) and installed (section 1, including the unit-file step and the settings-file step, so the server's settings name the `crafdapi` project and hold no key: the section's key-line count must print `0`). It answers 503 until A4, so section 1's smoke test fails until then: that is expected and is no reason to roll back; check only `/version` now, and run the smoke test at B4. | after the four merges and B0 | C cuts, S on the server | `/version` names the new tag | not done; the server runs v0.7.0 |
| B2 | The server key `server-2026-11` is installed (section 5). Its two scopes are proven only after A4, by B3's log lines (the row listing names the manifest; a file downloads): before a delivery, "nothing delivered" and "a missing scope" look the same in the log. | with B1 | S on the server, C guides | proven at B3 | not done |
| B3 | Within 5 minutes of A4 the service notices the delivery (it checks every 5 minutes) and then loads it by itself, which also proves B2's key. | after A4 | — | the log names the manifest and a downloaded file | not done |
| B4 | The smoke test passes with `partner-views-2026-11`: `--expect-tag vX.Y.Z --expect-run chrome_box_forecasting_20261112_210150 --expect-draws 1024` (it also checks months, summaries and totals adding up across levels). | after B3 | C, S on the server | `ALL PASS` | not done |
| B5 | Served summaries match the delivered draws: `scripts/check_served_against_delivered.py` on a few delivered shards. | after B3 | C, S on the server | `ALL MATCH` | not done |
| B6 | The first load is timed and its peak memory recorded (#180); a bulk download and slices timed (#228). | after B3 | C | the service log; `systemctl status` | not done |
| B7 | The memory cap fits the dedicated server. | — | — | `MemoryMax` in the unit file | done (24 GB of 32) |

## C. It is right, seen from another machine

| # | What must be true | When | Who | How to check | Status |
|---|---|---|---|---|---|
| C1 | From another machine, notebooks 01 and 02 run against the live service with `partner-views-2026-11` and `CRAFD_EXPECTED_RUN` / `CRAFD_EXPECTED_DRAWS` set: every check in section 4 of 01 passes, and the maps in 02 (world, Nigeria, Ukraine) look right. | after B4 | S | the notebooks' own output | not done |
| C2 | The executed notebooks are stored with their outputs and released as the next PATCH version. | after C1 | C | the notebooks on GitHub show outputs | not done |

## D. It can be put back

| # | What must be true | When | Who | How to check | Status |
|---|---|---|---|---|---|
| D1 | A rollback tried once between two releases that both read the `crafdapi` project: from C2's release back to B1's, and forward again. Rolling back to v0.7.0 is not a valid test: it reads the old shared project. | after C2, before keys | S on the server, C guides | `/version` names each tag; the smoke test passes on each | not done |
| D2 | Recovery is written down and tried once: what is lost if the server dies (no forecast data; it lives in Appwrite), how long a rebuild or a restore from the Hetzner backup takes. | before keys | C writes, S tries the restore | a short section in the runbook | not done |

## E. Partners, monitors, and removing the old

E8 comes only after B0 and E2–E7; E4 only after B4, B5, C1, D1, D2, E2, E5 and E6 (B6's timings may follow the keys).

| # | What must be true | When | Who | How to check | Status |
|---|---|---|---|---|---|
| E2 | Better Stack: the liveness monitor renamed and its test alert received; the forecast-health monitor created with `partner-views-2026-11` and its test alert received (`deployment/MONITORING.md`). Check by hand that no monitor still sends an old key. | after B4 | S | alert emails received | not done |
| E3 | The old GitHub freshness job is switched off at B1 (from then on its shared-project key is refused, so it would raise a false alarm every day): disable the workflow in GitHub Actions. After E2, #249 removes it and its repository secret is deleted. | at B1; removal after E2 | S (or C on S's yes) | the workflow shows as disabled; later, no job and no secret | not done |
| E4 | The partners, and the holder from B0, receive their keys by the written procedure (`deployment/PARTNER_KEYS.md`), only after the data is proved with our own key and it can be put back. | after B4, B5, C1, D1, D2, E2, E5 and E6 | S sends, C prepares | their first successful request | not done |
| E5 | A dated reminder to renew every key in the table, and the sixth from E4, before they expire, with a named owner. | before keys | S | the reminder exists | not done |
| E6 | crafdapi stopped on the FAO server; its old deploy key removed on GitHub. | now | S, C guides | `curl --resolve crafdapi.viewsforecasting.org:443:46.224.231.85 https://crafdapi.viewsforecasting.org/ping` must not answer 200 | not done; it answers 200 |
| E7 | The registry records which project each container is in (views-appwrite #201), so the two `crafd_bucket`s can be told apart. | before E8 | the views-appwrite session | the registry edition | not done |
| E8 | After one day served from the `crafdapi` project, and only after B0 and E2–E7: the old `crafd_bucket` and its collection deleted from the shared project; `crafd-caller-read` and `crafdapi-probe` revoked, and `crafdapi-probe` removed from the server's `.env.crafdapi` (if still there) and from the local `.env`; crafdapi's cache on the FAO server deleted. | a day after B4, and after B0, E2–E7 | S, C guides | the shared project holds nothing of CRAF'd's | not done |
| E9 | The runbook describes what was actually done during the launch; any surprise becomes a line in this list. | at the end | C | the runbook and this list | not done |

## After the launch

Any surprise during the launch becomes a new line here, so the next launch does not repeat it
(Limoncelli ch. 19, "Organizational Learning"). Keep the list short: drop lines that no longer
apply (same chapter, "LRC Maintenance").
