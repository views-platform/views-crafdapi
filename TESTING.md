# Test Architecture

The test suite has **1,295 tests across 103 files** (verified 2026-10-16 via `uv run pytest --collect-only -o addopts=""`), organized in 5 layers. *Earlier figures here — 674 across 53 (2026-06-26), then 1,126 across 95 (2026-08-15) — each drifted within weeks; a count nobody re-derives is a claim, not a measurement (ADR-014 §1).* Each layer catches a different class of bug; removing any layer creates a blind spot.

> **Counts drift.** The totals below are a dated snapshot. Re-verify with the commands in [Verifying the counts](#verifying-the-counts) rather than trusting a stale number — and update this file when a story changes them.

## Layer totals

| Layer | Marker | Red/Beige/Green (ADR-005) | Tests | Proves |
|-------|--------|---------------------------|-------|--------|
| 1 — Storage I/O | `layer1_storage` + `integration` | Beige / Red | 30 | Files upload, download, round-trip byte-identical; metadata dedup; cache hit/miss; session auth; failure modes return `OperationResult(success=False)`. **Cannot** prove fetched data is correct/served correctly. |
| 2 — Data Processing | `layer2_data` | Green / Beige | 427 | DataFrames validated; aggregation math; MAP/HDI statistics; format-detection cascade; the `forecast/` package (ingestion, frames, summarize, geography, aggregate, serialize, conformance). |
| 3 — HTTP API Contract | `layer3_http` | Beige / Red | 128 | Endpoints exist, correct status codes, response shapes match contract, query parsing, auth, error→HTTP-code mapping. |
| 4 — Infrastructure | `layer4_infra` | Green / Beige | 514 | SDK normalization, cache bounds, import safety, deployment config, model-config loading, disk-cache versioning + concurrency. |
| 5 — Audit & Regression | `layer5_audit` | Red | 57 | Prior bugs stay fixed; documented gaps tracked; SDK-compat assumptions hold; the `test_falsify_*` falsification suite. |
| *(unmarked)* | *(none)* | — | 139 | Tests without a layer marker — many `test_falsify_*` probes, `test_client`, `test_time`, `test_cache_isolation`, etc. See [Marker hygiene](#marker-hygiene). |
| **Total** | | | **1,295** | of which **49 are deselected by default** (`integration`). |

The red/beige/green column is the ADR-005 taxonomy (adversarial / realistic-misuse / supportive); the `layerN` markers are the operational selectors. See `docs/ADRs/active/005_testing_as_mandatory_critical_infrastructure.md` for the taxonomy definitions and the five named beige decision-support scenarios.

## Default run shape

`uv run pytest` (which applies `addopts = "-m 'not integration'"`) is **green-by-default**:

```
1212 passed, 10 skipped, 49 deselected, 23 xfailed, 1 xpassed, 0 failed
```

- **deselected (49):** the `integration` tests — need live Appwrite creds (below).
- **xfailed (23):** falsification probes that *document a genuinely-open concern* — 7 of them the priogrid-naming cluster (C-61..C-65), the rest spread across the dual-SDK, rename and provenance probes. An `xfail` here is expected, not a failure.
- **skipped (10):** tests whose required *environment/resource* is absent (e.g. a sibling repo, see the marker below; or a cached artifact).
- **xpassed (1):** the views-datafactory naming probe, which passes wherever that sibling is checked out (it skips in CI); see its comment.

*Measured 2026-10-16 on a maintainer machine with sibling repos and the delivered run present; a clean/CI checkout skips more and xpasses none.*

A **failure** (not xfail, not skip) is the only red signal that should ever stop a merge.

## Coverage

**Baseline: 81%** (line + branch), measured 2026-10-16 on the default non-integration suite (`src/views_crafdapi`, 5058 statements / 1666 branches). Coverage is a **signal, not a gate** — there is deliberately **no `fail_under`** (a hard coverage gate would recreate the red-by-default trap C-74/C-76 warn against). Config lives in `[tool.coverage.*]` in `pyproject.toml`.

```bash
# Measure coverage (term report with missing lines)
uv run pytest --cov=src/views_crafdapi --cov-report=term-missing

# HTML report
uv run pytest --cov=src/views_crafdapi --cov-report=html   # -> htmlcov/index.html
```

Well-covered: the `forecast/` package (mostly ≥90% — the value-object pipeline), `client.py` (95%), `managers/disk_cache.py` (95%), `managers/prediction/` (86–100%). Modules below 80%:

| Module | Cover |
|--------|-------|
| `managers/appwrite/metadata.py` | 45% |
| `managers/appwrite/manager.py` | 51% |
| `smoke.py` | 66% |
| `managers/model.py` | 72% |
| `managers/appwrite/file_cache.py` | 74% |
| `data/handlers/grid_dataset.py` | 75% |
| `managers/api.py` | 77% |
| `forecast/frames/builder.py` | 78% |
| `managers/log.py` | 78% |

*(The earlier table cited issues #78/#81 as trackers; in this repository those numbers resolve to unrelated work, inherited from views-faoapi — register C-241 — and are dropped.)*

Re-measure and update the baseline when a story moves it materially.

## Test-marking convention

Use the **narrowest** marker that states *why* a test is not an ordinary green test. Do not conflate "documents an open bug" with "can't run here".

| Marker | Meaning | Use when |
|--------|---------|----------|
| `@pytest.mark.layerN_*` | Which architectural layer the test belongs to (1–5). | Always — pick the layer by *what the test proves*, not what it imports. |
| `@pytest.mark.integration` | Needs live Appwrite credentials. | The test makes real network calls to Appwrite. Deselected by default. |
| `@pytest.mark.requires_sibling_repos("repo", …)` | Needs a sibling repo's **source on disk** under `PLATFORM_ROOT` (the monorepo parent). | A cross-repo falsification probe inspects e.g. `views-pipeline-core` source. A conftest hook (`pytest_runtest_setup`) **skips** the test cleanly when the named repo is absent — so the suite stays green in a clean/CI checkout. Introduced in S1 (#126). |
| `@pytest.mark.xfail(strict=False, reason=…)` | The test **documents a genuinely-open concern**; it is *expected to fail* until the concern is resolved. | The behaviour is wrong-but-tracked (cite the register C-xx). With `strict=False`, an unexpected pass (xpass) is tolerated. **Not** for environment problems — use `skip`/`requires_sibling_repos` for those. |
| `pytest.skip(reason=…)` (in-body) | The test cannot run here because a **resource is absent**, or it is **superseded** by a better test. | A required file/data subdir is missing, or the assertion moved elsewhere. Prefer the declarative `requires_sibling_repos` marker over an ad-hoc in-body `skip` for whole-repo dependencies. |

**Rule of thumb:** environment-absent → `skip`/`requires_sibling_repos` (it *can't* run); behaviour-wrong-but-tracked → `xfail` (it *should* run and fail). Keeping these distinct is what makes the green/red bar trustworthy (register **C-74**).

## Marker hygiene

139 tests currently carry **no** `layerN` marker (2026-10-16). This is a known backlog, not a convention: new tests **must** carry a layer marker (below). Backfilling the unmarked set is tracked separately; until then, `pytest -m layerN` undercounts the true suite, so use the total from `--collect-only` for headline figures.

## What happens if you remove a layer

| Layer removed | Consequence |
|---------------|-------------|
| Layer 1 (storage) | No proof Appwrite works over the network. All mock-based tests could pass while production silently fails to connect. |
| Layer 2 (data) | No proof aggregation math is correct. Somalia could get Ethiopia's forecast. HDI intervals could be systematically too narrow. |
| Layer 3 (HTTP) | No proof endpoints return correct responses. Routes could be broken, status codes wrong, JSON serialization failing. |
| Layer 4 (infra) | No proof caches have bounds, SDK normalization works, deployment config is valid, imports are safe. |
| Layer 5 (audit) | No tracking of known gaps and prior bugs. Regressions could silently return. |

## Running tests

```bash
# All non-integration tests (default; green-by-default)
uv run pytest

# By layer
uv run pytest -m layer2_data
uv run pytest -m "layer3_http and not integration"

# Integration tests (need APPWRITE_* env vars)
uv run pytest -m integration

# Show why things were skipped/xfailed
uv run pytest -rsx
```

Integration tests require `APPWRITE_ENDPOINT`, `APPWRITE_DATASTORE_PROJECT_ID`, `APPWRITE_CRAFD_BUCKET_ID`, `APPWRITE_METADATA_DATABASE_ID`, `APPWRITE_CRAFD_COLLECTION_ID`, and `APPWRITE_DATASTORE_API_KEY`. They are excluded by default via `addopts = "-m 'not integration'"` in `pyproject.toml`.

### Verifying the counts

```bash
# Total suite (incl. integration)
uv run pytest --collect-only -q -o addopts="" | tail -1

# Per layer
for m in layer1_storage layer2_data layer3_http layer4_infra layer5_audit; do
  echo -n "$m: "; uv run pytest --collect-only -q -o addopts="" -m "$m" 2>/dev/null | tail -1
done
```

## Adding new tests

1. Determine which layer your test belongs to based on what it *proves* (not what it imports).
2. Add `pytestmark = pytest.mark.<layer_marker>` at module level.
3. If the test requires live Appwrite, also add `pytest.mark.integration`.
4. If it inspects a sibling repo's source, add `@pytest.mark.requires_sibling_repos("<repo>")` instead of an in-body existence check.
5. If it documents a known-open bug, use `@pytest.mark.xfail(strict=False, reason="… (register C-xx)")` — not `skip`.
6. Do not remove a test file without understanding which layer it covers and what correctness property would be lost.
