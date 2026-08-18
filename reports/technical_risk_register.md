# Technical Risk Register

| Register Info     | Details                                        |
|-------------------|------------------------------------------------|
| Project           | views-crafdapi                                 |
| Owner             | Simon Polichinel von der Maase (simmaa@prio.org) |
| Last Updated      | 2026-08-15                                     |
| Total Concerns    | 26                                             |
| Open Concerns     | 25                                             |
| Resolved Concerns | 1                                              |
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

ADR-010 declares a register at `reports/technical_risk_register.md` as "a first-class governance artifact" and the single sink for all audit output. The file did not exist in this clone until this entry was written; `reports/` contained only `ops/betterstack_monitoring.md`. Meanwhile the source is unusually rich in register cross-references — `C-36`, `C-50`, `C-66`, `C-70`, `C-71`, `C-72`, `C-86`, `C-137`, `C-138`, `C-146`, `C-148`, `C-149`, `C-153`, `C-155`, `C-166`, `C-169`–`C-172`, `C-231`, `D-12`, `D-21`, `D-24` and more — and ADR-033 performs a formal register reconciliation at ratification. Every one of those citations is currently a dangling pointer. Two concrete consequences beyond lost context: the ID namespace had to be defended by hand (see the ID Namespace Note above), and three falsification probes now **xpass** (`test_falsify_priogrid_naming.py::TestP1…`, `test_falsify_shim_diagnosis.py::TestP1…` and `::TestP4_CICDocumentsShimAsContract::test_fao_pgm_cic_does_not_guarantee_priogrid_gid`) — meaning concerns recorded as open in the C-61..C-65 cluster may in fact be closed, with no artifact in which to record that.

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
| Location | `README.md:17-19` |

The README pins the Appwrite Seam Contract at `platform-001-v1.2.0` and describes a "v1.3.0 rename untagged" — stale: views-appwrite now publishes `appwrite-seam-v1.5.x` tags (current `v1.5.2`) and the contract file was renamed `PLATFORM-001` → "The Appwrite Seam Contract". S1 added a D2 binding note directly below (correctly citing `v1.5.2`), so the two now visibly disagree. **Doc-drift, no runtime effect** — the D2 binding pins `v1.5.2` in code (`seam_contract.REGISTRY_PIN_TAG`), not the README. crafd's equivalent of views-faoapi#340; deliberately out of S1 scope (contract-version tracking is its own concern). Named fix: a crafd doc-hygiene pass refreshing the README seam pin to the current published tag + renamed contract, mirroring faoapi#340.

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
