# The same defect, next door, already fixed

*Written 2026-08-22, after being asked whether views-faoapi's failure also exists here. It does.
Every fact below is read from a register entry, a commit, a `dmesg` line, a `grep` over source, or
a measurement taken in this repo — not from memory. The faoapi citations are to that repo's
`reports/technical_risk_register.md` on `fix/full-fetch-delivery-path`; nothing in that repo was
modified. The live entries here are **C-232**, **C-284**, and the three this report opens —
**C-285**, **C-286**, **C-287**.*

---

## The short version

views-faoapi was OOM-killed three times on 2026-08-14. The cause was a route that materialised an
entire dataset as Python objects before sending a byte. Over the following week faoapi measured
it, found four of its own confident claims were wrong, and fixed it.

views-crafdapi has the same defect, in the same shape of code, with larger numbers, on twenty-three
routes instead of two. It is not a new discovery — it has been registered as **C-232 (Tier 1) since
2026-08-10**. The sibling repo walked this road and we did not follow.

This report also corrects something I said about `/data/forecast/bulk` yesterday, which was wrong.

---

## What happened next door

From views-faoapi's `dmesg`, recorded at its register C-191:

```
three OOM kills of views-faoapi on 2026-08-14 at 06:45, 07:10, 07:26
each at ~23.3 GB anonymous RSS
constraint=CONSTRAINT_NONE ... global_oom
```

`CONSTRAINT_NONE` and `global_oom` mean the kernel was out of memory box-wide and chose a victim.
Not a cgroup limit — there was none in effect. **This is the same box crafdapi runs on**, and the
kernel could as easily have chosen us. Our own C-262 already records these kills; what it did not
record is *which request caused them*.

faoapi established that by measurement: `/data/{category}/latest`, cold load 10.27 GiB plus
11.4 GiB of serialization ≈ 21.7 GiB, against kills at ~23.3 GB. That closed an earlier hypothesis
— an unfiltered `subset` request — which had been recorded with confidence and was wrong.

Underneath sat two defects on one code path:

**The endpoint served rows with no data in them.** Since their store migration, `/latest` returned
`HTTP 200`, `success: true`, and rows carrying only the index — `{"month_id": 600, "priogrid_id":
100}`, zero value columns, zero geography. It ran that way in production for **roughly two
months**. Their register's line on it: *"No status code, no log line, no monitor, no client-side
exception."*

**And serialising even that emptiness was fatal.** `dataframe_to_dict` does `reset_index()` →
`to_dict(orient="records")` → `convert_numpy_types(records)`: one Python dict per row, then a full
second traversal rebuilding it, then FastAPI's encoder walking it a third time. On 28,356,996 rows
the *empty husk* cost **11.4 GiB** (432 B/row). The frame with values in it would have cost ~43 GiB.

An earlier sentence in their own register — *"the empty response is currently the only thing
preventing this endpoint from killing the service"* — had to be struck. The empty response was
itself the OOM.

**Why no timeout could have saved it.** Verified against the installed Starlette source:
`JSONResponse.__init__` does `self.body = self.render(content)`, and `__call__` emits the whole
body as a single ASGI `http.response.body` message. There is no first byte until the entire
payload exists. Their conclusion: *"no timeout adjustment can produce a working full fetch... The
remedy must change when bytes start flowing."*

---

## The same map, here

| | views-faoapi | views-crafdapi |
|---|---|---|
| `/latest` serves the index-only `.dataframe` → `200`, zero value columns | C-264 — ran ~2 months | **C-232, Tier 1, open since 2026-08-10, unfixed** |
| `dataframe_to_dict` → `reset_index` → `to_dict("records")` → `convert_numpy_types` | C-264/C-266 | same function, `managers/serialization.py:146-155` |
| Empty-husk serialization cost | 11.4 GiB | **12.9 GB** |
| Worst measured request | ~43 GiB | **34.5 GB objects, 9.9 GB JSON, 13.5 min** |
| …for what URL | `/data/historical/latest` | **`/pg/data/historical/subset` — no query string at all** |
| Routes with no row bound | 2 × `/latest`, unfiltered `subset` | **23** — 2 × `/latest`, 10 × `subset`, 10 × `hdi-map` |
| Fixed? | **Yes — `cb68bc2`, 2026-08-21** | **No** |

Every filter parameter on our subset and hdi-map routes — `time_ids`, `features`, `sample_idx`,
`entity_ids` — defaults to `None`, and `None` means *no filter* in both `parse_list_param` (whose
comment reads `# Treat empty as "all" rather than "none"`) and `_subset_mask`
(`grid_dataset.py:966-990`, which starts from `np.ones(len(self.dataframe), dtype=bool)`). The only
`limit=` anywhere in `managers/api.py` is on the `/files` *listing*, and bounds file metadata, not
rows. There is no `HTTPException(413` in the codebase.

The one capacity guard that exists — `CRAFDAPI_MAX_ASSEMBLED_BYTES` / `_guard_run_capacity`,
default 4 GiB — governs **wire-run ingest assembly only**. It has no visibility into, and no effect
on, how much of a resident dataset a single request may serialise back out.

## What faoapi's fix looks like

`cb68bc2`, which is the shape we lack:

- Both `/latest` routes rebuild the wide frame via `get_subset_dataframe()` — the same call
  `/subset` already serves — instead of returning the cached index-only frame.
- A size gate runs **on the index alone, before materialising anything**. Above
  `FAOAPI_MAX_LATEST_ROWS` (derived from a ~800 MiB peak budget) it refuses with **413**, naming
  the file channel as the alternative.
- A zero-value-column frame reaching the serializer is a **503** contract breach, not a `200`.
- Both routes go through **one** shared helper. Their commit body: *"Duplicating the fix would
  rebuild the conditions that hid the bug."*

Their stated principle is worth keeping: *"This bounds a rendering, not the data."* The full table
stays reachable — through the file channel, not as one JSON response.

`2ed9a21` then made the artifact addressable (`/provenance` emits `bucket_id`/`artifact_id`, so a
caller can find the file without out-of-band knowledge) and swapped the download route to
`FileResponse`. `2a72919` gave the client a `Range`-resumable download, because *"no single
deadline bounds a transfer of unknown length."*

## Two more of their findings transfer verbatim

Checked directly against our source, both present:

**The 294× download bug** (their C-272, ours **C-286**). `api.py:949` and `:955` return
`StreamingResponse(io.BytesIO(file_bytes), ...)`. Starlette iterates a non-async iterable through
`iterate_in_threadpool` — and **iterating a `BytesIO` yields lines**, splitting binary content
wherever `0x0A` happens to fall. faoapi measured it on a real 171 MB artifact: **680,848 chunks
averaging 251.6 bytes, 54.3 s**, against **0.185 s** for `FileResponse`. Our historical artifact is
~164 MB. We already use `FileResponse` correctly at `api.py:597` and `:993`, so this is an
inconsistency inside one file.

**Per-key cache partitioning** (their C-270/C-274, ours **C-287**). `dataset_service.py:181` keys
the warm cache `self._dataframe_cache[api_key_hash][category]`. Theirs produced a route that
returned **200 from the box and 504 to every external caller** — the operator's warm private
partition could not reproduce the consumer's cold one. That route was `/data/forecast/bulk`, and
their release note had named it the recommended path.

---

## The correction I owe

Yesterday I wrote, in C-284 and in conversation, that `/data/forecast/bulk` is *"genuinely safe —
462 KB, one file, the whole table."*

That was wrong, and wrong in a specific way: **462 KB is the response size, and the response size
has nothing to do with the memory cost.**

What is actually true:

- Bulk is structurally the best route we have. It never touches `dataframe_to_dict`, and
  `calculate_hdi_map(aggregate=True)` streams the sample store a month at a time — deliberately,
  replacing a prior ~10.9 GB materialisation.
- But it holds **both** datasets resident: the forecast run (~4 GiB, guarded) and the full
  historical grid (3.7–7.3 GB, **not** guarded), while `_actual_by_admin1`
  (`forecast/serialize/bulk_parquet.py:58-72`) reads the entire historical
  `_sample_array(target)[:, 0]` for every target.
- Measured: **7.3 G cold, 6.0 G warm.** It is not the safe outlier — it is the **largest legitimate
  consumer on the service**, and the number `MemoryMax=9G` was itself derived from.
- `force_refresh=true` is exposed on 23 routes including this one, bypasses both cache tiers, and
  re-triggers the full Appwrite download and ingest on demand.
- And I measured it warm, from the box, on the operator's key partition — the exact vantage point
  faoapi's register says proves nothing about the consumer.

So: **I cannot tell you that CRAF'd's key has ever completed a bulk request.** I can tell you the
route is the least dangerous one we expose, that its response is small and correct, and that
nothing about my verification rules out their 504.

## A third finding, which is ours alone

Every handler in `api.py` is `async def` wrapping synchronous pandas/pyarrow work. FastAPI runs
`async def` handlers on the event loop thread, the unit's `ExecStart` sets no `--workers` (so
uvicorn runs one), and `grep` finds no semaphore, lock or limiter anywhere in `src/`.

A long request therefore blocks the entire service — including the unauthenticated `/ping` route
that Better Stack polls every 3 minutes and whose failure is defined, in
`reports/ops/betterstack_monitoring.md:13`, as *"the **service** is down (outage)"*. Bulk has
measured at 25.8 s, 62.1 s, and — before the ADR-030 S7 fix — **501 s**.

This report does **not** claim it caused any August incident. Those are documented as deploy-gate
failures in `declared_vs_in_effect.md` and the evidence for that stands. What is true is that the
mechanism existed throughout and was never considered while they were being diagnosed. It is now
**C-285**.

One accident worth naming so it is not mistaken for design: because the handlers block, concurrent
heavy requests *serialise*, so memory does not multiply across callers. Making the handlers
non-blocking without adding a concurrency limit would remove that and make C-284's numbers
additive.

---

## What views-datafactory does instead

The question was how datafactory delivers full data with no RAM problem. The answer is that **it is
not an HTTP service.** There is no FastAPI anywhere in the repo. It sidesteps request/response
entirely.

It writes a static `grid.zarr` — the full 456-month × 360×720 × 75-feature grid — to a plain file
server, chunked at **~12 MB** (12 months × full spatial extent × one variable). Consumers open it
lazily with `xr.open_zarr` and fetch only the chunks they touch. Its ADR-021: *"Zarr over HTTP is
infrastructure-free... Any static file server can serve zarr. No application code, no running
process, no crashes."*

Its memory discipline was codified after **its own three OOM incidents**. ADR-031 states three
rules, of which the first is the one that matters here: *columnar in, columnar through, columnar
out* — with `.to_pylist()` and `.to_pydict()` **forbidden for datasets that may exceed 1M rows**.
`dataframe_to_dict` does exactly that, on 28.4M. ADR-037 replaced `np.full()` with
`np.lib.format.open_memmap()`, taking compile peak RSS to ~200 MB regardless of grid size.

**We have already borrowed half of this.** `forecast/ingestion/historical_stream.py`, shipped in
v0.5.1, is `open_memmap` plus row-group-at-a-time assembly — 12.205 GB → 3.940 GB locally, 16.8 G
→ 7.3 G in production. That is ADR-037's pattern applied to ingest.

The unborrowed half is the response side. What genuinely transfers: pre-slice before materialising,
build outputs columnar and convert at the boundary, never row-by-row. What does not: the zarr
export and batch assembly solve "build a 35 GB grid without OOMing the builder", a producer
problem. For a consumer service the closer analogue is `ParquetFile.iter_batches` plus a streaming
response — which datafactory does not use, because its data model is a dense grid, not a table.

---

## What faoapi got wrong, and where I am exposed

Their `3d32bc8` is titled *"the full-fetch investigation was substantially wrong"* and opens:
*"`/code-review max` and `/falsify` between them refuted four claims I had recorded with
confidence."* Each is worth holding against my own work.

| their error | my exposure |
|---|---|
| The remedy was designed against historical only, then described as solving "the full-fetch problem". Forecast is **110 files**, not one. | **Low.** C-284 measured both paths. |
| The silent forecast twin went unexamined for four days because the historical half broke loudly. | **Real.** C-284 calls our forecast twin "heavy but survivable" and moves on. It is the same C-232 silent-200 defect, just small enough to succeed — which is what made theirs invisible. |
| *"Works"* was concluded twice from the box while the logs disagreed — warm cache, operator's own key partition. | **Direct. I did this**, for bulk. See the correction above. |
| A route was recorded as *"the only working large-payload path"* and a recommendation built on it, while it was 294× slower than the trivial fix. | **Direct.** I called bulk "genuinely safe" and recommended it. |

Their generalised trigger, which this repo should adopt: *"Any 'verified working' claim made from
the box[:] ask which API key and which cache partition produced it. Reproduce with a fresh key
against a cold partition, or the verification proves nothing."*

---

## The ceiling decision

`MemoryHigh=8G` / `MemoryMax=9G` is committed to `deployment/views-crafdapi.service` and **was not
installed on 2026-08-22.** The temporary 14 G `/run` drop-in stays.

The arithmetic behind 9G assumed **one** resident historical dataset. `_dataframe_cache` is keyed
by `api_key_hash`, so *N* active keys hold up to *N* of them at ~4 GB each. Two keys plausibly
exceed 9G on their own — which would make the ceiling kill `/data/forecast/bulk`, the one endpoint
CRAF'd actually uses. Installing a ceiling below the primary consumer path would be the same class
of error the ceiling exists to prevent.

Holding is not free: without a cgroup limit, a bad request is a box-wide OOM and the kernel picks
the victim, exactly as it did to faoapi on 2026-08-14. The 14 G drop-in is loose but is not
currently killing anything.

**What would settle it**, both on the box and therefore both waiting for the operator:

1. An nginx access-log read showing whether CRAF'd's key has ever completed a `/data/forecast/bulk`
   request — status and duration.
2. A bulk request from a second key against a cold partition, with `systemctl status
   views-crafdapi | grep Memory` read after.

## What this report does not claim

- **Not** that C-285 caused any August outage. Those are documented as deploy-gate failures and
  that evidence stands.
- **Not** that bulk is broken. It is the least dangerous route we expose and its output is correct
  and byte-stable (461,991 bytes across v0.4.0 and v0.5.1). The claim retracted is only that it is
  *safe* in the memory sense.
- **Not** that CRAF'd is currently failing. Nobody has checked. That is the first item above.
- **Not** that the 7.3 G peak is stable. It is one measurement of one artifact shape.
- **Not** that faoapi's fix ports cleanly. Their `FAOAPI_MAX_LATEST_ROWS` budget was chosen against
  their grid; ours needs its own number, and C-284 requires that restoring C-232's columns and
  bounding the response land in a single change.

## Where it stands

| | state |
|---|---|
| C-232 (Tier 1, silent empty `/latest`) | open, unfixed, since 2026-08-10 |
| C-284 (no row bound on 23 routes) | open; its bulk row corrected today |
| C-285 (event-loop block, `/ping`) | opened today |
| C-286 (`BytesIO` line-streaming) | opened today |
| C-287 (per-key partition; verification and ceiling) | opened today |
| crafdapi memory ceiling | committed, **deliberately not installed** |
| views-faoapi ceiling | declared since 2026-08-09, **not in effect** — their #432 |

## Cross-references

**C-232** · **C-284** · **C-285** · **C-286** · **C-287** · **C-262** (the ceiling, and the three
OOM kills recorded without their cause) · **C-236** (caches bounded by entry count, not bytes) ·
**C-263** (resolved — the ingest half, and where ADR-037's pattern was already borrowed) ·
`reports/ops/declared_vs_in_effect.md` (the shape that put both ceilings out of effect) ·
views-faoapi C-191, C-264, C-266, C-267, C-270, C-271, C-272, C-274, and commits `cb68bc2`,
`2ed9a21`, `2a72919`, `3d32bc8` · views-faoapi **#432** · views-datafactory ADR-021, ADR-031,
ADR-037.
