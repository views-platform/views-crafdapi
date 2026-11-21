# ADR-043: The bulk files: what each one holds, when it is built, and when it is refused

**Status:** Accepted
**Date:** 2026-11-21
**Deciders:** Simon (PRIO), Claude Code
**Consulted:** the service specification (`docs/SERVICE_SPECIFICATION.md`), the partner guide
(`docs/api/README.md`)
**Informed:** partners who download the bulk files; whoever maintains this service

---

## Context

The service specification makes the bulk download the main way for partners to get data. It asks
for one ready-made Parquet file for each outcome at each level, built once for each monthly
forecast. Today that is nine files: three outcomes (state-based, non-state and one-sided violence)
at three levels (grid cell, admin 1 and country).

Before this decision the service had one bulk file. It covered admin 1 only, held all three
outcomes side by side, was built again on every download, and was sent with an empty
recorded-deaths column when the recorded deaths could not be loaded.

Three questions had to be answered to build the nine files:

1. Does a file hold the recorded deaths of past months, the forecast, or both?
2. When is a file built, and how does the service know a built file still matches the forecast it
   is serving?
3. What does a partner get when the recorded deaths cannot be loaded?

The sizes that matter, measured for state-based violence on a VIEWS forecast delivered in July
2026, on a developer computer:

| Level | Rows in one file | File size | Time to build |
|---|---|---|---|
| Grid cell | 30.7 million | 131 MB | 36 seconds |
| Admin 1 | 1.15 million | 3.2 MB | 7 seconds |
| Country | 96,000 | 0.4 MB | 8 seconds |

The grid file is large because it covers every grid cell for every month since 1990, recorded
months included. Built as a single table in memory it used more than 11 GB before it was stopped.

## Decision

### 1. One file per outcome and level, holding both the recorded months and the forecast months

Each file holds exactly the table that a slice of the same outcome and level returns when no
places, months or columns are named: every place, every recorded month and every forecast month.
The same code builds both, so the bulk file and the slices cannot disagree.

A row is either a recorded month or a forecast month, never both, and it is clear which:

- A **recorded month** (any month before the forecast starts) has the recorded number of deaths in
  the `{outcome}_actual` column. Its forecast columns are empty.
- A **forecast month** has the forecast columns (the chances of more than 0, 25, 100, 500 and 1,000
  deaths, the expected number, the severe case and the others in the data dictionary). Its
  `{outcome}_actual` column is empty.

The partner guide says this in plain words. A partner who wants only one kind of row keeps the rows
whose column of that kind is filled, or filters on the month.

### 2. A file is built the first time it is asked for, then kept on disk until a new delivery

The first download of a file after a new forecast or new recorded deaths arrive builds the file and
saves it on the server's disk. Every later download sends that saved file without building
anything. The saved files survive a restart of the service.

A saved file is named after what it was built from: the identifier the storage service gave the
forecast file and the identifier it gave the recorded-deaths file. Every partner key, and the
service's fallback to the last good forecast, loads the same delivery under the same identifiers,
so they all share one saved file. When a new forecast or new recorded deaths arrive, the next
download builds a new file under the new identifiers, and the files of every older delivery are
deleted.

If the service ever holds a forecast or recorded deaths without a known identifier, it builds the
file for that one download and deletes it afterwards. It never sends a saved file it cannot match.

Each file is written a year of months at a time, so building the grid file needs a few gigabytes of
memory instead of more than 11 GB.

### 3. Without the recorded deaths, a bulk file is refused

If the recorded deaths cannot be loaded, or do not cover the outcome asked for, the download is
refused with HTTP status 503 and the message "The recorded deaths are unavailable right now. Try
again later." A slice that reaches back into recorded months is refused in the same way.

### Also decided

- The old bulk download, `/data/forecast/bulk` (admin 1, all three outcomes in one file), is removed
  and answers 404. The new address is `/bulk/{level}/{outcome}`, for example `/bulk/admin1/sb`.
- When a slice is too large to send (HTTP status 413), its message now names the bulk file of the
  same level and outcome as the way to get the whole table.

## Reasons

**For one file holding both kinds of month.** The specification asks for one file per outcome and
level, and the partner guide already promised that the bulk file "lists every month" and that a
partner refused a too-large slice should "use the bulk download". A file with the forecast alone
would leave a partner who wants the recorded deaths for every grid cell with no single download
that gives them. Two files per outcome and level would double the number of files to explain and
maintain. Mixing the two kinds of month is safe because every row has only one of the two kinds
of value filled in, so a row can never be read as both.

**For building on first request and keeping the file.** This is the smallest change that meets
"built once per forecast". It needs no new scheduled job and no change to how the forecast is
loaded. Naming the files after the delivery identifiers, rather than after the copy of the forecast
held in memory, matters because each partner key loads its own copy of the same forecast. Matching
on the copy would rebuild the grid file every time a different partner downloaded it.

**For refusing without the recorded deaths.** An empty recorded-deaths column looks the same as "no
deaths were recorded", which is false and could mislead a partner. A refusal tells the partner to
come back, and it matches what the slices already do.

## Consequences

- **The first grid download after a new forecast takes about 40 seconds** (an estimate from the
  36-second build above; the server may differ). The service answers one request at a time, so
  while a file is being built every other request, including the uptime check, waits. If all nine
  files are asked for, this adds up to about two minutes of waiting, once per forecast (an
  estimate: three grid files at about 36 seconds each, plus six smaller files). It can be
  removed by building the files when a new forecast is loaded, which is planned separately (issue
  208 on GitHub: load the forecast when the service starts).
- **The server keeps about 135 MB of bulk files per outcome**, almost all of it the grid file, and
  only for the current delivery. That was measured for state-based violence; for all three
  outcomes it is about 400 MB if the other two are of similar size (an estimate).
- **A partner may get a refusal they did not get before.** The old bulk file was sent with an empty
  recorded-deaths column when the recorded deaths were missing. Now it is refused until they are
  back.
- **Partners who used `/data/forecast/bulk` must change the address** and download one file per
  outcome. The change is in the changelog (`docs/api/CHANGELOG.md`). No partner has a key yet, so
  no one is using the old address.
- **The file layout follows the slice layout.** A change to the slice columns changes the bulk files
  in the same way, and needs no separate decision.

## Where this lives in the code

- `src/views_crafdapi/forecast/serialize/outcome_table.py` builds the table, for slices and bulk
  files alike.
- `src/views_crafdapi/forecast/serialize/bulk_file.py` writes it as a Parquet file, a year at a time.
- `src/views_crafdapi/managers/bulk_files.py` keeps the built files and decides when to rebuild.
- `src/views_crafdapi/managers/api.py` holds the `/bulk/{level}/{outcome}` route.
