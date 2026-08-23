# What to subtract

*Written 2026-08-23, after a week that added seven register entries, three reports and five issues
and fixed nothing. This asks the opposite question: **what can come out?** Every claim below is
measured in this session. Tier 1 was executed on the branch that carries this file; Tiers 2–5 are
written down and deliberately left alone.*

---

## Start with what is **not** the answer

Two things look like the obvious targets, and both are load-bearing. Establishing that is worth as
much as the deletions, because it stops the next person spending a day rediscovering it.

**`data/handlers/` cannot go.** `grid_dataset.py` (1,405 lines) and `forecast_dataset.py` (966) are
the largest files in the repo and look like migration residue. They are not. ADR-030's S8
acceptance criterion — *"retire the `_ViewsDataset → _PGDataset` chain"* — was **formally reneged
and amended** (register **D-21**):

> "the chain was **rationalized, not deleted**... a full single-class merge was rejected... the
> 'delete the chain' AC was an over-specified *means* and is amended here."

and the closeout keeps it explicitly: *"`data/handlers.py` stays on the ratchet allowlist as the
DataFrame-returning serving boundary."*

**The legacy/wire split cannot go either.** It is not transition debt. It is the live serving path
for historical — *"Historical still arrives as loose files until the producer co-delivers it on the
wire path"* — and the fail-safe degrade path for forecasts. Removing it is blocked on a change in a
repository we do not own.

---

## Tier 1 — done on this branch

| removed | evidence | |
|---|---|---|
| `wandb==0.18.7` + `src/views_crafdapi/wandb/` + `APIManager.run()` | only caller of the package was `run()`, which is **never invoked** — the entry point is `uvicorn ... api:create_app --factory` | **53 MB** |
| `src/views_crafdapi/shapefiles/` | C-239 code-dead, re-verified: the only `gpd.read_file` is `plotting.py:58`, fetching Natural Earth from a URL | 672 KB |
| `fastparquet==2024.11.0` | zero references, including lazy imports | 7.6 MB |
| `tqdm==4.67.1` | zero references | — |
| `art==6.4` + `ModelManager.__ascii_splash()` | one lazy import, printing randomly-coloured ASCII art to stdout on boot — into journald | — |
| `joblib==1.5.2` + `_GridDataset.tqdm_joblib` | the method is **never called**; `joblib` was imported solely to support it | — |

**venv 692 MB → 604 MB (−88 MB).** Suite 1081 passed (−8: the deleted `test_wandb_redaction.py`).

The `joblib` removal came out of the final leftovers sweep rather than the plan: grepping for stray
`tqdm` references found `_GridDataset.tqdm_joblib`, a static method that patches joblib's progress
reporting into a tqdm bar and is called from nowhere. `joblib` was imported for that one method and
nothing else — so a dead method took a runtime dependency with it.

**Two things this surfaced that were not in the plan.**

*My "zero imports" evidence was wrong for `art`.* It is imported lazily *inside* a method
(`model.py:546`), and the first measurement only caught module-level imports. The boot test caught
it. Removing the splash is still right — it is decoration on a production service — but it is a
**behaviour change** (boot log output), not the no-op the other four were.

*Removing `wandb` fired a trigger the register had already written.* **C-271** said, in as many
words: *"when a transitive dependency stops requiring one of these — e.g. `wandb` dropping
`PyYAML`"*. It did. The suite went to **30 collection errors** with `ModuleNotFoundError: No module
named 'yaml'`, on the production boot path C-271 named. `PyYAML`, `requests` and `pydantic` are now
declared explicitly. A subtraction that forced three honest additions, and the register predicted it.

**A side effect worth naming:** deleting the shapefiles cleared a documented **public-release
blocker**. `test_falsify_path_to_public.py::test_no_unlicensed_thirdparty_data_bundled_for_public_release`
was `xfail` on exactly that third-party geodata and is now a **live passing guard**.

Resolved by deletion: **C-239**, **C-271**, **C-273**. The register went from 5 resolved to 8 — the
first movement in the right direction this week.

---

## Tier 2 — a write surface no route can reach

`PredictionStoreManager` exposes **15 public methods; the serving path calls 7.** The eight unused
include `upload_predictions`, `update_prediction_metadata`, and **`delete_prediction`**.

Coverage corroborates — the least-executed modules in the repo:

```
managers/appwrite/metadata.py   31%   (154 of 234 statements never executed)
managers/appwrite/manager.py    51%   (188 missed)
managers/model.py               67%   (88 missed)
```

`metadata.py` at 31% is the collection-provisioning machinery, and it is the same file carrying the
`Role.any()` defect tracked as **#123** and **#91**. **Deleting the provisioning path would close
that issue by removing the code rather than fixing it** — which is the strongest argument in this
document for subtracting rather than adding.

Left alone deliberately: it removes a destructive method and intersects two open security issues, so
it deserves its own change and its own verification, not a ride-along with a dependency cleanup.

## Tier 3 — rearrangement

- **Break the `data ↔ forecast` cycle** (**C-289**). The back edge is three imports, two of which
  are one constant (`_VALUE_SCHEMA_VERSION`).
- **Resolve the S7 duplication** (**C-260**, issue **#100**, open with zero progress since
  2026-08-18). `forecast/aggregate/reduction.py::joint_sum_to_level` and
  `forecast_dataset.py::_frame_native_joint_sum` already **differ in three known ways** — the
  code→unit mapping, the missing-code predicate, and float width — and nothing proves they agree.
- **Split `managers/model.py`.** `ModelPathManager` is training-pipeline scaffolding;
  `APIManager`/`APIPathManager` is what `api.py:1` needs. `pyprojroot` leaves with it.

## Tier 4 — retire a served surface

`/data/{category}/latest` is hollow (**C-232**), fatal at scale (**C-284**), returns **88 MB of
valueless rows** in 9 s, and is used by no consumer. C-284 already calls retirement *"probably right
and the largest change"*.

**Retiring it deletes a Tier 1 defect instead of fixing it.** Scoped here, not done: removing a
documented public route deserves a deliberate decision, not a side effect of a cleanup branch. If
retirement is declined, **#125** still owns the bounded-response fix.

## Tier 5 — prose that actively misleads

- `tests/test_pandas_ratchet.py:9-10` — *"as `data/handlers.py` is dissolved… it drops out of the
  allowlist"*. Contradicted by its own `_ALLOWED` set eighteen lines below, which hard-codes both
  files permanently. **The dissolution it describes was cancelled by D-21.** One file disagreeing
  with itself.
- The `[tool.ruff]` comment citing "risk register C-34" — a **views-faoapi** entry. Already on #101.
- **C-291**: 38 ADRs, none ever marked superseded, across the entire ADR-030 migration.

---

## What this does not claim

- **Not that the repo is now lean.** 86 MB and one dead directory is a start, not a fix. The five
  oversized files (C-292) are untouched.
- **Not that Tier 2 is safe to delete as-is.** Nothing was checked for external callers outside this
  repo; `PredictionStoreManager` is plausibly used by a producer.
- **Not that the coverage percentages mean dead code.** Low coverage means untested, which
  *suggests* unused; the reachability analysis is the actual evidence, and it was run separately.
- **Not that `/latest` has no users.** No consumer we know of calls it. That is weaker than "unused".

## Cross-references

**C-239** · **C-271** · **C-273** (all resolved here) · **C-260** · **C-289** · **C-291** ·
**C-292** · **C-232**/**C-284** · **D-21** · issues **#84**, **#91**, **#100**, **#101**, **#123**,
**#125** · ADR-030 §6-§8 and its 2026-06-28 closeout ·
`reports/architecture/measured_against_the_siblings.md` ·
`reports/architecture/what_the_api_actually_serves.md`
