# Technical Risk Register

| Register Info     | Details                                        |
|-------------------|------------------------------------------------|
| Project           | views-crafdapi                                 |
| Owner             | Simon Polichinel von der Maase (simmaa@prio.org) |
| Last Updated      | 2026-08-18                                     |
| Total Concerns    | 52                                             |
| Open Concerns     | 48                                             |
| Resolved Concerns | 4                                              |
| Governed by       | [ADR-010](../docs/ADRs/active/010_technical_risk_register.md) |

---

## Tier Definitions

| Tier | Severity | Description |
|------|----------|-------------|
| 1 | Critical | Silent data corruption or model output correctness risk. Requires immediate attention. |
| 2 | High | Structural fragility that will cause failures under realistic change scenarios. |
| 3 | Medium | Maintainability or coupling issues that increase cost of change. |
| 4 | Low | Code quality concerns that do not affect correctness or reliability. |

---

## ID Namespace Note (read before adding entries)

This repository is a governed clone of `views-faoapi` (ADR-031, epic #1). The **source code and ADRs
of this repository cite roughly 40 inherited concern/disagreement IDs** — `C-05`, `C-07`, `C-27`,
`C-36`, `C-50`, `C-57`, `C-59`, `C-61`–`C-65`, `C-66`, `C-68`–`C-72`, `C-74`, `C-76`, `C-77`, `C-81`,
`C-83`, `C-86`, `C-87`, `C-136`–`C-138`, `C-144`, `C-146`–`C-149`, `C-153`, `C-155`–`C-157`, `C-160`,
`C-166`–`C-173`, `C-175`, `C-177`–`C-181`, `C-231`, and `D-01`–`D-26` — whose entry bodies live in the
**upstream faoapi register that was not carried into this clone** (see `C-241`).

Consequently:

- **New IDs in this register start at `C-232`** (one above the highest inherited reference, `C-231`)
  and at `D-27`. Never allocate an ID at or below those bounds — doing so would silently re-point a
  live source-comment reference at an unrelated concern.
- Cross-references below of the form "inherited `C-xx` (body not in this register)" name a known
  upstream entry whose text is unavailable here. Resolving `C-241` is what makes those readable.

---

## Open Concerns

### C-232: `/data/{category}/latest` serves rows with no data columns behind HTTP 200

| Field | Value |
|-------|-------|
| ID | C-232 |
| Tier | 1 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When serving or documenting `/data/{category}/latest`, assert that a response body contains at least one non-index column — and when the next consumer (CRAF'd, a notebook, or `CrafdApiClient`) is pointed at these endpoints, verify the payload carries values before treating an empty result as "no data upstream". |
| Location | `src/views_crafdapi/managers/api.py:448-507`, `src/views_crafdapi/data/handlers/grid_dataset.py:200,223`, `src/views_crafdapi/managers/dataset_service.py:512`, `README.md:157-170`, `docs/api/README.md:80` |

The ADR-030 §5 representation migration hard-drops both the `pred_*` sample columns (`grid_dataset.py:200`) and the historical scalar columns (`grid_dataset.py:223`) from `.dataframe`, moving them into `_sample_store` / `_feature_store`. `DatasetService.get_latest_dataframe` still returns `cache["data"].copy()` — that now index-only frame — and the two `/latest` route handlers serialize it directly. Verified empirically on the repo's own fixture: `ForecastDataset(make_fao_df()).dataframe.shape == (8, 0)` and `dataframe_to_dict(...)[0] == {'month_id': 600, 'priogrid_id': 100}`; the historical/feature path behaves identically. **The silent path is:** column drop → index-only `.dataframe` → `cache["data"].copy()` → `dataframe_to_dict` → HTTP 200 with `success: true` and `columns: []`, with no exception, no log line, and no `/health` degradation — while `README.md:157-170` and `docs/api/README.md:80` both document these endpoints as returning "the full latest dataframe". A consumer cannot distinguish "the artifact has no rows" from "this endpoint no longer carries data". Partially self-revealing (the envelope's `shape` and `columns` fields do expose the emptiness to a caller who inspects them), which is the only argument for Tier 2; Tier 1 is assigned because the deception is server-side, partner-facing, and unsignalled — precisely the "healthy-looking wrong answer" class ADR-033 exists to eliminate. Currently unmitigated and untested: `tests/test_api_endpoints.py:172-186` asserts only `status_code == 200`, `success is True`, `"dataframe" in body["data"]` and `category == "forecast"` — never that the payload carries values.

See also inherited `C-148`/`C-153`/`D-12` (the `.dataframe` seal — body not in this register), which govern the *internal* access rule; this concern is the un-revisited *served-contract* consequence of that seal. See also `C-243` (the test suite's shape-only assertions are why this went unseen).

---

### C-233: A manifest-lookup failure serves a superseded or quarantined run with no degraded signal

| Field | Value |
|-------|-------|
| ID | C-233 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When an operator quarantines a bad run via `APPWRITE_CRAFD_QUARANTINED_FILE_IDS` and needs it to stop serving immediately, verify that `get_latest_manifest()` is actually succeeding — a metadata error makes the quarantine a no-op for up to the 4-hour warm TTL. Equally, when adding a retry or circuit-breaker around Appwrite metadata calls, decide explicitly whether `manifest_known=False` should keep serving. |
| Location | `src/views_crafdapi/managers/dataset_service.py:194-198,605-606,220-242` |

`get_latest_dataframe` looks up the run manifest once per forecast request to re-key the cache. On any exception it logs a WARNING and leaves `manifest_doc` at the `_MANIFEST_UNFETCHED` sentinel, so `_identity_ok` receives `manifest_known=False` and returns `True` unconditionally — the warm entry is served regardless of whether the manifest has changed, been replaced, or been quarantined. The comment at `:601-603` states the intent ("never drop a good entry because the metadata query flickered"), and the tier is High rather than Critical because the event is logged and the window is bounded by the 4-hour `TTLCache`. The residual risk is that the C-71 quarantine gate — the operator's only no-deploy rollback path — is defeated by exactly the Appwrite instability that makes a rollback urgent, and neither `/health` nor `/provenance` reports a degraded state during that window (`_forecast_serving_state` is set to `{"degraded": False}` on the warm hit at `:239-241`).

Part of the same root cause as `C-234`: both are deliberate transition-safety fall-throughs in an otherwise fail-visible design (ADR-033). See also inherited `C-71`, `C-166`, `C-172` (bodies not in this register).

**Extended 2026-08-14, corrected 2026-08-15 (expert-review, #60): the same missing signal has a second surface, and the first description of it was wrong.**

`/provenance/forecast` gained a store-side manifest path so it could report a wire run on a worker that has not served one (#60). On that path `forecast_serving_state()` returns `None`, so `serving_state` and `refusal_reason` are omitted — the endpoint answers 200 with a file id, timestamp and freshness verdict for the newest *manifested* run, with nothing stating whether that run is servable.

The first draft of this note claimed the silent case was "the endpoint answers 200 while `/data/forecast/latest` returns 503". **That is wrong and worth recording as an error rather than overwriting.** Every forecast 503 first sets `_forecast_serving_state`, so that response *does* carry `serving_state` and `refusal_reason`. The genuinely blind case is narrower: a worker that has **never attempted** a forecast serve, where the state was never set at all. An operator following the original wording would have hunted for a field that is present and missed the case that is blind.

A mitigating `is_served` field was added and then **removed** — it derived from `_last_forecast_provenance`, which is never cleared on refusal and is not keyed by API key, so it reported `true` for a run no longer served and `false` on a worker actively serving. A field that is wrong in both directions is worse than no field.

Location extends to `src/views_crafdapi/managers/api.py::_forecast_lineage` and `src/views_crafdapi/forecast/provenance.py`.

---

### C-234: Wire-artifact content-hash verification is skipped when the manifest omits a hash

| Field | Value |
|-------|-------|
| ID | C-234 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When the CRAF'd producer in `views-postprocessing` first emits run manifests (ADR-034 gates this — no producer exists yet), verify that every `shards[].sha256` and `sidecar.sha256` is populated before the first run is served; and when tightening the ADR-033 §7 `contract_version` gate to fail-closed, tighten this alongside it. |
| Location | `src/views_crafdapi/forecast/ingestion/wire_reader.py:150-165`, called from `src/views_crafdapi/managers/dataset_service.py:756,772` |

`verify_content_hash` treats an absent `expected_sha256` as a manifest-completeness gap rather than a refusal: it logs `"no sha256 in manifest for %s — skipping content-hash verification"` and returns. A manifest that lists shards or a sidecar without hashes therefore produces an ingest in which **no bytes are verified**, while every other gate (capability, sample-ordering, cell coverage, plausibility, rectangularity) passes and the run is served as a normal `Served` result. The ADR-013 §4.5c integrity chain is thus only as strong as the producer's stamping discipline, and ADR-034 records that the CRAF'd producer does not exist yet — so the first real stamping contract is still unwritten. This is structurally identical to the unstamped-`contract_version` pass in `forecast/contract.py:50-51`; both were justified as transition-safety, and neither records a tighten-by condition or has a test that would fail when stamping becomes guaranteed.

Part of the same root cause as `C-233` (deliberate transition-safety fall-throughs). See also inherited `C-171` (body not in this register).

---

### C-235 (RESOLVED): The aggregate reduction is a per-row Python loop while the cell-level path beside it streams

| Field | Value |
|-------|-------|
| ID | C-235 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When CRAF'd supplies the additional target list that ADR-034 §2 holds as `ADDITIONAL_TARGETS = ()`, benchmark `/gaul2/analysis/forecast/hdi-map?aggregate=true` and `/data/forecast/bulk` before shipping — the loop is linear in targets × months × units. Also check this before enabling any request-timeout shorter than the cold-load path. |
| Location | `src/views_crafdapi/data/handlers/forecast_dataset.py:533-544,562-604`, consumed by `src/views_crafdapi/forecast/serialize/bulk_parquet.py:84` and `src/views_crafdapi/managers/api.py:509-542` |

The cell-level reduction was deliberately rewritten (S6b-1, `grid_dataset.py:1254-1318`) to stream one month at a time so the full `(n_time, n_entity, S, targets)` grid — sized at ~57 GB in the code's own comment — is never materialized. The aggregate path was not given the same treatment: `ForecastDataset.calculate_hdi_map` first materializes the entire cell-level subset and joins the geo table (`:533-541`), then iterates `for idx in aggregated_df.index:` calling the vectorized `collapse()` on a **single row** per `(month, unit)` per variable (`:572-593`), each call constructing a fresh `PredictionFrame`. At gaul2 grain across a full month horizon and three targets this is tens of thousands of single-row collapses. `/data/forecast/bulk` sits on the same path via `build_bulk_table`. No benchmark, load test, or timeout bound exists for it; the only operational signal is `smoke.py`'s 600-second default per-request timeout and its warm-retry-once helper, which exists precisely because cold loads already run long.

**RESOLVED 2026-08-18 (two changes).** The reduction was batched first (PR #92): `collapse` is
vectorised over `(N, S)`, so the whole variable reduces in one call — 29,184 calls to 3 on a
4-month gaul1 query, 54.9 s to 9.1 s. That fixed the call count but not the memory: the path
still exploded the contiguous `(N, S)` store into one ndarray object per row per target to
satisfy the DataFrame interface, then stacked them back for the joint-sum.

The round trip was then removed (ADR-030 **S7 addendum**): `forecast/aggregate/reduction.py`
joint-sums arrays directly, streamed per month exactly as the cell path does. Full month range
against the delivered run, all four aggregate levels: **430.8 s → 124.3 s**, peak RSS
**10.7–13.7 GB → 4.5–6.6 GB**, every level byte-identical (gaul1 re-checked at exact float bits).
`/data/forecast/bulk` — the trigger's own benchmark — builds in **31.2 s at 10.5 GB** including
historical, against a 300 s proxy timeout; it was 501 s and returning 504 (#79).

The trigger's second clause is discharged too: the benchmark it asked for now exists as
`tests/forecast/test_aggregate_reduction_is_batched.py` (call-count bound) and
`tests/forecast/test_aggregate_path_is_array_native.py` (no explode, no re-stack). Both were
confirmed to fail against the pre-change code rather than merely passing against the new.

Still open on this path, deliberately: the historical leg's `pd.read_parquet` of 28.4M rows
(**C-169**) is now the larger remaining term — a 13.1 GB transient on a cold cache, ~6 GB of the
build's peak warm. It did not need fixing for the endpoint to come inside budget.

---

### C-236: In-memory caches are bounded by entry count, not by bytes (residual of the C-07 fix)

| Field | Value |
|-------|-------|
| ID | C-236 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When raising `workers` above 1 in the server config, or when a second API key is issued to a distinct caller, measure resident memory against the box's RAM before deploying — the current limits admit up to 50 resident datasets plus 20 raw file payloads per worker. ADR-011's own "Open Questions" flags the `maxsize=100` manager limit as an estimate. |
| Location | `src/views_crafdapi/managers/api.py:239-242`, `docs/ADRs/active/011_caching_strategy_and_eviction_policy.md:92,113-115,133` |

ADR-011 replaced unbounded dicts with `LRUCache(maxsize=100)` / `TTLCache(maxsize=50, ttl=4*3600)` / `LRUCache(maxsize=20)` and declares that this "resolves C-07" and makes memory "bounded and predictable under sustained load". The bound is on **entry count**, not bytes: `_dataframe_cache` holds `ForecastDataset` objects, and while wire runs are mmapped (S6b-1), the legacy/historical path is resident — `dataset_service.py:447-458` sizes that path at "~6-7 GB" of string columns and "~10 GB of per-cell target arrays" on the global historical grid, and `:41-42` sizes the historical dataset at "~4 GB" alongside a 16–24 GB host. Fifty such entries is not a predictable bound. `_file_cache` similarly holds raw downloaded bytes for 20 entries. The documented capacity reasoning (`CRAFDAPI_MAX_ASSEMBLED_BYTES`, `_guard_run_capacity`) governs only wire-run *assembly*, not the aggregate resident footprint of the caches. Currently masked by the single-worker, effectively single-key production deployment; ADR-011's line 133 records the limits as unvalidated estimates.

Causal residual of inherited `C-07` (unbounded in-memory caches — body not in this register), which ADR-011 marks resolved. See also `C-237` (both become acute on the same multi-worker trigger).

---

### C-237: `CrafdApiManager` installs process signal handlers that pre-empt uvicorn's graceful shutdown

| Field | Value |
|-------|-------|
| ID | C-237 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When raising `workers` above 1, or when adding any cleanup to the FastAPI `lifespan` shutdown block that must actually run, verify the handler installed at `api.py:195-196` is removed or made cooperative — `signal.signal` raises outside the main thread, and the current handler exits the process before `lifespan` teardown. |
| Location | `src/views_crafdapi/managers/api.py:195-196,163-177,1101-1106,509-542` |

`CrafdApiManager.__init__` calls `signal.signal(SIGINT/SIGTERM, self._signal_handler)` during app-factory construction, replacing the handlers uvicorn installs for graceful shutdown. `_signal_handler` runs `self._shutdown()` and then `sys.exit(0)` directly, so in-flight requests are not drained and the FastAPI `lifespan` shutdown block at `:169-177` does not run. Two concrete consequences: the `BackgroundTask(shutil.rmtree, tmpdir)` cleanup registered by `/data/forecast/bulk` (`:533-538`) can leak a per-request temp directory holding a full bulk parquet, and any future shutdown work placed in `lifespan` will be silently skipped. Under the production `Restart=always` systemd unit this path executes on every deploy and every restart. It is also a latent multi-worker blocker, since `signal.signal` raises `ValueError` when called off the main thread. Partially mitigated in that `_shutdown` is deliberately correct about *not* deleting the durable disk cache (inherited `C-66`), so a restart is cheap rather than a cold rebuild.

**Confirmed in production 2026-08-21.** The predicted traceback, from the journal on an ordinary
`systemctl restart`:

```
api.py:1116, in _signal_handler / sys.exit(0)
SystemExit: 0
During handling of the above exception, another exception occurred:
  starlette/routing.py:645, in lifespan / await receive()
asyncio.exceptions.CancelledError
```

`SystemExit` raised inside the running event loop, then `CancelledError` from starlette's
lifespan receive — the `lifespan` shutdown block never completes. The unit still reports
`Deactivated successfully`, so this is invisible unless the journal is read. Predicted from the
code on 2026-08-10; observed 11 days later, on a restart nobody was investigating it for.

See also `C-236` (both surface on the multi-worker trigger) and inherited `C-66` (body not in this register).

---

### C-238: The config layer fails silently where the environment layer fails loudly

| Field | Value |
|-------|-------|
| ID | C-238 |
| Tier | 3 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When adding any new key to `self.configs` (e.g. a served-target list, a tunable threshold, or a feature flag), verify the production default is correct *without* the config file — the deployed box never loads one. Also, when diagnosing why a config change had no effect, check whether `__load_config` returned `None` because the file was absent or because AST screening refused it. |
| Location | `src/views_crafdapi/managers/model.py:594-654,557-592`, `src/views_crafdapi/managers/api.py:187-192,1225,1234,1265-1273`, `src/views_crafdapi/managers/dataset_service.py:462-469` |

`create_app` constructs `APIPathManager("un_crafd", validate=False)` precisely because the `apis/un_crafd/` model-training tree is gitignored and absent from a clean deploy checkout (documented at `api.py:1265-1273`). All three config scripts therefore fail to load and `ModelManager.configs` is permanently `{}` in production. Where `_validate_appwrite_env` fails loud on a missing environment variable, the config layer fails silently: `historical_targets` falls back to auto-detecting "every column that is not an index or metadata column" (`dataset_service.py:465-468`), and `clear_cache` / `clear_manager_cache` are permanently false so `_maintenance`'s deliberate purge path is unreachable. Compounding this, `__load_config` returns `None` both when the file is missing and when `_validate_config_ast` refuses it as unsafe (`model.py:611-616`), so an operator cannot distinguish an absent config from a rejected one. The `host`/`port`/`workers` defaults are harmless in practice because the systemd unit invokes uvicorn directly.

**Corrected 2026-08-21 from the production journal — the conclusion holds, the stated cause does
not.** This entry says the scripts "fail to load" because the `apis/un_crafd/` tree is absent
from a clean deploy checkout. The box says otherwise; every boot logs three ERRORs:

```
ERROR - Config config_deployment.py failed AST safety check — refusing to execute
ERROR - Config config_hyperparameters.py failed AST safety check — refusing to execute
ERROR - Config config_meta.py failed AST safety check — refusing to execute
```

`__load_config` only reaches that message when `self._script_paths.get(script_name)` is truthy —
so the files **are** found, and `_validate_config_ast` is **rejecting** them (`_BLOCKED_MODULES`
includes `os`, which an ordinary config script imports). `configs == {}` in production either
way, so everything this entry concludes downstream is unaffected. What changes is the fix: the
scripts are present and refused, not missing, so "make the tree available to the deploy" would
not help.

One thing the entry got exactly right, now observable: it warned that `__load_config` returns
`None` for both an absent file and a rejected one, so "an operator cannot distinguish an absent
config from a rejected one". Only the log line distinguishes them, and it took reading the
journal for an unrelated reason to notice.

---

### C-239: Bundled GAUL shapefiles are code-dead, LFS-tracked against their ADR's rationale, and a public-release licensing blocker

| Field | Value |
|-------|-------|
| ID | C-239 |
| Tier | 3 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When flipping this repository public (the `test_falsify_path_to_public.py` gate), resolve the FAO GAUL redistribution terms before the flip — the repo-root MIT `LICENSE` would otherwise purport to relicense third-party data. Separately, when implementing the spatial joins ADR-017 anticipates, confirm the LFS-tracked files actually smudge in the deploy checkout. |
| Location | `src/views_crafdapi/shapefiles/GAUL_2024_L1/`, `src/views_crafdapi/shapefiles/GAUL_2024_L2/`, `.gitattributes`, `docs/ADRs/active/017_reference_data_in_repository.md:17,33-35,43-47`, `tests/test_falsify_path_to_public.py:36-51` |

Three compounding issues in one artifact. (a) **Code-dead:** no source path reads the bundled shapefiles — the only `gpd.read_file` call is `plotting.py:58`, which fetches Natural Earth from a URL; served geography comes from the artifact's own metadata columns and the wire sidecar. ADR-017 concedes this in its own Context ("the shapefiles themselves *will* be consumed when spatial joins are implemented") while its Decision asserts they are "required at runtime". (b) **Mechanism drift:** ADR-017's rationale is "size is negligible… Git handles this efficiently", but `.gitattributes` routes every `.shp`/`.dbf`/`.shx`/`.cpg`/`.prj` through git-LFS — so the stated and actual storage mechanisms disagree, and a checkout without LFS smudging (as here) contains only pointer stubs where the ADR promises reproducible reference data. (c) **Licensing:** a standing `xfail` records that these are third-party FAO data with no bundled license or terms, and that a public flip would redistribute them under the repo's MIT `LICENSE`. ADR-017 additionally cites `data/handlers.py:1146-1156`, a file deleted when handlers was split into a package (epic #325 S11).

The licensing half is referenced at `tests/test_falsify_path_to_public.py:10` as "the GAUL-data-redistribution blocker" with **no concern ID assigned upstream**; this entry is its first registration. See also inherited `C-77` (hardcoded credential in git history — the other public-flip blocker, body not in this register).

---

### C-240: The `forecast/` package is absent from ADR-002's topology, leaving an undeclared load-bearing import cycle

| Field | Value |
|-------|-------|
| ID | C-240 |
| Tier | 3 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When adding any re-export to `src/views_crafdapi/forecast/ingestion/__init__.py`, verify the package still imports — a single `from .wire_reader import …` there closes the `data.handlers` ↔ `forecast.ingestion` cycle and breaks boot. Amend ADR-002's layer table before the next `forecast/` sub-package is added. |
| Location | `docs/ADRs/active/002_topology_and_dependency_rules.md:38-60`, `src/views_crafdapi/forecast/` (7 sub-packages, 17 modules), `src/views_crafdapi/forecast/ingestion/wire_reader.py:49`, `src/views_crafdapi/forecast/ingestion/__init__.py`, `src/views_crafdapi/data/handlers/forecast_dataset.py:6-20` |

ADR-002 is the artifact that makes dependency violations "architectural defects", but its layer table lists only `data/`, `configs/`, `shapefiles/`, `managers/` and `wandb/` — it predates and does not describe the epic #87 decomposition that produced the repository's largest domain package. There is therefore no declared rule governing the **package-level cycle** between them: `data/handlers/forecast_dataset.py` imports five `forecast/` sub-packages, while `forecast/ingestion/wire_reader.py:49` imports `views_crafdapi.data.handlers`. No module-level cycle exists today, but only because `forecast/ingestion/__init__.py` happens to be docstring-only, so importing `forecast.ingestion.plausibility` never pulls in `wire_reader`. That accident is load-bearing and undocumented. ADR-002 also still describes the repository as "views-faoapi" throughout, as do ADR-001, ADR-030 and ADR-033 — expected for historical records, but it means a reader cannot tell ADR staleness from deliberate historical preservation.

See also `C-241` (both are governance artifacts that lag the code).

---

### C-241: ADR-010's mandated risk register was absent, leaving ~40 in-code `C-xx` references dangling

| Field | Value |
|-------|-------|
| ID | C-241 |
| Tier | 3 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When a contributor follows any inherited `C-xx` citation in a source comment (e.g. `C-155` at `grid_dataset.py:196`, `C-231` at `appwrite/manager.py:408`) to understand *why* a guard exists, they will find no entry — port the referenced upstream entries, or annotate each as inherited. Do this before the next audit adds entries that could be mistaken for the inherited ones. |
| Location | `docs/ADRs/active/010_technical_risk_register.md:15`, `reports/technical_risk_register.md` (this file, created 2026-08-10), ~40 distinct IDs cited across `src/`, `tests/` and `docs/` |

ADR-010 declares a register at `reports/technical_risk_register.md` as "a first-class governance artifact" and the single sink for all audit output. The file did not exist in this clone until this entry was written; `reports/` contained only `ops/betterstack_monitoring.md`. Meanwhile the source is unusually rich in register cross-references — `C-36`, `C-50`, `C-66`, `C-70`, `C-71`, `C-72`, `C-86`, `C-137`, `C-138`, `C-146`, `C-148`, `C-149`, `C-153`, `C-155`, `C-166`, `C-169`–`C-172`, `C-231`, `D-12`, `D-21`, `D-24` and more — and ADR-033 performs a formal register reconciliation at ratification. Every one of those citations is currently a dangling pointer. **Verified and worsened 2026-08-18.** Four of the cited IDs were resolved against views-faoapi's register, and the result is worse than "absent":

| cited in crafdapi as | what it actually is in views-faoapi | verdict |
|---|---|---|
| `C-70` joint sampling / cross-cell correlation | "aggregation assumes cross-cell sample-index alignment" — **RESOLVED 2026-06-28** | meaning ok, closed, wrong repo |
| `C-146` "cells with no GAUL code are excluded, not summed into a phantom unit" | "the pandas-to-edges parity harness is synthetic… ragged-S blind spot" — **RESOLVED 2026-06-28** | **meaning wrong** — we cite a sub-finding of its fix |
| `C-155` sample/index row alignment | "`_sample_store` decouples samples from their index" — **RESOLVED 2026-06-28** | meaning ok, closed, wrong repo |
| `C-169` "the 13 GB historical pandas load" | "historical is not shipped on the wire-contract path" | **meaning wrong** — that is delivery, not memory |

So a reader following `C-146` from `reduction.py` finds nothing here, eventually finds faoapi's, and reads a *closed* concern about something else. That is worse than an uncited comment, because it reads as authoritative provenance.

The ADR-030 S7 work (PRs #93/#97) **added six more** rather than reducing them — `C-146` at `forecast/aggregate/reduction.py:49` and `tests/forecast/test_aggregate_path_is_array_native.py:92,117`, `C-70` at `reduction.py:88` and `test_aggregate_path_is_array_native.py:124`, `C-169` at `ADR-030:177,188`. The memory concern that `C-169` was standing in for now has a local entry, **C-263**.

This is the same shape as **#58** (`ADR-017` colliding across three repos, fixed by a `vcr_`/`vmo_`/`vpp_` prefix): identifiers inherited from a parent repo and used unqualified in a clone. Whatever resolves #58 should resolve this. Tracked as **#101**.

Two concrete consequences beyond lost context: the ID namespace had to be defended by hand (see the ID Namespace Note above), and three falsification probes now **xpass** (`test_falsify_priogrid_naming.py::TestP1…`, `test_falsify_shim_diagnosis.py::TestP1…` and `::TestP4_CICDocumentsShimAsContract::test_fao_pgm_cic_does_not_guarantee_priogrid_gid`) — meaning concerns recorded as open in the C-61..C-65 cluster may in fact be closed, with no artifact in which to record that.

See also `C-240` (governance artifacts lagging the code) and `C-243` (the other stale governance document).

---

### C-242: The registry cross-check is skipped silently when `APPWRITE_REGISTRY` is unset

| Field | Value |
|-------|-------|
| ID | C-242 |
| Tier | 3 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When provisioning a new box or re-pointing an existing one at a different Appwrite project/bucket, confirm `APPWRITE_REGISTRY` resolves to a readable file on that box — otherwise the "registry is canonical" guarantee is inert and only the presence check runs. Also check whether the `APPWRITE_REGISTRY_VERSION` stamp written by `bootstrap.sh part2` matches the registry in use. |
| Location | `src/views_crafdapi/managers/api.py:68-93`, `deployment/bootstrap.sh` (registry-version stamp), `docs/ADRs/active/035_deploy_time_coordinate_provisioning.md` |

`_validate_env_against_registry` returns immediately when `APPWRITE_REGISTRY` is empty or does not point at a file (`api.py:76-78`), so the coordinate-match assertion — the mechanism that makes the platform registry canonical at runtime — is a no-op on any box where the file is absent. The code documents this as transition-safe ("if the registry is not reachable in this environment, only the presence check above runs"), and a correctly bootstrapped box is consistent because `bootstrap.sh part2` builds `~/.env.crafdapi` from the registry once. The residual is precisely the divergence ADR-035 was written to record: a hand-built box (as ADR-035 reports faoapi's production box to be) passes the eight-variable presence check while its coordinates are never compared to anything. `bootstrap.sh` stamps `APPWRITE_REGISTRY_VERSION` into the env file so the provenance is grep-able, but nothing reads that stamp back or compares it at boot.

---

### C-243: `TESTING.md` counts and module map have drifted materially from the suite

| Field | Value |
|-------|-------|
| ID | C-243 |
| Tier | 4 |
| Source | repo-assimilation (2026-08-10) |
| Trigger | When the next story changes the test count or coverage, re-run the "Verifying the counts" commands in `TESTING.md` and update the tables — and before quoting the 66% coverage baseline as evidence in a review or ADR, re-measure it. |
| Location | `TESTING.md:3,10-17,26,37,47-55` |

`TESTING.md` states "674 tests across 53 files" and a default run shape of `588 passed, 8 skipped, 53 deselected, 25 xfailed`. Measured 2026-08-10: **1,048 collected across 89 test files**, running `968 passed, 10 skipped, 48 deselected, 19 xfailed, 3 xpassed, 0 failed` in 40s. Its low-coverage table cites `managers/appwrite.py` and `prediction.py`, both of which were split into packages by epic #325 (S9/S10) and no longer exist as modules, and the 66% coverage baseline dated 2026-06-26 is now unanchored to any current measurement. The document is honest about the risk — it opens with a "Counts drift" warning and supplies verification commands — so this is disclosed staleness rather than a false claim, and no correctness or reliability property depends on it. The practical cost is that the suite's headline numbers cannot be cited as evidence without re-measuring.

This recurs inherited `C-27` (TESTING.md / register count synchronisation — body not in this register; `tests/test_falsify_merge_readiness.py:17-22` records a prior round of the same drift being fixed). See also `C-232` (the shape-only `/latest` assertions are the substantive gap behind the count drift) and `C-241`.

---

### C-244: The served output schema is 45 columns in code and 36 in every governing document

| Field | Value |
|-------|-------|
| ID | C-244 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-10), surfaced by graphify community analysis |
| Trigger | When CRAF'd supplies the real exceedance thresholds that ADR-034 §3 holds as placeholders, update `EXCEEDANCE_THRESHOLDS`, the goldens, **and** ADR-025 §4 / the data dictionary / `BulkParquetWriter.md` in the same change, and decide whether `METHODOLOGY_VERSION` must bump (ADR-023). Before that: when any contributor reconciles `schema.py` against ADR-025's self-declared "canonical column schema (36 columns)", verify which side is authoritative — following the ADR would delete nine live served columns. |
| Location | `src/views_crafdapi/forecast/serialize/schema.py:34-43,127-150`, `src/views_crafdapi/forecast/serialize/json_contract.py:90-97`, `src/views_crafdapi/methodology.py:15`, `docs/ADRs/active/025_fao_output_schema_and_naming.md:37,42,44`, `docs/CICs/BulkParquetWriter.md`, `docs/CICs/forecast_package.md`, `docs/CICs/README.md:58`, `docs/ADRs/README.md:124`, `src/views_crafdapi/client.py:138-139`. *(`docs/api/data_dictionary.md` was corrected 2026-08-11 — see the addendum; its former `:69` citation no longer resolves.)* |

`schema.bulk_columns()` returns **45** columns (6 identity + 3 series × 13) and `series_value_column_names()` returns **12** per series for the JSON API — both pinned by name in `tests/forecast/test_schema.py:88-96` and `tests/forecast/test_bulk_parquet.py:27-30`. Every governing document still says **36** (10 per series): ADR-025 §4 under the heading "Canonical column schema (36 columns)", `data_dictionary.md:69` ("6 identity + 3 series × 10 = 36 columns"), and `BulkParquetWriter.md:53`. The three ADR-034 exceedance columns per series — `s_p_gt25`, `s_p_gt100`, `s_p_gt1000` — appear **zero times** in the data dictionary and zero times in any CIC, so CRAF'd receives nine columns that no consumer-facing document defines. This is **not** limited to the bulk parquet: `json_contract.series_value_column_names` emits them on every `/{level}/analysis/{category}/hdi-map` response. Commit `449bc13` ("serve the exceedance columns … (#20, ADR-034)") shipped the code without touching ADR-025 or the data dictionary.

Three compounding facts make this Tier 2 rather than a documentation nit. (a) **The authoritative document is the wrong one.** ADR-025 declares itself the single source of truth for served column names, and ADR-003 makes declarations authoritative over inference — so a contributor who reconciles code to governance would *remove* live columns. (b) **The values are provisional.** ADR-034 is still **Proposed** ("awaiting CRAF'd/product sign-off"), and `schema.py:41-42` records the thresholds as "Placeholders … pending CRAF'd's real numbers" — placeholder cutpoints from an unratified ADR are already in the live served contract and pinned by goldens. (c) **The methodology version was not bumped.** `METHODOLOGY_VERSION` is still `crafdapi-methodology/3`, and `methodology.py:15` still describes the v3 schema as "36 columns" — yet the repo's own precedent (the v2→v3 bump for the additive `bimodality_flag`, described there as "a schema enrichment, not a value shift") is that an additive published column warrants a bump under ADR-023. Currently mitigated only in that the code side is internally consistent and test-pinned; nothing reconciles it to the governance layer.

Same family as `C-232` (served contract diverged from its documentation during a migration, undetected) but the opposite direction — there the endpoint serves *less* than documented, here *more*. See also `C-243` (governance documents lagging the code) and `C-241` (no register in which ADR-034's provisional status was being tracked).

**Partially addressed 2026-08-11 (epic #40, D8 / #48).** `docs/api/data_dictionary.md` now defines all three `s_p_gt{c}` columns with their strict-`>` and NaN semantics, marks `s_actual` bulk-parquet-only, records that the thresholds are ADR-034 placeholders whose confirmation will **rename** the columns, and corrects its bulk blockquote to 45. The headline bullet no longer claims "not probabilities" while three served columns are probabilities — the one defect here with a plausible path to a wrong humanitarian read.

*Evidence note, corrected:* an earlier draft of this addendum cited the golden `served_hdi_map.json` as proving `s_actual` is absent from the JSON path. **That artifact cannot support the claim** — it is built from synthetic series-less vars (`conftest.py:41`), so its 36 keys are 12 identity/geo (including a stray `index` key the real API never emits) plus 2 *fixture* series × 12, unrelated to the real 3 × 12; the real `pg` response is 47 keys. No golden contains an `sb`/`ns`/`os` key, so none exercises `to_consumer_columns` at all. The claim is nonetheless true, on the correct evidence: `json_contract.series_value_column_names` returns 12 names and `actual` is not among them.

**Still open — this entry does not close.** The title and the pre-2026-08-11 body above describe the state at registration; the *data dictionary* half is now done, everything else stands:
- **12 stale sites remain** (≈27 individual lines): `ADR-025 §4` (`:37,42,44`), `schema.py:5,88`, `methodology.py:15`, `client.py:138-139`, `json_contract.py:84-87`, `docs/ADRs/README.md:124`, `BulkParquetWriter.md`, `forecast_package.md`, `ForecastDataset.md`, `CICs/README.md:58` (which says **33**, a *third* total), `test_bulk_parquet.py:1`, `test_consumer_naming.py:80`. The repo therefore carries three different totals — 33, 36 and 45.
- **The "raw fatality counts" absolute survives in five higher-traffic places** the dictionary fix did not reach: `docs/api/README.md:19` (the primary HTTP-consumer document, which never mentions `p_gt` anywhere), `notebooks/README.md:14`, `notebooks/01_quickstart.ipynb:23`, `notebooks/03_offline_demo.ipynb:58`, and `forecast/serialize/bulk_parquet.py:16` — the last self-contradictory, since the same docstring lists `s_p_gt{25,100,1000}` eight lines earlier. Notebooks 01 and 03 render `hdi_map` frames that now carry those columns under that header.
- **ADR-025 is the dangerous one and is untouched.** It declares itself canonical and ADR-003 makes declarations authoritative, so a contributor reconciling code to it would still *delete* nine live served columns. The §4a amendment with an explicit "MUST NOT remove" imperative is unwritten.
- **ADR-034's own served-column plan (`:107-118`) is a different 36** — identity + map + HDI + exceedance, omitting `severe_scenario`, `bimodality_flag` and `actual`. A producer implementing the newest ADR would ship a table missing three per-series columns — and that does **not** surface as an error: `bulk_parquet.py:103-106` fills any absent schema column with `pd.NA` before the final projection, so the result is a normal HTTP 200 download with silently all-null columns (the same mechanism as `C-248`). *(An earlier draft claimed `bulk_parquet.py:107` would raise `KeyError`; verified unreachable.)*
- **No recurrence guard exists.** This drift class already recurred once here (`C-27` → `C-243`), so a one-shot documentation fix will rot. A guard deriving the expected set from `schema.py` is the durable fix; `C-246` records that the JSON contract has no test at all.
- The `METHODOLOGY_VERSION` question remains undecided: ADR-023 §1 says an additive column needs no bump, `methodology.py`'s own v2→v3 precedent says it does, and `api/README.md:169` says column changes are ADR-023-governed. Three documents, three answers. *(Previously cited here as "C-247" — an unallocated ID at the time, which is the dangling-pointer defect `C-241` exists to track. It is deliberately left unregistered rather than given a number, because it is a **disagreement**, not a concern: it belongs in the Disagreements section as `D-27` when someone rules on it.)*

---

### C-245: `/analysis/historical/hdi-map` serves probability columns under raw-count names

| Field | Value |
|-------|-------|
| ID | C-245 |
| Tier | 1 |
| Source | expert-review (2026-08-11) — found while documenting the served columns, epic #40 / D8 (#48) |
| Trigger | Before pointing any consumer at a historical `hdi-map` route, decide whether that route should exist at all. **Do not simply extend the consumer rename to it** — observed data is `S=1`, so the reduction collapses every HDI bound onto the point value (`sb_hdi95_lower == sb_hdi95_upper == sb_map`), making a zero-width interval indistinguishable from a near-certain forecast. Retiring the route, or serving observed values only via `/data/historical/subset`, is the likelier fix. |
| Location | `src/views_crafdapi/managers/api.py:740-741`, `src/views_crafdapi/forecast/serialize/json_contract.py:162-172`, `src/views_crafdapi/data/handlers/grid_dataset.py:1230` |

**The route is broken at its documented defaults.** A real historical artifact is built with the configured `historical_targets` (non-`pred_` names, `dataset_service.py:462-469`), so `is_prediction` is False and `calculate_hdi_map` raises at `grid_dataset.py:1230` — **HTTP 500 at every level** for `aggregate=false`, which is the default. Verified end-to-end on a real-shaped historical dataset.

With `aggregate=true` it does respond, and that is where the misread lives. The ADR-025 `sb`/`ns`/`os` rename is applied **only** when `category == "forecast"` (`api.py:740`) — "historical keeps its own columns" — so since ADR-034 added exceedance to the shared reduction, the route emits **`lr_ged_sb_p_gt25`**: a posterior probability in `[0,1]` wearing an internal `lr_ged_` prefix. Verified: the reduction emits `['lr_ged_sb_p_gt25', ...]` under the historical target stem.

*Correction, 2026-08-11:* this entry first named the column `pred_lr_ged_sb_p_gt25`. That name came from `tests/test_api_endpoints.py:33-42`, which seeds the historical cache slot with a `pred_*` dataset — the very fixture this entry calls "a forecast wearing historical's name". No served artifact can produce both the `pred_`-prefixed name and the 500, because they arise from opposite values of `is_prediction`. The wrong name briefly propagated into `data_dictionary.md`; corrected there too.

That collides head-on with the consumer documentation, which instructs analysts to **ignore** those prefixes because "`lr_ged_sb` is a raw count, *not* a log-rate" (`docs/api/data_dictionary.md`, ADR-024 §1-2). An analyst who follows the documentation reads `p_gt25 = 1.0` — *certainty of exceeding 25 fatalities* — as **1 fatality**. Tier 1: a silent, plausible misread of a UN-facing humanitarian quantity, with no error signal.

Compounding: the same route with `aggregate=false` returns **HTTP 500** (`grid_dataset.py:1230`, "HDI and MAP calculation only valid for prediction dataframes"). CI misses both because `tests/test_api_endpoints.py:33-42` seeds the historical cache slot with a `pred_*` dataset, so the test fixture is a forecast wearing historical's name.

See also `C-244` (the documentation half) and `C-246` (nothing tests the JSON column set).

---

### C-246: the JSON per-series column contract has no test

| Field | Value |
|-------|-------|
| ID | C-246 |
| Tier | 2 |
| Source | expert-review (2026-08-11) — found while documenting the served columns, epic #40 / D8 (#48) |
| Trigger | When adding, removing or renaming a served JSON value column — including when CRAF'd confirms the exceedance thresholds and the `p_gt{c}` columns are renamed — add the exact-set assertion first, or the change ships green. |
| Location | `src/views_crafdapi/forecast/serialize/json_contract.py:90-97`, `tests/forecast/test_consumer_naming.py:80-103`, `tests/forecast/test_schema.py:88-96` |

`json_contract.series_value_column_names` — the 12-column JSON contract now documented to CRAF'd as a served surface — is referenced **nowhere** in `tests/`. The two tests that look like they pin it (`test_schema.py:88-96`, `test_bulk_parquet.py:27-30`) pin the **bulk** 45, a different product. The only endpoint-level naming test, `test_consumer_naming.py:80-103`, iterates a hardcoded **9-item pre-ADR-034 list** and asserts *subset* membership (`assert tmpl.format(s=s) in cols`), despite being named `..._has_the_exact_consumer_value_columns`.

Consequence, stated precisely: the *values* are guarded — patching out the exceedance emission fails `test_exceedance.py::TestExceedanceReachesServedColumns` and `test_served_output_golden.py` (verified: 2 failed, 1005 passed). What is unguarded is the **name-list contract** itself: deleting the `exceed_col` line from `series_value_column_names` alone leaves the suite green, because nothing asserts what that function returns. The fix is small — derive the expectation from it and assert set equality in `test_consumer_naming.py` — and was scoped out of D8 (docs-only) deliberately.

*Correction, 2026-08-11:* this entry first claimed a deletion "passes the entire suite green", which contradicted C-244's own note that the thresholds are golden-pinned. Only the narrower name-list edit is green.

---

### C-247: `severe_scenario` collapses to a single draw when the consumer subsets samples

| Field | Value |
|-------|-------|
| ID | C-247 |
| Tier | 2 |
| Source | expert-review (2026-08-11) — found while documenting the served columns, epic #40 / D8 (#48) |
| Trigger | When a consumer reports `severe_scenario` equal to a MAP or to an obvious single draw, check their `sample_idx`. Before advertising `sample_idx` to CRAF'd, decide whether to enforce a minimum draw count. |
| Location | `src/views_crafdapi/forecast/summarize/severe.py:47`, `src/views_crafdapi/managers/api.py:701,728`, `src/views_crafdapi/data/handlers/grid_dataset.py:1281-1284` |

`expected_shortfall` computes `k = max(1, int(np.ceil(tail * s)))`, so for any `S <= 20` the "mean of the worst 5% of draws" is **the single worst draw** — precisely the raw sample maximum the statistic was designed to avoid (ADR-025 documents it as "deliberately **not** the raw sample maximum, which is high-variance and non-reproducible").

`sample_idx` is a **public query parameter**, and `grid_dataset.py:1281-1284` validates only the index *range* — no minimum count, no de-duplication. So `GET /pg/analysis/forecast/hdi-map?sample_idx=0` serves one draw under a column documented as a worst-5% mean, alongside `p_gt{c}` quantized to `{0, 1}`. Verified: `sample_idx=0` → severe equals that draw; `k` first reaches 2 at `S=21`.

---

### C-248: a failed historical fetch silently nulls every `*_actual` in the bulk parquet

| Field | Value |
|-------|-------|
| ID | C-248 |
| Tier | 2 |
| Source | expert-review (2026-08-11) — found while documenting the served columns, epic #40 / D8 (#48) |
| Trigger | When a consumer reports "no observations exist for these months", check the server log for the historical-fetch warning before believing it. Before CRAF'd relies on the bulk product for forecast-vs-actual skill scoring, give this an in-band signal. |
| Location | `src/views_crafdapi/managers/api.py:522-528`, `src/views_crafdapi/forecast/serialize/bulk_parquet.py:98-107` |

`/data/forecast/bulk` catches a historical-fetch failure, logs a **server-side warning only**, and calls `write_bulk_parquet(out, forecast_ds, None)`; the writer then fills all three `*_actual` columns with `pd.NA`. The download succeeds with HTTP 200 and 45 columns.

A forecast-vs-actual skill check silently scores against nothing — and **nothing raises**. Verified in the repo venv (pandas 2.3.3): `build_bulk_table(fc_ds, None)` returns 45 columns, `sb_actual` dtype `object`, `.mean()` → `nan`, **`.sum()` → `0`**, and the dtype survives the parquet round-trip. A skill script gets a silent zero, not an exception.

The dtype is, however, an **in-band discriminator**: a real join yields `float32`, a failed one `object`. Note the *expected* case is also all-null — every forecast month legitimately has no observation — so all-null alone means nothing; only the dtype separates the two.

*Correction, 2026-08-11:* this entry first claimed `.mean()` raises. It does not. The fabricated fail-loud consolation made the concern look self-revealing when it is silent, and any regression test written from it (`pytest.raises`) would fail immediately.

---


### C-250: README seam-contract pin is stale (`platform-001-v1.2.0`) against the live `appwrite-seam-v1.5.2`

| Field | Value |
|-------|-------|
| ID | C-250 |
| Tier | 4 |
| Source | /code-review max on S1 (seam-contract D2, epic views-faoapi#383) (2026-08-12) |
| Trigger | The next reader follows `README.md:17`'s pinned URL / believes `platform-001-v1.2.0` is the current seam-contract edition when repointing or onboarding. |
| Location | `README.md:17-19`, `docs/ADRs/active/035_deploy_time_coordinate_provisioning.md:25-26,138-141` |

The README pins the Appwrite Seam Contract at `platform-001-v1.2.0` and describes a "v1.3.0 rename untagged" — stale: views-appwrite now publishes `appwrite-seam-v1.5.x` tags (current `v1.5.2`) and the contract file was renamed `PLATFORM-001` → "The Appwrite Seam Contract". S1 added a D2 binding note directly below (correctly citing `v1.5.2`), so the two now visibly disagree. **Doc-drift, no runtime effect** — the D2 binding pins `v1.5.2` in code (`seam_contract.REGISTRY_PIN_TAG`), not the README. crafd's equivalent of views-faoapi#340; deliberately out of S1 scope (contract-version tracking is its own concern). Named fix: a crafd doc-hygiene pass refreshing the README seam pin to the current published tag + renamed contract, mirroring faoapi#340.

**Extended 2026-08-18 (graphify): the pin is stated in three places at three different editions, and one of them is an ADR.** The graph's citation extraction put `README.md`, `seam_contract.py` and ADR-035 on the same `appwrite-seam` node and they disagree: the README says `platform-001-v1.2.0`, **ADR-035 says `appwrite-seam-v1.4.4`** (four pinned URLs — `:25-26` in Context, `:138-141` in the evidence block), and the code says `appwrite-seam-v1.5.2`. The original entry framed this as README-vs-code doc drift with "no runtime effect", which held while the README was the only stale copy. ADR-035 is different in kind: under ADR-000 an ADR is the source of truth when code and ADRs disagree, so a reader resolving the contradiction *by the rules* lands on `v1.4.4` — an edition that predates the `UNCRAFD_CONSUMER_DOCUMENT_NAME` row the binding test requires (that row exists only from `v1.5.2`, per `seam_contract.py:34` and the resolved C-249). Still no runtime effect today, because `REGISTRY_PIN_TAG` is what CI reads. Tier stays 4 on that basis; the fix now has to cover ADR-035, not just the README.

---

## Disagreements

(No disagreements registered yet. New IDs start at `D-27` — see the ID Namespace Note.)

---

### C-251: The forecast-lineage precedence rule is encoded three times, in two different orders

| Field | Value |
|-------|-------|
| ID | C-251 |
| Tier | 3 — no wrong answer today: `/health` and `/provenance` were reconciled to the same order by #60. The cost is that one rule now lives in three places with nothing holding them equal, and they demonstrably diverged once already. |
| Source | expert-review (2026-08-14), #60 |
| Trigger | When the forecast freshness anchor moves from the manifest document's `$createdAt` to the run manifest's own completion time (which `api.py`'s own comment already calls it), change `/health` and `forecast/provenance.py` in the same commit — nothing links them, and `/health` reads the raw store key while the lineage path reads it through `_provenance_from`. |
| Location | `src/views_crafdapi/managers/api.py::get_health` (inline served → manifest → legacy chain), `src/views_crafdapi/managers/api.py::get_provenance` (the call site), `src/views_crafdapi/forecast/provenance.py::_base_record` (the declared rule) |

"Which source answers *what forecast is live?*" is answered in three places. `/health` walks served → manifest → legacy inline. `/provenance` delegates to `forecast/provenance.py`, which declares manifest > stored. Before #60 the `/provenance` call site gated the manifest lookup on `stored is None`, encoding served → legacy → manifest — the opposite store-side order — and an empirical probe produced two endpoints giving **opposite freshness verdicts about the same store in the same process**: `/health` reporting the manifest's timestamp and `is_stale: false`, `/provenance` reporting a superseded legacy artifact and `is_stale: true`.

#60 fixed the `/provenance` side by removing the gate, so the two now agree. What was not fixed is that they agree *by coincidence of two independent implementations*. `/health` also reads the timestamp as a raw store key (`m.get("$createdAt")`) while the lineage path reads it through `_provenance_from`'s defaulting — so the same value arrives by two routes with different failure behaviour.

The correct shape is for `/health` to call the same resolver and delete its inline chain. That was deliberately not done in #60 to keep the change scoped to the reported 404.

Same family as `C-243` (two descriptions of one thing drifting apart), and the structural cause of the bug `C-255` records.

---

### C-252: On the manifest-only lineage path, `source` is unattributable — the exact field C-86 exists to expose

| Field | Value |
|-------|-------|
| ID | C-252 |
| Tier | 3 — honest-absent, not wrong: the endpoint reports `"unknown"` and `served: false` rather than guessing. But it is the normal crafd response on a cold worker, and `source` is the field whose whole purpose is making a silent upstream switch visible. |
| Source | expert-review (2026-08-14), #60 |
| Trigger | When a second ensemble is delivered to `crafd_bucket` and an operator needs to confirm *which* one is live from `/provenance/forecast` alone, check whether the answer still depends on whether that worker has served a forecast yet. If it does, either stamp `source` on the manifest producer-side (views-postprocessing `contract/wire/sink.py`) or resolve the ensemble from the shard header at report time. |
| Location | `src/views_crafdapi/managers/prediction/manager.py::_provenance_from`, `src/views_crafdapi/forecast/provenance.py` (module docstring records the limit) |

`_provenance_from` derives `source` from `doc.get("source") or doc.get("pipeline") or "unknown"`. No producer stamps either field on a manifest: views-postprocessing's wire sink uploads `{name, category, loa, filename, type, targets, description}`, and `PredictionMetadata.to_dict()` emits exactly `{loa, name, type, targets, category, description}`. The ensemble identity lives in the shard header's arrow KV blob at `metadata.provenance.ensemble` and is read only when a run is actually ingested.

So on a worker that has not yet served a forecast — which is every worker after a restart, and the shape `crafd_bucket` presents by default because it is greenfield and has no legacy record to fall back to — `/provenance/forecast` answers 200 with `source: "unknown"`. It becomes attributable the moment that worker serves the run. The value is therefore **worker-state-dependent for the same store and the same HTTP call**, which is the same defect shape as `C-254` records for `run_id`.

Verified 2026-08-14 against production: `/provenance/historical` returned `"source": "unknown"` on a real delivered artifact.

Cross-refs `C-254` (the sibling schema-variance defect on the same path), `C-251` (why two paths answer differently).

---

### C-253: The lineage route makes two uncached metadata round trips, the first guaranteed empty, and the second sorts client-side

| Field | Value |
|-------|-------|
| ID | C-253 |
| Tier | 3 — latency and Appwrite request cost on an endpoint designed to be the *cheap* alternative to fetching the dataframe. No correctness impact. |
| Source | expert-review (2026-08-14), #60 |
| Trigger | When `/provenance/forecast` is added to a monitor's polling loop (the Better Stack checks in `reports/ops/betterstack_monitoring.md` currently poll `/ping` only), measure the per-call round trips first — at one poll per 3 minutes this is two full metadata queries per poll, one of which cannot match anything. |
| Location | `src/views_crafdapi/managers/api.py::get_provenance`, `src/views_crafdapi/managers/prediction/manager.py::get_predictions_by_metadata`, `src/views_crafdapi/managers/appwrite/metadata.py` (the paging call) |

#60 made the route fetch both store sources unconditionally, because gating one on the other's absence silently inverts the precedence (`C-251`). The correctness argument is sound; the cost is that on `crafd_bucket` the legacy query is pinned by the §11.4 guard to `type="model"` and therefore matches **nothing on 100% of requests** — it exists purely to be ranked below the manifest.

Underneath, `get_predictions_by_metadata` pages the full result set with `Query.limit(DEFAULT_PAGE_LIMIT)` + `offset` and no `order_desc`, then sorts client-side and takes `docs[0]`. A probe with 250 matching documents produced 3 HTTP round trips returning 250 documents to select one. There is no cache in front of either query.

Cheaper without changing the precedence: push `order_desc('$createdAt') + limit(1)` server-side, and skip the legacy query when a manifest was found (ranking is unaffected — the manifest already wins). Same family as `C-235`: a correct implementation whose cost was never measured against the shape of the data.

---

### C-254: `run_id` appears in the lineage response only on workers that have served a forecast

| Field | Value |
|-------|-------|
| ID | C-254 |
| Tier | 3 — a documented field that is present or absent depending on process state, not on the data. Consumers in this repo hedge with `.get()`, so it is a contract/doc mismatch rather than a live crash. |
| Source | expert-review (2026-08-14), #60 |
| Trigger | When a notebook, monitor or partner script is written to key off `run_id` from `/provenance/forecast` — as `client.py`'s docstring invites — confirm it survives an API restart. It will `KeyError` on the first call to a fresh worker. |
| Location | `src/views_crafdapi/forecast/provenance.py` (`_SERVED_IDENTITY_KEYS`), `src/views_crafdapi/managers/prediction/metadata.py::PredictionProvenance.to_dict`, `src/views_crafdapi/client.py` (documents the field) |

`run_id` is overlaid from the served record but is **not** a key of `PredictionProvenance.to_dict()`. So the response carries it once a worker has served the run and omits it entirely otherwise — for the same run, the same store and the same HTTP call, changing across a restart. `client.py` documents the return as "the run id / filename, creation time, methodology version, freshness verdict", so the field is advertised.

The manifest-only path is exactly where `run_id` is most useful (it is the only identifier a cold worker can offer beyond a file id) and exactly where it is absent. It is recoverable from the manifest filename — `<run_id>__manifest.json` — but nothing extracts it, deliberately: parsing identity out of a filename is the kind of inference ADR-003 forbids, so the fix belongs producer-side or in an explicit metadata field.

Same family as `C-244` (the served contract differs from the document that describes it), and the sibling of `C-252` on the same path.

---

### C-255: The §11.4 legacy pin makes every type-less category query silently empty on a wire-only bucket — four callers and the historical path still depend on it

| Field | Value |
|-------|-------|
| ID | C-255 |
| Tier | 2 — this is the root cause of #60, already proven to produce a confident wrong answer once. The remaining callers are latent only because the historical leg has not yet moved to the wire contract; when it does, the failure is silent by construction. |
| Source | expert-review (2026-08-14), #60 |
| Trigger | When the historical artifact moves onto the wire contract (ADR-033's plan; `C-169`), re-check every type-less `{"category": ...}` query **before** the first delivery — `/data/historical/latest`, the C-172 invalidation lookup, `get_latest_file_id`, `get_latest_file_metadata`, `download_latest_file`, and `/provenance/historical`. Each returns "nothing found" against a wire-only bucket while the artifact is present. |
| Location | `src/views_crafdapi/managers/prediction/manager.py::get_predictions_by_metadata` (the guard), `src/views_crafdapi/managers/prediction/constants.py` (the "inert here" claim), `src/views_crafdapi/managers/dataset_service.py` (historical selection, C-172 invalidation), `src/views_crafdapi/managers/api.py::get_provenance` (historical branch) |

The ADR-013 §11.4 transition guard pins any query naming a `category` and no `type` to `type="model"`. It is **correct** and must not be relaxed: it exists so a legacy selection can never resolve a wire artifact as "the dataset".

Its consequence is category-agnostic and was not reasoned through. `crafd_bucket` is greenfield on the wire contract and holds **no** `type="model"` documents, so a type-less category query matches nothing — not "the legacy answer", but *no answer* — and every caller that reads that as "absent" concludes the artifact does not exist. #60 was one instance: `/provenance/forecast` returned 404 for a run the API was serving correctly, verified in production 2026-08-14 while `/health` reported the same forecast as fresh and `smoke.py` returned 1,030 IDN cells.

#60 fixed the forecast lineage path only. The same shape remains in the historical selection path, the C-172 cache-invalidation lookup, three file-level helpers, and `/provenance/historical`. These are latent **only** because historical is currently delivered as `type="model"` — which happens to match the pin. The named trigger above is when that stops being true.

The compounding hazard is where the failure surfaces: a historical lookup that returns nothing is swallowed by the bulk-parquet path and ships `s_actual` as all-`NaN` behind HTTP 200, which is `C-248` exactly. Selection returning empty and the consumer being unable to tell are two halves of one silent-null failure.

`constants.py` still records that the guard is *"inert here"* because crafd has no pre-wire documents. That belief is what produced #60: the guard is inert for **selection**, which is what it was written for, and precisely **not** inert for a type-less lineage or metadata query, where having no legacy documents is exactly what guarantees the empty result. The correction currently lives only in `forecast/provenance.py`'s docstring, which a `constants.py` reader has no reason to open.

Cross-refs `C-248` (the silent-null consumer half), `C-251` (the duplicated precedence that let two endpoints disagree about this), `C-169`/`C-172` (inherited; bodies not in this register).

---

---


### C-256: The Appwrite key has three legitimate homes and every document that names one has named a different one

| Field | Value |
|-------|-------|
| ID | C-256 |
| Tier | 3 — no correctness impact; the cost is that a new user (or an agent) following the docs cannot authenticate, and the failure surfaces as an Appwrite *scope* error that names neither a file nor a key. |
| Source | review-diff (2026-08-15), during D7 (#47) |
| Trigger | When the CRAF'd serve key is rotated (it expires **2026-11-17**, C-84), update all three homes in the same change — the deployed `.env.crafdapi`, the operator's export, and any local `.env` — and re-check that `README.md`'s table still matches `deployment/RELEASE_RUNBOOK.md`. |
| Location | `README.md` (Configuration), `deployment/RELEASE_RUNBOOK.md`, `notebooks/README.md`, `.env.example`, `views-models/.env` (a different repo, same secret) |

The same secret legitimately lives in three places for three different consumers, and nothing holds the documentation of them consistent:

1. **Local dev / notebooks** — `.env` at this repo's root, found by `load_dotenv()` and `pyprojroot.here()`.
2. **Deployed** — coordinates come from the views-appwrite registry into `.env.crafdapi`; the secret is exported into the environment by the operator. `RELEASE_RUNBOOK.md` is explicit: *"never from anyone's `.env`"*.
3. **The postprocessor** — `views-models/.env`, which is what actually delivers into the bucket.

`README.md` said the API's `.env` was "located in `views-models`" — option 3, which is the one consumer it is *not*. That is **#28**, open since before this epic. The D7 fix for it initially replaced the wrong answer with a different wrong answer (asserting the root `.env` unconditionally, which is false for the deployed service), because the claim was inferred from `pyprojroot.here()` without reading `RELEASE_RUNBOOK.md`. Both drafts were single-location claims about a three-location fact.

The consequence is not theoretical: an absent key fails as `401 … User (role: guests) missing scopes (["buckets.read"])` and a placeholder key as `401 … not authorized`. Neither message mentions a key, a file, or `.env`. Two full notebook runs were spent rediscovering this during D7 before the notebooks were given guards that name the file and the fix.

Mitigated in this change: `README.md` now carries a table distinguishing all three, and `01`/`02` fail at their first cell with the remedy. What is **not** mitigated is that nothing enforces the table against `RELEASE_RUNBOOK.md` — they can drift again, and `tests/test_doc_accuracy.py`'s checks do not cover it.

**Recurred 2026-08-18**, during the v0.4.0 deploy, costing four exchanges mid-release. Three distinct failures in one sitting: a placeholder pasted verbatim as the key (`401 … not authorized`); `APPWRITE_DATASTORE_API_KEY not set` after `read` swallowed the following line; and the operator asking, correctly, why the name in their password manager matched none of the names used in the docs or by the agent guiding them. One secret had been referred to as `APPWRITE_DATASTORE_API_KEY`, "CRAF'd caller key", "CRAF'd caller/read-scoped key" and "Appwrite caller key — CRAF'd" within a single session.

Mitigated further in this change: `RELEASE_RUNBOOK.md` now carries a table mapping **password-manager entry name → env var → what uses it**, names the password manager as the source of truth, and gives a `curl /health` check that verifies a key returns 200 before it is pasted anywhere. What is still not enforced is agreement between that table and `README.md`.

Cross-refs **C-84** (both keys expire 2026-11-17 — the named trigger), **#28**.

---


### C-257: The `un_fao` → `un_crafd` launcher clone is the second partner copy, and nothing links them

| Field | Value |
|-------|-------|
| ID | C-257 |
| Tier | 3 — WET-before-DRY at n=2 is a defensible choice and nothing is shipping wrong. What is missing is the trigger that stops "today" lasting indefinitely, and any link that would make a fix to one prompt a look at the other. |
| Source | manual (2026-08-15), D9 close-out of epic #40 |
| Trigger | **Rule of Three, and a second condition that has already nearly fired.** Extract when either: (a) a **third** partner postprocessor is added, or (b) **the first bug is hand-patched in both launchers**. (b) came within one commit of firing during this delivery — views-models#385 (the pin is not applied) and #392 (a failed install does not stop the run) were both fixed in `tools/launcher/postprocessor.sh`, which `un_fao` and `un_crafd` already share. Had that body not been extracted first, both fixes would have needed hand-applying twice. |
| Owner | Whoever adds the third partner, or takes the first launcher bug after this one. |
| Location | `views-models/postprocessors/un_crafd/` and `views-models/postprocessors/un_fao/` (a different repo; recorded here because epic #40 created the duplicate) |

`un_crafd` was created by cloning `un_fao`'s directory skeleton. The partition was made deliberately and is the right one **today**: `configs/*` and `main.py` vary by **partner**, so copying is correct; `run.sh` varies by **delivery protocol**, so it was extracted into the shared `tools/launcher/postprocessor.sh` rather than duplicated (un_fao's launcher went 136 → 21 lines). That extraction is why views-models#385/#392 were single fixes.

What remains duplicated is the per-partner surface: two `config_meta.py`, two `config_queryset.py`, two `main.py`, two `requirements.txt`. Those are *supposed* to differ — but `requirements.txt` is currently **byte-identical** in both, which is how the `views-datafactory` floor conflict (#386) hit both partners at once and was diagnosed twice.

views-postprocessing registered the first instance of this pattern — its own partner-manager clone — as **C-33**, with the same Rule-of-Three trigger. This is the second. Registering it is the point: WET at n=2 is a choice; *unregistered* duplication is drift that nobody decided on.

Cross-refs **C-33** (views-postprocessing, the first instance), views-models **#385**, **#392**, **#386**, **#333** (the story that created the clone).

---

## Resolved Concerns

### C-249: `seam_contract.declared_value` raises a bare `KeyError` if the registry pin is lowered below the row's first edition

| Field | Value |
|-------|-------|
| ID | C-249 |
| Tier | 4 |
| Source | /code-review max on S1 (seam-contract D2, epic views-faoapi#383) (2026-08-12) |
| Trigger | A contributor lowers `REGISTRY_PIN_TAG` below `appwrite-seam-v1.5.2` (the edition where `[contract.UNCRAFD_CONSUMER_DOCUMENT_NAME]` first exists) — e.g. copying faoapi's `v1.5.0` pin out of habit. |
| Location | `src/views_crafdapi/seam_contract.py` (`declared_value`, `REGISTRY_PIN_TAG`) |

`declared_value` does `tomllib.loads(text)["contract"][contract_key]["value"]`. At a registry edition predating the UNCRAFD row the `[contract]` table (or the key) is absent, so the binding test errors with a bare `KeyError('contract')`/`KeyError('UNCRAFD_CONSUMER_DOCUMENT_NAME')` instead of a message naming the tag + row. **Loud, never silent** — the test fails either way — and the docstring documents the KeyError, so this is a clarity nit, not a correctness risk. Named fix: catch the absent table/key and raise a `ValueError` naming `REGISTRY_PIN_TAG` and the row ("the pinned edition predates this contract row — pin ≥ v1.5.2"). Mirrors the same nuance in views-faoapi's `test_seam_contract_binding` (faoapi#379).

**RESOLVED (2026-08-12, S1):** `declared_value` now catches the absent `[contract]` table/key and raises a clear `ValueError` naming the missing row and the likely cause (a pin predating `appwrite-seam-v1.5.2`). Test: `test_seam_contract_binding.py::test_declared_value_fails_clearly_when_the_row_is_absent`.

---

## Register Conventions

- **ID format:** `C-xx` for concerns, `D-xx` for disagreements. IDs are permanent — gaps in numbering indicate merged, resolved, or (here) inherited-but-unported entries. **New IDs start at `C-232` / `D-27`** — see the ID Namespace Note.
- **Sources:** `repo-assimilation`, `expert-review`, `test-review`, `falsification-audit`, `clean-architecture-review`, `pr-review`, `tech-debt-audit`, `incident`, `manual`
- **Resolution:** Move to "Resolved Concerns" with resolution date and summary when addressed
- **Header counts:** Manually maintained — update whenever a concern is added or resolved
- **Governed by:** [ADR-010](../docs/ADRs/active/010_technical_risk_register.md)

---

### C-258: A float64 invariant guard is pinned to one of two sibling aggregate methods, and the live endpoint uses the other

| Field | Value |
|-------|-------|
| ID | C-258 |
| Tier | 1 |
| Source | code-review max (2026-08-18, PR #93) |
| Trigger | Before changing anything reached by `ForecastDataset.calculate_hdi_map(aggregate=True)`, check whether the invariant you rely on is guarded on *that* method or only on `get_subset_dataframe(aggregate=True)`. Concretely: when the historical leg moves to the wire contract (C-169), re-pin every float64 guard to both entry points before touching either. |
| Location | `tests/test_aggregation.py:284` (`TestFeatureAggregationStaysFloat64`), guarding `src/views_crafdapi/data/handlers/forecast_dataset.py` `get_subset_dataframe` but not `calculate_hdi_map` |

ADR-030 §1 states that the historical/scalar leg must stay **float64 and byte-identical** — "the frame path's float32 stack would re-baseline it" — and `_aggregate_distributions` enforces it with an `is_prediction` dispatch. `TestFeatureAggregationStaysFloat64` exists specifically to hold that line, and its docstring says so.

It guards `get_subset_dataframe(aggregate=True)`. The live route `/{level}/analysis/historical/hdi-map?aggregate=true` (`managers/api.py:812` → `create_hdi_map_endpoint("historical", …)` → `calculate_hdi_map`) goes through the **other** method. PR #93's first draft rewrote `calculate_hdi_map`'s aggregate path to call `_sample_array` unconditionally — which returns `feat[var].astype(np.float32)` for feature datasets — and the full suite stayed green (1047 passed), because the guard was pointed at the sibling.

Reproduced on 2000 cells of the guard's own `rng.gamma(2.0, 30000.0)` distribution: the legacy path serves **115,868,248.0**, the S7 draft served **115,868,152.0** — a silent drift of **96.0** on a UN-facing endpoint. The difference is *where* the narrowing happens: the legacy path sums in float64 and lets `collapse` narrow once at the end; the frame leaf accumulates in float32 and compounds the error across every cell.

Two things this entry does **not** claim, both checked against `8a3a966` before writing: `calculate_hdi_map` never emitted the raw float64 sum on this leg — `collapse` has always narrowed its output — so the served value was float32-*width* before and after. And the initial review report's framing (drift measured against the float64 sum, 98.0) used the wrong baseline; the regression is against the prior served value, 96.0. Separately and pre-existing: `calculate_hdi_map` narrows while `get_subset_dataframe(aggregate=True)` returns float64 cells, so the two aggregate entry points have always disagreed in width on this leg. That is recorded here rather than opened as its own entry because it shares this one's root — two siblings, one guard.

Fixed in PR #93 before merge (the historical leg keeps the pre-S7 pandas path) and pinned with a new guard on `calculate_hdi_map`. **The entry is Tier 1 for the pattern, not the instance**: a stated invariant with a named regression guard was violated on the live path and CI reported success. Cross-refs: **C-169** (historical still on the legacy pandas path), **C-245** (the same historical endpoint serving mislabelled columns).

---

### C-259: Input validation lives inside `get_subset_dataframe`, so any path that reads samples directly inherits none of it

| Field | Value |
|-------|-------|
| ID | C-259 |
| Tier | 2 |
| Source | code-review max (2026-08-18, PR #93) |
| Trigger | When adding a third read path that goes to `_sample_array` without calling `get_subset_dataframe` — e.g. the tensor-native subset for the cell path, or any future streaming exporter — copy or extract the four validations rather than assuming the caller supplied clean input. |
| Location | `src/views_crafdapi/data/handlers/grid_dataset.py:1026` (feature membership), `:1038` (sample bounds + `sample_size is None`), `src/views_crafdapi/data/handlers/forecast_dataset.py` (the S7 aggregate path) |

`get_subset_dataframe` validates four things — feature membership (`ValueError: Invalid features specified`), sample-index bounds, `sample_size is None`, and time/entity ID existence — as a side effect of materialising columns. Nothing else does, and nothing names them as a contract.

PR #93 replaced `super().get_subset_dataframe(...)` with a direct `_sample_array` read on the aggregate path and silently lost all four. Measured consequences before the fix: `sample_idx=-1` wrapped to the last draw and served a **zero-width credible interval** (`hdi90_lower == hdi90_upper == map`) as a real posterior, where the cell path still raised; `features=['typo']` degraded from a descriptive `ValueError` to HTTP 500 with body `"'typo'"`; `features=[]` flipped from an empty result to a full multi-target aggregate.

Fixed in PR #93 by re-adding the validations, which leaves **two copies**. The residual risk is that they drift. Extracting them to a named boundary check is the real fix and is deliberately not done here (scope). Tracked as **#100**. Cross-refs: **C-247** (`severe_scenario` degenerating under sample subsetting — the same input, a different failure).

---

### C-260: Two live joint-sum implementations of the same query, already disagreeing on three axes

| Field | Value |
|-------|-------|
| ID | C-260 |
| Tier | 3 |
| Source | code-review max + review-diff (2026-08-18, PR #93) |
| Trigger | When the next caller needs a cross-level joint-sum, or when `views-frames` changes `aggregate_distributions_arrays`' grouping or dtype behaviour — check both implementations, not the one your test happens to cover. |
| Location | `src/views_crafdapi/forecast/aggregate/reduction.py::joint_sum_to_level` and `src/views_crafdapi/data/handlers/forecast_dataset.py::_frame_native_joint_sum` (live behind `get_subset_dataframe(aggregate=True)`, `managers/api.py:668`) |

The same logical operation — joint-sum `(N, S)` cell samples to a geographic level — now has two implementations. ADR-030's S7 addendum calls the duplication deliberate (WET; the two paths return different things and one caller genuinely wants a DataFrame), and that reasoning stands. What is *not* deliberate is that they already differ in three ways: the code→unit mapping (`np.unique`, sorted, vs `pd.factorize`, order-of-appearance), the missing-code predicate (`has_level_code` vs the `-1` sentinel), and — until fixed — float width for historical data.

`tests/forecast/test_cross_level_aggregate.py` proves `elementwise_sum` ≡ `aggregate_via_leaf`. Nothing proves the two joint-sum paths agree. Duplication is acceptable; undetected divergence between duplicates is what this entry tracks. Tracked as **#100**.

---

### C-261: `geo_metadata` is never checked against the metadata columns each level is documented to carry

| Field | Value |
|-------|-------|
| ID | C-261 |
| Tier | 3 |
| Source | code-review max (2026-08-18, PR #93) |
| Trigger | When loading a dataset via `from_value` from a cache written by an older schema, or when assigning `ds.geo_metadata` directly — verify the frame carries every column in `LEVEL_METADATA_COLUMNS` for the levels you will serve. |
| Location | `src/views_crafdapi/data/handlers/forecast_dataset.py` (metadata selection), `src/views_crafdapi/forecast/geography/metadata_table.py::LEVEL_METADATA_COLUMNS`, `from_value` (`:270`) |

`LEVEL_METADATA_COLUMNS` declares which metadata columns each served level carries. Nothing validates that `geo_metadata` actually has them. The pre-S7 aggregate path guarded each column with `if meta_col in aggregated_df.columns` and **silently omitted** any that were missing — so a `geo.parquet` written by an older schema yields a response quietly missing `admin1_gaul0_name`, with a 200 and no signal. `__init__` reindexes for uniqueness but does not check the column set; `from_value` assigns `pd.read_parquet(...)` with no check at all.

PR #93 preserves the silent-skip behaviour deliberately, to keep the change behaviour-neutral. The underlying gap is registered rather than fixed inside a performance PR. Tracked as **#100**. Cross-refs: **C-244** (schema documented at 36 columns against 45 in code) — both are the served column set drifting from its declaration.

---

### C-262: Neither service on the shared box has a memory ceiling, and the box has already run out three times

| Field | Value |
|-------|-------|
| ID | C-262 |
| Tier | 2 |
| Source | production `dmesg` + `systemctl status`, taken during the v0.4.0 deploy (2026-08-18) |
| Trigger | Before the next dataset grows — a longer month horizon, the ADR-034 `ADDITIONAL_TARGETS` list, or a finer grain — check the peak of BOTH units against the box total, not just the one you changed. Also check this before adding any endpoint that materialises a full-history frame. |
| Location | `/etc/systemd/system/views-crafdapi.service`, `/etc/systemd/system/views-faoapi.service` (neither declares `MemoryMax=`); `deployment/RELEASE_RUNBOOK.md` |

`views-crafdapi` and `views-faoapi` share one 22 GiB host. Measured on 2026-08-18, before the S7 deploy:

| | |
|---|---|
| box total | 22 GiB |
| `views-faoapi` `MemoryCurrent` | 5.9 G |
| `views-crafdapi` (v0.3.0, 21 h uptime) | 7.8 G, **peak 14.8 G** |
| available | 13 GiB |

crafd's own peak plus faoapi's resident set is **20.7 of 22 GiB**. There is no headroom left over, and nothing enforces a share.

It has already failed. `dmesg` records **three OOM kills of views-faoapi on 2026-08-14** (06:45, 07:10, 07:26) at ~23.3 GB anonymous RSS each — and crucially `constraint=CONSTRAINT_NONE ... global_oom`, meaning the *whole box* exhausted memory rather than a cgroup limit being enforced. The kernel picked faoapi because it was the largest consumer at that moment. On a different request mix it would have picked crafdapi, and a faoapi request would have taken down the CRAF'd API with no crafd-side signal at all. That coupling is the concern; the OOM itself is views-faoapi#418's problem.

Measured again after the v0.4.0 deploy, which changes *which* workload the ceiling must accommodate: the bulk endpoint in isolation now peaks at **6.0 G** (was 14.8 G), but the first request after a restart peaked at **16.8 G** because it included a cold load of both datasets — the historical leg's `pd.read_parquet` of 28.4M rows is a ~13 GB transient — registered locally as **C-263**. So a `MemoryMax=` set from the steady state would kill the service on every cold start, while one sized for the cold start does not fit beside faoapi's 5.9 G on a 22 GiB box (16.8 + 5.9 = 22.7). **C-263 has to move before this entry is satisfiable at all**; that is a dependency, not a preference.

S7 improves the arithmetic but does not address the coupling. `MemoryMax=` on both units converts "the box falls over and the kernel chooses a victim" into "this request fails, loudly, in the service that caused it" — a bounded local change, and the failure becomes attributable. Deliberately not bundled into the v0.4.0 deploy.

Tracked as **#99**, blocked by **#98**. Cross-refs: **C-235** (resolved — what used to drive crafd's peak), **C-263** (what drives it now, and blocks this), **views-faoapi#418** (the neighbouring service's own memory defect).

---

### C-263: The cold-start historical load is a ~13 GB transient, and it — not the aggregate path — is what the box cannot absorb

| Field | Value |
|-------|-------|
| ID | C-263 |
| Tier | 2 |
| Source | production measurement during the v0.4.0 deploy (2026-08-18) |
| Trigger | Before setting `MemoryMax=` on either co-hosted unit (C-262), measure the **first request after a restart**, not the steady state — they differ by ~2.8x. Also re-measure this before the historical artifact grows (a longer horizon, a finer grain, or additional targets). |
| Location | `src/views_crafdapi/managers/dataset_service.py` — the historical leg's `pd.read_parquet` of the 28.4M-row artifact (`:414` reads the parquet, `:520` logs the row count) |

Measured on the deployed service, same endpoint, twice:

| | peak RSS |
|---|---|
| first request after `systemctl restart` (cold caches) | **16.8 G** |
| after a restart with the disk cache warm, bulk endpoint only | **6.0 G** |

The difference is the historical leg being decoded from parquet into pandas. ADR-030 S7 removed the aggregate path's contribution (v0.3.0 ran at `peak: 14.8G`); what is left at 16.8 G is dominated by this load.

**Why it is Tier 2 rather than a performance note.** It is the binding constraint on a shared host, and it makes C-262 unsatisfiable as stated:

| | |
|---|---|
| box total | 22 GiB |
| crafd cold-start peak | 16.8 G |
| views-faoapi resident | 5.9 G |
| **sum** | **22.7 G** |

There is no `MemoryMax=` for crafd that both survives a cold start and leaves faoapi its memory. Sized from the 6.0 G steady state, the service dies on every restart; sized for the cold start, it does not fit beside its neighbour. Something has to give on this side first.

**Not to be confused with views-faoapi's C-169**, which this repo's code and ADR-030 both cite for it. That entry is *"historical is not shipped on the wire-contract path — a cutover silently freezes FAO historical / `s_actual`"* — a **delivery** concern, not a memory one, and it lives in a different repo's register (see **C-241**, **#58**). The memory cost recorded here was tracked in neither repo before this entry.

Tracked as **#98** (blocks **#99**). Cross-refs: **C-262** (the ceiling this blocks), **C-241** (why the C-169 citation resolves nowhere here), **C-236** (caches bounded by entry count, not bytes).

**Addressed in code 2026-08-18 (branch `perf/c263-cold-start-historical`, not yet deployed).**
The cause was not the parquet decode on its own: it is that the decoded frame and the
constructor's copies of it are resident **simultaneously**, so the peak is their sum. Freeing
afterwards cannot lower a peak that has already occurred — measured, and it does not.

`forecast/ingestion/historical_stream.py` now assembles the value-dir a row group at a time
(float64 blocks straight into `open_memmap`, geography appended to an open `ParquetWriter`),
adopted with the same `write_value_dir` the wire path uses and read back mmap'd. Measured on the
real artifact, both figures on the same host in the same state: **3.940 GB peak above baseline
against 12.205 GB** for the in-memory path — a 3.1x reduction, 8.27 GB saved — and near-flat
rather than linear in row count (0.354 GB at 60 months, 0.274 GB at 120). It is *slower*: 36.5 s
against 26.5 s, because the loader makes two passes; that trade is deliberate on a memory-bound
box. Byte-identical — manifest, index and every float64 feature block byte-for-byte, geo table
including category order, and the served outputs on 1.55M real rows (the C-258 `country`
aggregate, the gaul1 subset, the bulk `s_actual`, a cell-level subset).

*Two numbers in the first version of this note were corrected on 2026-08-21.* The in-memory peak
was given as "~9.4 GB extrapolated" — linear from the 120-month row, taken because the dev host
had 9.6 GB free. Measured with 19 GB free it is **12.205 GB**: the path grows superlinearly and
the extrapolation understated it by 30%, in the direction that flattered the change. And the
"~15-20% wall time" credited to the copy elisions was a blended, confounded figure; measured
separately they are worth ~9%, while the fourth change — validating `country_iso_a3` against
`.cat.categories` instead of `.astype(str)` over every row — is **135x** on that call (2.384 s to
0.018 s at 3.9M rows, ~17 s at full scale) and is the real wall-time win.

Three plausible causes were measured and rejected first, and are worth keeping because each
would otherwise be re-proposed: the "~6-7 GB of object-dtype geography strings" named in this
repo's own source comment is a `memory_usage(deep=True)` artifact (shared strings counted once
per row) and is really ~0.7 GB; releasing the retained file bytes and the source frame fixes two
genuine defects and moves the peak by **zero**; eliding three redundant frame copies buys
~15-20% wall time and also moves the peak by zero. The comment has been corrected in place.

**The entry stays open, and C-262 stays blocked**, because the number that closes it —
cold-start peak on the box, `systemctl restart` then one `/data/forecast/bulk` — has not been
taken. The local measurement predicts ~16.8 G → ~11 G; that is a prediction, not a result.

---

### C-264: A 401 tells an unauthenticated caller which Appwrite operation failed

| Field | Value |
|-------|-------|
| ID | C-264 |
| Tier | 4 |
| Source | observed during the v0.4.0 deploy smoke test (2026-08-18) |
| Trigger | Before the repo goes public (epic #315's crafdapi equivalent, **#38**), or when adding any new auth failure path — return a fixed string to the caller and log the detail server-side. |
| Location | `src/views_crafdapi/managers/api.py:318` and `:333`; the message originates at `src/views_crafdapi/managers/appwrite/manager.py:965` |

`api.py:318` builds the 401 body by interpolating the validation error:

```python
detail=f"Invalid API key: {validation_result.error or 'Authentication failed'}"
```

and that error is composed upstream as `f"List buckets failed: {e.message}"`. A caller presenting *any* wrong key therefore receives:

```
401  Invalid API key: List buckets failed: The current user is not authorized to
     perform the requested action.
```

which discloses that the backend is Appwrite, that the key is validated by attempting a bucket listing, and the provider's own wording. Observed live while a placeholder was pasted as a key. `api.py:333` has the same shape with `{str(e)}`.

No credential or data is exposed and the request is correctly refused, hence Tier 4. The cost is that an unauthenticated probe learns the storage backend and one of its operations for free. The fix is a fixed client-facing string with the detail logged, not returned. Tracked as **#102**.

---

### C-265 (RESOLVED): Runbook Step 7 states the pre-delivery failure state as the current expectation

| Field | Value |
|-------|-------|
| ID | C-265 |
| Tier | 3 |
| Source | falsify (2026-08-18, "it is now safe to shutdown this session") |
| Trigger | Before the next deploy verification, or the first time anyone other than the author follows the release procedure — read Step 7 against what the service actually does today, not against what it did at stand-up. |
| Location | `deployment/RELEASE_RUNBOOK.md:158-168` |

Step 7 instructs the reader:

> *"because the CRAF'd bucket is empty, the forecast/historical coverage checks will report **503 (fail-visible)** — that is **expected and correct** until the producer's first delivery, *not* a broken deploy. What must pass now is `ping` and `version = 0.1.0`."*

The first delivery landed **2026-07-27**. The service serves **0.4.0** and `smoke.py` returns **ALL PASS**. A reader following Step 7 to verify a deploy today is told to expect a failure state that would now indicate a real outage — and told to accept it as correct. The document does not lie about a past event; it states a stale expectation in the present tense, inside a procedure meant to be followed.

The staleness was identified during the v0.4.0 deploy and explicitly promised as a follow-up ("I'll fix that stale text in the runbook separately"). It was not done, and nothing outside that conversation recorded the promise. **That is the part that makes this a register entry rather than a typo**: the loss mode was an undone commitment held only in a chat log, not a missing measurement — every measurement from that session did survive.

Fix: mark Step 7 explicitly as the historical first stand-up, or restate its expectations for a service that has data. Enforced by `tests/test_falsify_shutdown_safety.py::test_runbook_does_not_tell_the_reader_to_expect_the_pre_delivery_failure_state`.


**RESOLVED (2026-08-21, release/v0.5.0).** Step 7 is now explicitly framed as the record of the
2026-08-02 first stand-up, with a block quote stating the current expectation directly: the first
delivery landed 2026-07-27, `smoke.py` returns ALL PASS, and a 503 from the coverage checks is a
real outage rather than an expected state. Readers are pointed at the recurring "Every future
release" block for any deploy after the first. Enforced by
`tests/test_falsify_shutdown_safety.py::test_runbook_does_not_tell_the_reader_to_expect_the_pre_delivery_failure_state`,
which had been `xfail(strict=True)` and is now an ordinary passing guard.
---

### C-266 (RESOLVED): The release block still hardcodes a real-looking tag — the trap it was rewritten to remove

| Field | Value |
|-------|-------|
| ID | C-266 |
| Tier | 3 |
| Source | falsify (2026-08-18, "it is now safe to shutdown this session") |
| Trigger | At the next release, **before** pasting the "Every future release" block onto the box — confirm the tag in it is the one you intend to deploy, not the one the last release left behind. |
| Location | `deployment/RELEASE_RUNBOOK.md:199` |

Before v0.4.0 this block named `v0.2.0` **twice**, two releases after that stopped being current. Pasting it unedited would have written `v0.2.0` to the deploy-tag file and passed `--expect-tag v0.2.0` to `smoke.py` — silently rolling production back while reporting success, because both copies agreed with each other.

It was rewritten to a single `TAG=` variable, and the runbook now asserts *"One variable, changed once, is why it is written this way."* That claim overstates the fix. The block reads:

```bash
TAG=v0.4.0   # <-- the ONLY line to change. Set it to the tag you are deploying.
```

The duplication is gone; **the trap is not**. `v0.4.0` is a real, plausible tag that pastes cleanly and will be stale at the next release exactly as `v0.2.0` was. The comment mitigates but does not prevent — and the original failure happened precisely because someone was moving fast enough not to read.

A placeholder (`TAG=vX.Y.Z`) fails at the deploy gate on an unedited paste, which is the correct direction: a stale placeholder fails loudly, a stale real tag succeeds wrongly. Enforced by `tests/test_falsify_shutdown_safety.py::test_runbook_release_block_does_not_hardcode_a_real_looking_tag`.

Cross-refs: **C-167** shape (tag/version dual source of truth) and the 2026-08-15 deploy-gate outage, which is what taught this repo that a tag naming the wrong thing is an outage, not a nit.


**RESOLVED (2026-08-21, release/v0.5.0).** The block reads `TAG=vX.Y.Z`, and the smoke line now
uses `--expect-tag "$TAG"` rather than a second written-out tag. An unedited paste fails at the
deploy gate — `checkout-deploy-tag.sh` cannot resolve `refs/tags/vX.Y.Z` and refuses — which is
the direction the entry asked for: a stale placeholder fails loudly, a stale real tag succeeds at
deploying the wrong version. Enforced by
`tests/test_falsify_shutdown_safety.py::test_runbook_release_block_does_not_hardcode_a_real_looking_tag`,
previously `xfail(strict=True)`, now an ordinary passing guard.

Worth keeping in view: `v0.4.0` in that slot *was* the fix for the `v0.2.0` trap, and it
reintroduced the same defect one release later. The un-xfailed test is what stops a third round.
---

### C-267: An unknown entity id is answered with an empty 200, not a rejection — at every level, on every path

| Field | Value |
|-------|-------|
| ID | C-267 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-18) |
| Trigger | When a consumer reports "the API returns nothing for country X", check the id against `geo_metadata` before concluding the forecast is empty. And when adding a new served level or a new entity vocabulary (M49 codes, a GAUL edition bump), add the existence check — there is none to copy. |
| Location | `src/views_crafdapi/data/handlers/forecast_dataset.py:665-670` and `:927-932` (code→cell resolution), `src/views_crafdapi/forecast/geography/metadata_table.py:28` (`resolve_level_cells`), `src/views_crafdapi/data/handlers/grid_dataset.py:958-982` (`_subset_mask`), `:1292` (the guard that never fires), `src/views_crafdapi/managers/api.py:641-700` |

Both `calculate_hdi_map` and `get_subset_dataframe` resolve a caller's entity codes to PRIO-GRID cells **before** any validation runs. `resolve_level_cells` returns `[]` for a code no cell carries, `entity_ids` becomes `[]`, and `_subset_mask`'s `.isin([])` selects nothing — so the base class's `KeyError: Invalid entity IDs` (`grid_dataset.py:1292`) is handed an empty set and never fires. The check exists; the resolution step upstream of it guarantees it has nothing to reject.

Verified on the repo's own fixture (`make_fao_df`, ISO3s `AAA`/`BBB`): `calculate_hdi_map(entity_ids=['ZZZ'], level='country')` returns `(0, 33)` un-aggregated and `(0, 24)` aggregated; `get_subset_dataframe(entity_ids=['ZZZ'], level='country', aggregate=True)` returns `(0, 2)`; `calculate_hdi_map(entity_ids=[999999], level='gaul1', aggregate=True)` returns `(0, 24)`. All surface as HTTP 200, `success: true`, `shape: [0, N]`.

What makes this a concern rather than a documented policy is the **inconsistency**: the same endpoint answers a bad `time_ids` with `KeyError` → HTTP 500 and a bad `features` with `ValueError` → HTTP 500. Three parameters, three behaviours, one of them silent. A consumer who writes `GRB` for `GBR` — or holds a GAUL code retired by a boundary revision — receives a well-formed "no fatalities predicted" where they should receive a rejection, and nothing in the response, the logs, or `/health` distinguishes that from a genuine zero. The same class as **C-232** (a healthy-looking empty answer), reached by a different mechanism, and adjacent to but not covered by **C-259**, which concerns validations *lost* on the direct `_sample_array` path — this one is bypassed on **every** path, including the ones C-259 restored, because the resolution step runs first.

Cross-refs: **C-232**, **C-259**, **C-261** (the other unchecked assumption about `geo_metadata`'s contents).

---

### C-268: Revoking an API key does not stop it being served — the fail-soft cache paths keep answering for weeks

| Field | Value |
|-------|-------|
| ID | C-268 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-18) |
| Trigger | When revoking or narrowing an Appwrite key in response to a suspected compromise, **restart the service** (or evict `_manager_cache`) — ADR-027 §4's promise that faoapi "inherits the change on the next authenticated call" does not hold. Equally, when adding a TTL or revalidation to `_manager_cache`, decide explicitly what the fail-soft metadata degradations should do on a *permanent* authorization failure rather than a transient one. |
| Location | `src/views_crafdapi/managers/api.py:337-352` and `:354-378` (the two FastAPI dependencies), `:304-330` (`_validate_api_key`), `src/views_crafdapi/managers/dataset_service.py:196-206` (the manifest lookup), `:527-592` (`_serve_last_good_within_sla`), `docs/ADRs/active/027_authentication_and_per_key_isolation.md:41` |

`_validate_api_key`'s docstring states it "validates the API key on every call", but both dependencies that reach it are guarded by `if api_key_hash not in self._manager_cache` — an `LRUCache(maxsize=100)` with **no TTL**, a choice ADR-011 §3 made deliberately ("no TTL for `_manager_cache` since client objects are stateless"). A key validated once therefore skips revalidation until 100 distinct other keys evict it or the process restarts.

The revoked key then does not fail loudly downstream, because every metadata call on the forecast path is fail-soft by design. `get_latest_manifest()` → `search_files_by_metadata` catches the `AppwriteException` and returns `success=False` → `get_predictions_by_metadata` logs and returns `[]` → the manifest resolves to a genuine `None`. That is `NoRun`, which routes to `_serve_last_good_within_sla("no_manifest")`, and the last-good **manifested** run in that key's own disk partition is served — degraded, logged, but served — for as long as it stays inside the 45-day freshness SLA and the 3.5-week disk TTL. The revoked key keeps reading real forecast numbers through `/{level}/analysis/forecast/hdi-map` and `/data/forecast/bulk` for up to ~24 days. `/historical` degrades to 404 once the 4-hour warm TTL lapses, so the exposure is forecast-specific.

No cross-key leakage is involved — the data served is that key's own partition. The failure is that **revocation, the operator's only response to a leaked key, has no effect on the serving path**, and the ADR asserts the opposite. Tier 2 rather than 1: the answer served is correct data, not corrupt data; what fails is the access-control lifecycle. Every mechanism involved was individually justified (no-TTL manager cache, ADR-011; never drop a good entry on a metadata blip, C-233; bounded grace fallback, ADR-033 §6) — the concern is their composition, which nothing states or tests.

Cross-refs: **C-233** (the same fail-soft-on-metadata-error root, a different consequence), **C-269** (the other ADR-027 claim that does not hold), ADR-011 §3, ADR-027 §4.

---

### C-269: The downloaded-file cache is shared across API keys, and `/files/{id}/cached` serves from it with no authorization check

| Field | Value |
|-------|-------|
| ID | C-269 |
| Tier | 2 |
| Source | repo-assimilation (2026-08-18) |
| Trigger | **Before issuing a second API key with a narrower Appwrite scope than the first** — either partition `CacheManager`'s directory per caller or remove the `/files/{bucket_id}/{file_id}/cached` route. Also when reading ADR-027 §3's isolation list as a completeness claim: it enumerates three cache layers and this is a fourth. |
| Location | `src/views_crafdapi/managers/appwrite/manager.py:90-108` (`_setup_cache`), `src/views_crafdapi/managers/appwrite/file_cache.py:65-66` (`_get_cache_key`), `:146-165` (`get_cached_file_path`), `:57-63` (`_save_cache_metadata`), `src/views_crafdapi/managers/api.py:966-999` (the `/cached` route), `:1004-1032` (`/cache/stats`, `DELETE /cache`), `docs/ADRs/active/027_authentication_and_per_key_isolation.md:31-37` |

ADR-027 §3 states that "all cached state is keyed by `api_key_hash`, so callers are isolated by construction" and that a request authenticated with key *A* "can never be served *B*'s cached dataset". Each per-key `AppWriteFileManager` does construct its own `CacheManager` — but every one of them resolves the same directory, `self.config.path_manager.cache / "appwrite_cache"`, because `path_manager` is the single shared `self._model_path` handed to every `AppwriteConfig`. The cache key is `f"{bucket_id}_{file_id}"` with no caller component.

The `/files/{bucket_id}/{file_id}/cached` route reads `manager.cache_manager.get_cached_file_path(...)` **first** and returns those bytes as a `FileResponse`. The only Appwrite call it makes afterwards is `get_file`, for a display filename — and its failure is handled by falling back to `filename = file_id` and returning the file anyway. So any caller whose key passes the one-time `list_buckets` probe can retrieve any file another key has cached, including from buckets their own key cannot read. The sibling `/download` route does 404 first on a failed `get_file`, which is what makes this route's ordering the defect rather than a general design.

Two lesser consequences of the same sharing: `DELETE /cache` purges every caller's cached bytes, and `/cache/stats` enumerates the whole box's cache broken down by bucket. One more, independent of authorization: each `CacheManager` loads the metadata JSON once at construction and rewrites the entire dict on every `add_to_cache`, so two long-lived per-key managers silently drop each other's entries — a lost-update race on a file no lock protects.

Masked today by an effectively single-key deployment, which is the same condition that masks **C-236** and **C-272**. The trigger is the day that stops being true.

Cross-refs: **C-236** (the same `_file_cache`/`CacheManager` layers, bounded-by-count rather than isolation), **C-268** (the other ADR-027 claim that does not hold), ADR-027 §3.

---

### C-270: The documented `search` parameter on `/files/{bucket_id}` builds a query syntax the pinned SDK does not speak

| Field | Value |
|-------|-------|
| ID | C-270 |
| Tier | 3 |
| Source | repo-assimilation (2026-08-18) |
| Trigger | When a caller reports that `?search=` on the file listing 404s or ignores the term. And before documenting any new Appwrite query parameter — build it with `appwrite.query.Query`, never by string formatting. |
| Location | `src/views_crafdapi/managers/api.py:838-841`, `src/views_crafdapi/managers/appwrite/manager.py:851-885` (`list_files`), `:945-955` (`list_buckets`, the correct sibling), `README.md:160,220`, `docs/api/README.md:94` |

The route appends `f"search('name','{search}')"` to the query list. That is Appwrite's pre-0.15 query syntax; the pinned SDK (`appwrite==19.2.0`) emits JSON — verified locally: `Query.search('name','forecast')` returns `'{"method":"search","attribute":"name","values":["forecast"]}'`. The hand-built string passes through `list_files` unchanged to `storage.list_files`, the server rejects it, and the route returns HTTP 404 `"Error listing files"` for what is a malformed request, not a missing bucket.

`list_buckets` fifteen lines further down uses `Query.search` correctly, so this is one missed call site rather than a systemic pattern — and the reason it survived the SDK-19 migration (ADR-018/ADR-019) is that no test constructs a real query for this parameter; the file-management surface is mocked at the manager boundary throughout. Both `README.md` and `docs/api/README.md` document `search` as a working listing filter.

Secondary, and worth naming even though it is currently inert: the caller's raw input is interpolated into a query string with no escaping. It is inert only because the query is rejected wholesale — the construction, not the rejection, is what would have to change if the syntax were ever corrected by making the string valid rather than by switching to `Query`.

Cross-refs: **C-243** (the same shape — a documented behaviour with no test that would notice it breaking), ADR-018/ADR-019 (SDK normalization and pinning).

---

### C-271: Three runtime dependencies are imported by `src/` but never declared; two more are declared dev-only

| Field | Value |
|-------|-------|
| ID | C-271 |
| Tier | 3 |
| Source | repo-assimilation (2026-08-18) |
| Trigger | When a transitive dependency stops requiring one of these — e.g. `wandb` dropping `PyYAML`, or `appwrite`/`fastapi` dropping `pydantic` — or when trimming the declared dependency set under ADR-015. Note that ADR-015 audits only the *declared-but-unused* direction and would not catch any of these. |
| Location | `src/views_crafdapi/managers/log.py:4` (`yaml`), `src/views_crafdapi/client.py:9` + `src/views_crafdapi/__init__.py:7` (`requests`), `src/views_crafdapi/managers/appwrite/sdk_compat.py:7` (`pydantic`), `src/views_crafdapi/plotting.py:11,14-17` (`geopandas`, `matplotlib`), `pyproject.toml:13-41` (runtime), `:73-81` (dev) |

`PyYAML` sits on the **production boot path** — `CrafdApiManager.__init__` → `ModelManager.__init__` → `LoggingModule._setup_logging` → `yaml.safe_load` of `configs/logging.yaml` — and reaches the environment only through `wandb`. `requests` is imported by the package's own `__init__` (via `client.py`), so a bare `import views_crafdapi` fails without it; it too arrives transitively. `pydantic` is imported by `sdk_compat`, which normalizes **every** Appwrite SDK response on the serving path. None of the three appears in `[project] dependencies`.

Separately, `geopandas` and `matplotlib` are declared under `[dependency-groups] dev` yet imported at module scope by `src/views_crafdapi/plotting.py`, so `import views_crafdapi.plotting` raises `ModuleNotFoundError` in a production install — a `src/` module that only works in a dev environment.

`uv.lock` currently pins all five, so the failure mode is a future resolution rather than today's deployment: the boot path breaking on an upstream's dependency trim, in an environment built from `pyproject.toml` rather than the lock. ADR-015 §1 mandates that "every runtime dependency in `pyproject.toml` must have at least one corresponding import in `src/`" — and stops there; nothing states or checks the converse, and its own Alternative A (`deptry` in CI) was deferred as not yet worth the overhead. This entry is the evidence for revisiting that.

Cross-refs: ADR-015 (whose audit direction leaves this uncovered), **C-239** (the other place a `src/` asset's real requirements diverge from what the package declares).

---

### C-272: The forecast serving-state and served-provenance are process-global, so one caller's refusal rewrites `/health` and `/provenance` for everyone

| Field | Value |
|-------|-------|
| ID | C-272 |
| Tier | 3 |
| Source | repo-assimilation (2026-08-18) |
| Trigger | Before issuing a second API key — or when diagnosing a `degraded` `/health` that no operator action and no log line for *your* key explains. Also when acting on `/provenance/forecast`'s `artifact_id`: it names the last run **any** caller was served, not yours. |
| Location | `src/views_crafdapi/managers/dataset_service.py:131,135` (the two fields), `:137-152` (their accessors), `:236-241`, `:317-341` (where they are set), `src/views_crafdapi/managers/api.py:1037-1100` (`/health`), `:394-435` (`_forecast_lineage`) |

Every cache layer in the service is keyed by `api_key_hash` — but `_forecast_serving_state` and `_last_forecast_provenance` are plain instance attributes on the single process-wide `DatasetService`. A request from key *B* that ends in `Refused` or `NoRun` sets `{"degraded": True, "reason": …}`, which flips `/health`'s `status` to `degraded` and attaches `refusal_reason` to `/provenance/forecast` for key *A*, whose own serve is healthy. The inverse holds too: *B*'s next successful serve clears the state *A*'s refusal set, so a genuinely degraded caller can be shown green.

**C-233** already records the not-keyed-by-API-key property, but in a specific and now-historical context — as one of two reasons a since-removed `is_served` field reported wrongly in both directions. The surface that remains after that removal — the `degraded` flag that drives external monitoring (ADR-032) and the `artifact_id` an operator uses to identify what is live — is not covered there. Registering separately rather than extending C-233 because the fix differs: C-233 wants a signal that exists on the manifest-only path, this wants the existing signals keyed per caller.

Masked today by the effectively single-key deployment, as with **C-236** and **C-269**.

Cross-refs: **C-233** (same root, different surface), **C-254**, **C-269**, ADR-032 (which pages on the `/health` body this contaminates).

---

### C-273: `wandb_alert` redacts the literal string `"None"` when no path is supplied

| Field | Value |
|-------|-------|
| ID | C-273 |
| Tier | 4 |
| Source | repo-assimilation (2026-08-18) |
| Trigger | When re-enabling W&B notifications — `wandb_notifications` is `False` on every current construction path, so this is dormant until someone turns it on. |
| Location | `src/views_crafdapi/wandb/utils.py:13,34`, `tests/test_wandb_redaction.py:59` |

The redaction is `str(text).replace(str(models_path), "[REDACTED]")`. With the parameter's default `models_path=None`, `str(None)` is `"None"`, so every occurrence of that substring in the alert body is replaced — garbling exactly the messages ("value is None", "returned None") an alert is most likely to carry. The suite characterizes this as a footgun and **pins** the behaviour (`test_models_path_none_redacts_literal_none_substring`) rather than correcting it, which is the right call for a characterization test and the reason it needs a register entry instead: the test records the defect, it does not schedule it. Unreachable today — `CrafdApiManager` is constructed with `wandb_notifications=False` and no serving path calls `wandb_alert` — hence Tier 4. Named fix: skip the replacement entirely when `models_path` is falsy.

---

### C-274: Three `xfail` tests documenting open priogrid concerns now pass, and nothing reports it

| Field | Value |
|-------|-------|
| ID | C-274 |
| Tier | 4 |
| Source | repo-assimilation (2026-08-18) |
| Trigger | When next reading `C-61`–`C-65` as open concerns, or when considering `xfail_strict = true` in `pyproject.toml` — these three are the cases that would flip the suite red, and each needs a decision (gap closed → delete the xfail; assertion gone vacuous → fix it) before that switch. |
| Location | `tests/test_falsify_priogrid_naming.py::TestP1_DatafactoryAlsoEmitsPriogridGid::test_datafactory_grid_to_dataframe_uses_priogrid_id`, `tests/test_falsify_shim_diagnosis.py::TestP1_MigrationTestCoverageGap::test_api_py_also_references_priogrid_gid`, `tests/test_falsify_shim_diagnosis.py::TestP4_CICDocumentsShimAsContract::test_fao_pgm_cic_does_not_guarantee_priogrid_gid`, `TESTING.md:75` |

All three are marked `xfail` with the reason "documents open priogrid concerns C-61..C-65" and currently **XPASS** — verified 2026-08-18 via `uv run pytest -m "not integration" -rX` (1056 passed, 25 xfailed, **3 xpassed**). Under pytest's default non-strict setting an xpass neither fails the run nor appears in the default summary line's detail, so a documenting test that has started passing is indistinguishable, to anyone reading CI, from one still failing as designed. The gap it records may have closed upstream, or the assertion may have gone vacuous against changed source — the suite reports the same thing either way, and the register still lists C-61..C-65 as open on the strength of these tests.

TESTING.md documents the xfail-vs-skip convention (skip for absent siblings, xfail for known gaps) but says nothing about what an xpass means or who acts on it. Same family as **C-243**: the suite's own claims drifting from what it measures.

Cross-refs: **C-243**, inherited **C-61**–**C-65** (bodies not in this register — see **C-241**).

---

### C-275: A CIC in an ADR-006-governed directory declares "Related ADRs: None"

| Field | Value |
|-------|-------|
| ID | C-275 |
| Tier | 4 |
| Source | graphify (2026-08-18) |
| Trigger | When changing `PredictionStoreManager`'s selection, quarantine, or manifest-resolution behaviour and using its CIC to find which ADRs constrain the change — the header says none constrain it. Also when auditing CIC coverage under ADR-006, since this row reads as "checked, nothing applies" rather than "not filled in". |
| Location | `docs/CICs/PredictionStoreManager.md:6` |

Every other class contract in `docs/CICs/` names its governing ADRs, and seven of the nine name **ADR-006 ("this contract")** explicitly — the ADR that mandates the CIC's own existence. `PredictionStoreManager.md` alone declares `**Related ADRs:** None`.

The claim is false on the file's own subject matter. `PredictionStoreManager` is where the ADR-013 §11.4 legacy-type transition guard lives (`manager.py:118-122`), where the C-71 quarantine blocklist and approval allowlist are applied, where `get_latest_manifest` implements the ADR-033 §2 manifest-first selection, and where `_provenance_from` builds the C-86 lineage record. ADR-006, ADR-013, ADR-033 and ADR-023 all bear on it directly.

Registered rather than silently fixed because the interesting part is the failure mode, not the omission: an explicit **"None"** is a *declaration* under ADR-003, not a blank. A reader who trusts declarations — which this repository instructs them to do — concludes that no ADR governs the store manager and changes it without consulting one. A blank field would have prompted a check. Named fix: replace with the four ADRs above, or with `<to be filled>` if the audit has genuinely not been done.

Cross-refs: **C-243** (docs asserting things about the code that are no longer true), **C-276** (the sibling CIC defect found in the same pass), ADR-006, ADR-003.

---

### C-276: `ForecastDataset.md`'s rename record was flattened by a global search-and-replace and now says nothing

| Field | Value |
|-------|-------|
| ID | C-276 |
| Tier | 4 |
| Source | graphify (2026-08-18) |
| Trigger | When tracing why `ForecastDataset` has a back-compat alias, or what the class was called before Phase 4a of #87 — this line is the only record and it no longer carries the old name. Also before running any future repo-wide identifier rename across `docs/`. |
| Location | `docs/CICs/ForecastDataset.md:7` |

The line reads:

> **Supersedes (as the public leaf):** `ForecastDataset.md` (the class was renamed `ForecastDataset → ForecastDataset` in Phase 4a of #87; `ForecastDataset` is retained as a back-compat alias).

A file that supersedes itself, and a rename from a name to the same name. The sentence was written to record a real event — the `_FAOPGMDataset`/`FAOForecastDataset`-era class becoming `ForecastDataset` during the un_fao → un_crafd clone — and a repo-wide rename pass then rewrote **both** sides of the arrow, destroying the only thing the sentence existed to preserve. The graph surfaced it as a self-edge: the extraction produced a `supersedes` relation whose source and target were the same node.

Harmless to running code and to the served contract; it costs a reader the one recorded answer to "what was this called before, and why is there an alias". The same rename pass is the mechanism behind **C-257** (the `un_fao` → `un_crafd` launcher clone) — a class of change this repository has now been bitten by twice. Named fix: recover the pre-rename name from `git log --follow docs/CICs/ForecastDataset.md` and restate it, or delete the line and say plainly that the pre-clone name was lost.

Cross-refs: **C-275** (found in the same pass), **C-257** (same rename-pass mechanism), **C-243**.
### C-277: The disk-cache read path serves whatever is in the slot, without re-running the C-72 gate

| Field | Value |
|-------|-------|
| ID | C-277 |
| Tier | 2 |
| Source | code-review medium (2026-08-21, PR review of `perf/c263-cold-start-historical`) |
| Trigger | When adding any **third** writer of a value-dir — a wire-contract variant, a backfill tool, an operator repair script — validate before `write_value_dir`, not after reading back. The read path will not catch it for you. Equally, before relying on "implausible data cannot reach FAO", check that every writer validates, because nothing checks the reader. |
| Location | `src/views_crafdapi/managers/dataset_service.py:283-306` (the disk gate), `src/views_crafdapi/managers/disk_cache.py::read`, versus the write-side gates at `dataset_service.py:521-522` (in-memory), `forecast/ingestion/wire_reader.py::WireRunAssembler.append_month` (wire), `forecast/ingestion/historical_stream.py::assert_plausible_chunk` (streamed) |

ADR-013 §4.5 and the DatasetService CIC §6 both state the C-72 guarantee as "implausible data fails loud rather than reaching FAO". That guarantee is **entirely write-side**. `CrafdDiskCacheManager.read` reconstructs a dataset from the value-dir and hands it back; the disk gate in `get_latest_dataframe` serves it directly. No plausibility check runs on that path, ever.

That is safe only while every writer validates before adopting — which is now true, but was not: the streamed historical ingest introduced on this branch called `write_value_dir` and validated *afterwards*. Reproduced end-to-end before the fix: ingest a good artifact, then one with `pg_ycoord = 999.0`; the bad value-dir was adopted (`rmtree` + `os.replace` over the previous good entry), validation raised, the broad fallback caught it, the request 500'd from the in-memory path — and the **next** request took the disk gate and served `pg_ycoord = 999.0` with `success: true`. The last good entry was already deleted.

Fixed on this branch by moving the check into `stream_to_value` so it runs per row group, before anything is committed, and by raising `ImplausibleArtifact` rather than falling back (a data fault is not a streaming fault). **The entry is registered for the residual, not the instance**: there are now three independent writers of that cache slot and one unguarded reader, and the invariant that keeps them honest is written down in an ADR rather than asserted anywhere in code. A fourth writer that forgets produces exactly the failure above, silently.

Cheapest real mitigation, deliberately not taken here (scope): validate on the read path once, or record the validation verdict in the value-dir meta so the reader can refuse an unvalidated entry.

Cross-refs: **C-259** (validation living in one path and not its sibling — the same shape, one layer up), **C-72** (inherited — the gate this is about), **C-263** (the change that exposed it).

---

### C-278: The streamed and in-memory historical ingests accept different artifacts, and nothing tests that they agree

| Field | Value |
|-------|-------|
| ID | C-278 |
| Tier | 3 |
| Source | code-review medium (2026-08-21, PR review of `perf/c263-cold-start-historical`) |
| Trigger | When the producer changes the historical artifact's shape — a new value column, a nullable geography column, an unsigned index dtype, a different row-group layout — check which path will ingest it. A change that only makes the *streamed* path refuse degrades silently to a 12.2 GB cold start rather than failing. Also when `historical_targets` is ever actually configured (it is `{}` in production today — C-238). |
| Location | `src/views_crafdapi/forecast/ingestion/historical_stream.py::stream_to_value` (the `NotStreamable` preconditions) versus `src/views_crafdapi/data/handlers/grid_dataset.py::_init_dataframe` + `data/handlers/forecast_dataset.py::__init__` |

There are now two implementations of "turn a historical artifact into a `ForecastDataset`", and they do not accept the same inputs. The streamed one refuses: a non-dense grid, rows not already in `sort_index()` order, null geo metadata, and any value column outside the declared `targets`. The in-memory one accepts all four — it dense-fills, sorts, backfills geography per entity, and carries the extra columns as features.

Each refusal is deliberate and each was added because the alternative was worse: reimplementing the constructor's dense-fill and per-entity backfill inside the streamer would duplicate the subtlest logic in the repo, and getting it slightly wrong changes served geography with no error. Refusing and falling back is the safe direction.

The residual is that the divergence is **invisible when it fires**. A refusal logs at INFO and the request still succeeds — at the memory cost this whole change exists to remove. So the failure mode of C-263's fix is not an error; it is a quiet return to the old peak, discoverable only by reading logs or re-measuring the box. Nothing asserts that the two paths accept the same set of artifacts, and the byte-identity tests compare outputs only for inputs *both* accept.

Also folded in here rather than given its own entry: the historical target-detection rule (`configs_getter().get("historical_targets")` else every non-index, non-metadata column) is now written twice — `dataset_service.py:504-507` and `:593-596`. Second occurrence, so WET-before-DRY says leave it; recorded because the two must agree or the paths build different `targets`.

Cross-refs: **C-260** (two live implementations of one operation, already disagreeing — the same shape on the aggregate path), **C-238** (why `historical_targets` is unset in production, which is what currently masks the target-coverage divergence), **C-263**.

---

### C-279: A byte-identity proof over one artifact left a guard hole that eight mutation tests also missed

| Field | Value |
|-------|-------|
| ID | C-279 |
| Tier | 3 |
| Source | code-review medium (2026-08-21, PR review of `perf/c263-cold-start-historical`) |
| Trigger | When the next change is justified by "byte-identical on the real artifact", ask which properties of *that* artifact the proof depended on, and whether the guard is checked against inputs that violate them. Specifically: before trusting a streaming validator, construct an input where the violation straddles a chunk boundary rather than sitting inside one. |
| Location | `src/views_crafdapi/forecast/ingestion/historical_stream.py:160-185` (the ordering guard), `tests/forecast/test_historical_stream.py::TestReviewFindings` |

The streamed ingest rests on one precondition — file order already *is* `sort_index()` order — and the module verifies it per row group rather than assuming it. The verification had a hole: it compared cells only *within* a chunk and months only *across* the boundary, so a month split across two row groups with its halves written out of order passed every check. At 64,742 cells per month against ~1,048,576-row row groups a month spans row groups roughly every 16 months, so this is the ordinary case, not an exotic one.

What makes it worth an entry is what did **not** catch it. The change shipped with: byte-identity against the in-memory path on the real 28.4M-row artifact; served-output equality on 1.55M rows across four call paths; and eight mutation tests, each confirmed to fail when its guard was removed. All of them passed. They passed because the real artifact is globally sorted, so the hole was unreachable through any of them — the proof was over an input whose properties made the defect invisible.

The lesson generalises past this module: a guard tested only against inputs that satisfy the precondition tests the happy path of the guard, not the guard. Fixed by carrying the last cell across the boundary, with a test that builds the three-row-group artifact directly.

Cross-refs: **C-258** (Tier 1 for the pattern rather than the instance — a stated invariant violated while CI reported success), **C-243** (shape-only assertions), **C-263**.

---

### C-280: The deploy gate's refusals are deterministic, and nothing bounded the retry

| Field | Value |
|-------|-------|
| ID | C-280 |
| Tier | 2 |
| Source | production journal + Better Stack incident history (2026-08-21) |
| Trigger | When a deploy leaves the service 502ing for more than a minute or two, read the journal for `FATAL deploy-gate:` before assuming a slow start — and when adding any new refusal to `checkout-deploy-tag.sh`, check that it is one an operator can act on from `systemctl status` alone. |
| Location | `deployment/views-crafdapi.service` (`Restart=always`, `RestartSec=5`), `scripts/checkout-deploy-tag.sh:19-65` (four `exit 1` paths) |

`checkout-deploy-tag.sh` runs as `ExecStartPre` and refuses on four conditions: a missing or
blank deploy-tag file, a tag absent from origin, a `uv sync --frozen` failure, and a
tag-vs-package-version mismatch. Every one is **deterministic** — retrying changes nothing. The
unit paired that with `Restart=always` and `RestartSec=5` and no start-limit, so a refused
release retried every five seconds indefinitely: the socket never bound, nginx returned 502 for
as long as it took a human to notice, and `systemctl status` reported `activating` rather than
`failed`, because from systemd's point of view a start was always in progress.

Measured, which is what separates this from the theory it replaced. A *successful* restart is
**~3 seconds** (2026-08-21: stop 10:06:21, `deploy-gate: serving tag v0.4.0` 10:06:23, listening
10:06:24), so the gate is not slow and the deploy path does not need restructuring. The Better
Stack history is the other half: `crafdapi/ping` incidents of **47 min and 20 min on 2026-08-15**
(the tag/version drift that produced `v0.2.1`) and **11 min on 2026-08-17**, all `Status 502`,
all auto-resolved once someone fixed the underlying refusal. Those are failing gates, not slow
starts.

Fixed on this branch: `StartLimitIntervalSec=600` / `StartLimitBurst=30` under `[Unit]`, and
`TimeoutStartSec=180` under `[Service]`. A failing attempt cycles in ~7 s, so the unit enters
`failed` after ~3.5 minutes of refusals.

The sizing is a trade and was got wrong first time. A tighter bound (5 attempts / 120 s, ~35 s)
is right for the deterministic refusals, which never recover — but it converts a *transient*
failure, a DNS blip on `git fetch --tags`, from "self-heals unattended" into "down until someone
runs `systemctl start`". ~3.5 minutes is long enough for a real blip to clear and short enough
that a broken release stops thrashing. The service is down either way — the change is that it is *visibly* down,
stops thrashing git and uv against the network, and names the state in `systemctl status`.

**The placement is the part worth remembering.** `StartLimitIntervalSec`/`StartLimitBurst` moved
from `[Service]` to `[Unit]` in systemd 229 and are **silently ignored** under `[Service]`;
Ubuntu 24.04 ships systemd 255. The first draft of the fix put them in `[Service]`, where the
hardening would have been present in the file, absent in effect, and untested — a guard that
reads as protection and is not. Now pinned by
`tests/test_deployment_artifacts.py::TestRestartLoopIsBounded`, mutation-checked in both
directions.

Residual, deliberately not addressed here: a `failed` unit produces no alert of its own. Better
Stack notices via `/ping`, which is the same 3-minute signal as before — the incident just stops
being open-ended. An alert on unit state would be a monitoring change (ADR-032), not a unit one.

Cross-refs: **C-262** (the same box, the other unbounded resource), **C-266** (a stale tag is one
of the four refusals this used to loop on), **C-256**, ADR-022, ADR-032.

---

### C-281: The runbook labels which *user* to be and never which *host* — the third defect of this shape

| Field | Value |
|-------|-------|
| ID | C-281 |
| Tier | 4 |
| Source | operator, 2026-08-21 (a pasted release block failed on the laptop) |
| Trigger | When adding any command block to a runbook, state the host as well as the user — and when a third instance of a defect shape appears, treat the shape as the finding rather than fixing instance three and moving on. |
| Location | `deployment/RELEASE_RUNBOOK.md` (the "Every future release" block and the measurement section), `tests/test_falsify_shutdown_safety.py::test_runbook_blocks_naming_the_deploy_user_say_they_run_on_the_box` |

The recurring release block was labelled `# --- as your own user ---` and
`# --- then as the deploy user ---`. Both name the *account*; neither names the *machine*. The
first-stand-up section does say "From your **laptop**" and "Then SSH in", but that framing does
not carry into the block an operator actually reuses every release.

Pasted on a laptop it fails at the first line with `sudo: unknown user views-crafdapi-deploy`.
The failure is **safe** — nothing is changed, and the account exists only on the box — but the
reader has already been told to run it, and the block gives no cue that it was wrong.

Fixed: explicit `=== ON THE BOX ===` markers on every block that invokes the deploy account, a
sentence naming the exact error a laptop paste produces, and a guard asserting that any line
invoking `sudo -u`/`sudo -iu views-crafdapi-deploy` has a host marker within the preceding 25
lines. The guard's first version flagged prose that merely *mentions* the account and had to be
narrowed to lines that *invoke* it — recorded because an over-broad guard that fires on correct
text is how guards get disabled.

**The entry exists for the shape, not the instance.** This is the third runbook-text defect in
eight days: **C-265** (Step 7 stating the pre-delivery failure state as current), **C-266** (a
real-looking `TAG=v0.4.0` that pastes cleanly and deploys the wrong version), and now a block
that pastes cleanly on the wrong machine. All three read correctly and mislead in use, and all
three were found by someone following the document rather than reviewing it. Rule of Three: the
next runbook change should assume the reader is pasting without context, and say host, user, and
expected output for every block.

**Fourth instance, same day, different cause — the block does not have to be *wrong* to mislead.**
After the host markers above were added, the corrected block was pasted on the box and the deploy
still went sideways. `/version` returned:

```
{"version":"0.5.1","deployed_tag":"sudo systemctl restart views-crafdapi5"}
```

The deploy-tag file transiently contained pasted *command text*, and a later line of the same
paste executed as `sudo cp <src> /etsudo cp <src> /etc/systemd/system/` — a command carrying a
fragment of itself. Re-running the restart resolved it, and the end state was correct
(`v0.5.1`, tag file exactly `v0.5.1\n`, no debris in `/etc/systemd/system/`).

The mechanism is one this runbook already knew about in a narrower form. It warns "paste those
two lines **separately** — pasted together, `read` swallows the next line as the key" — scoped
to `read -rsp`. But **any** prompting command in a pasted block reads the terminal while the rest
of the paste is still buffered, and `sudo`'s password prompt is in every one of these blocks. The
existing warning was right about the hazard and wrong about its extent.

What makes this worth recording rather than filing as a typo: the *loud* outcome is fine. A
corrupted tag normally fails at the deploy gate, because `refs/tags/sudo systemctl restart
views-crafdapi5` does not resolve — fail-visible, exactly as designed. The dangerous case is the
quiet one, where corruption happens to spell **a different real tag** and the service deploys the
wrong version with every check green. That is precisely **C-266**'s failure mode reached by a new
route, which is why bounding it matters even though C-266 itself is resolved.

Two mitigations added, both cheap: the warning is generalised from `read` to any prompt, with the
`ssh -t <box> '...'` single-command alternative; and `od -c` on the tag file now sits between
writing it and restarting, so the content is confirmed before anything acts on it. Neither
prevents paste corruption — they make it visible before it deploys.

Also worth keeping: **`/version` caught this unaided.** It reported the garbage rather than
hiding it, which is what that endpoint exists for (C-167's tag/version dual source of truth). The
observability worked; the procedure did not.

Cross-refs: **C-265**, **C-266** (both RESOLVED, same shape — and C-266's failure mode is the one
this could reach quietly), **C-256** (one secret, several names — the same "reads fine, misleads
in use" family), **C-167** (the dual source of truth `/version` exists to expose).

---

### C-282: A released ingest change can lie dormant behind a valid disk-cache entry — including, right now, the one v0.5.1 exists for

| Field | Value |
|-------|-------|
| ID | C-282 |
| Tier | 2 |
| Source | deploy of v0.5.1 (2026-08-21) |
| Trigger | When releasing any change to how an artifact is *ingested* — the parquet decode, `ForecastDataset` construction, the value-dir layout — decide before deploying whether it must take effect immediately. If it must, bump `_VALUE_SCHEMA_VERSION` (which invalidates the cache) or clear the affected entries; a deploy alone does not run it. Also whenever "is the fix live?" is asked about anything on the ingest path. |
| Location | `src/views_crafdapi/managers/disk_cache.py::_derive_cache_schema_version` + `CACHE_SCHEMA_VERSION`, `src/views_crafdapi/data/value_format.py::_VALUE_SCHEMA_VERSION`, the disk gate at `src/views_crafdapi/managers/dataset_service.py:283-306` |

`CACHE_SCHEMA_VERSION` is derived from the meta sidecar layout and `_VALUE_SCHEMA_VERSION`, and
**deliberately not** from the code that produces the value — that decoupling is the C-138 fix, so
a class rename no longer invalidates a 3.5-week cache. The consequence is the mirror image: a
release that changes the *ingest path* but neither of those two inputs leaves every existing
value-dir valid, and the new code does not run until the entry is superseded by a newer upstream
artifact, expires at its 3.5-week TTL, or is cleared by hand.

**This is live today.** v0.5.1 ships the streamed historical ingest (C-263), and the box holds two
valid historical value-dirs (`063ff140…` written 2026-08-14, `a4efa5de…` written 2026-08-18) whose
TTLs run to roughly 2026-09-07 and 2026-09-11. `/version` reports `0.5.1`, every check is green,
and `historical_stream.stream_to_value` has not executed once. The service is running the new code
and the old path.

Nothing is wrong — the served numbers are byte-identical either way, which is the whole point of
that gate — but two things follow that are easy to get wrong:

- **"Deployed" and "in effect" are different questions on this path**, and only the first is
  answerable from `/version`. There is no surface that reports which ingest path last ran.
- **A measurement taken now measures the old path.** This is what makes #98's acceptance criterion
  subtle: restart-and-curl reproduces the *warm-disk* number (~6 G), not the cold start, and it
  looks like a spectacular result rather than a null one.

Tier 2 rather than 3 because the failure is silent and self-congratulatory: the natural way to
verify the fix confirms it, at the moment it is provably not running. Mitigated in
`RELEASE_RUNBOOK.md` (the measurement section now clears the entries first and says why), not in
code.

Named options, none taken here: bump `_VALUE_SCHEMA_VERSION` when the ingest path changes
(blunt — invalidates every partition, forcing a cold rebuild for every key on the next request);
record the producing code version in the value-dir meta and refuse a mismatch (precise, but a new
invariant on the hot path); or leave it manual and documented, which is the current position.

Cross-refs: **C-263** (the change this currently masks), **C-138** (the decoupling that causes it,
and was right), **C-278** (the other way the streamed path can silently not run), **C-236**,
ADR-011.

---

### C-283: The release ritual has an undocumented step, and skipping it turns `check-branch` red for a reason unrelated to the change

| Field | Value |
|-------|-------|
| ID | C-283 |
| Tier | 3 |
| Source | release of v0.5.1 (2026-08-21) |
| Trigger | Immediately after merging any `development` → `main` release PR — merge `main` back into `development` before opening the next PR of any kind. Also when `check-branch` fails: read whether it is objecting to *your* branch or to the release topology before merging past it. |
| Location | `.github/workflows/prevent_merge_when_branch_behind.yml:16-43`, `deployment/RELEASE_RUNBOOK.md` (which does not mention the step) |

Merging `development` → `main` creates a merge commit **on `main` only**. `development` is then
behind `main` by that commit, and `check-branch` — which asserts
`git merge-base --is-ancestor origin/$branch HEAD` — correctly fails the *next* release PR. The
remedy is to merge `main` back into `development` after each release, which the history shows has
been done before (`1fb30b6`, PR #95's merge commit, appears in `development`'s history between
PRs #94 and #96) but which **is written down nowhere**: not in the runbook, not in the workflow,
not in the contributor protocols.

On 2026-08-21 the step was skipped, PR #112 (`development` → `main`, v0.5.1) went red on
`check-branch`, and it was merged anyway. The content was unaffected — `git diff main development`
was empty, the trees were identical, and the failure was purely topological — but that was
established *after* the merge, not before.

The concern is not the red check; it is what the red check teaches. This repository has no branch
protection and no required status checks — `gh api …/branches/main/protection` returns
`404 Branch not protected` — so **every gate here is enforced socially**, and ADR-023 says so
explicitly ("the gate is enforced at PR review"). A check that fails predictably for a reason
unrelated to the change under review is precisely how a socially-enforced gate stops being
enforced. C-74 and C-76 already record what red-by-default costs this project.

Fixed for now by syncing (`3471b07`), so the next release PR starts green. The durable fix is one
line in the runbook's release section — write it down, since the convention exists and has simply
never been recorded — and it is deliberately not bundled into this entry's own branch.

Cross-refs: **C-74**, **C-76** (red-by-default signal loss), **C-266** (the other release-ritual
step that was documented but wrong), ADR-023 §"the gate is enforced at PR review".
