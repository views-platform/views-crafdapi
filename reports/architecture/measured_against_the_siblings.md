# Measured against the siblings

*Written 2026-08-23, comparing `views-crafdapi` against `views-frames`, `views-bayesian` and
`views-datafactory` across nine dimensions. Every number below comes from a command run in this
session or a file quoted directly. Where a claim I expected to make turned out to be false, it is
recorded as false rather than dropped.*

*The comparison deliberately excludes `views-faoapi`, which is treated separately in
`reports/ops/same_defect_next_door.md`.*

---

## The control that has to come first

The three comparators are not the same kind of thing as this repo, and several of their practices
are justified by their role rather than by being better:

| repo | what it is | what that justifies |
|---|---|---|
| **views-frames** | a frozen, published library leaf (PyPI, v2.0.0) at the root of the platform's dependency graph | 100% coverage, a four-version CI matrix, a governed conformance floor |
| **views-datafactory** | a five-stage batch pipeline producing a ~35 GB grid | artifact-boundary isolation between stages, append-only provenance |
| **views-bayesian** | a research repo with a config-driven runner and an experiment sandbox | a two-tier typing standard, a carve-out for volatile code |
| **views-crafdapi** | a live HTTP service on a shared box | — |

None of the three is a serving API. So every recommendation at the end is an argument, not a
demonstration.

---

## 1. Governance — the sharpest number in this report

**Does the risk register converge, or only accumulate?**

| repo | assigned | open | resolved | resolved |
|---|---|---|---|---|
| views-frames | 94 | 10 | 84 | **89%** |
| views-datafactory | 354 | 43 | 308 | **87%** |
| views-bayesian | 20 | 8 | 12 | 60% |
| **views-crafdapi** | **57** | **52** | **5** | **9%** |

Read individually from each register header, not from one grep.

The siblings work theirs down. Ours does not. And the two that converge have machinery for it that
we lack:

- **datafactory** strikes resolved entries through in place — `| ~~C-253~~ | ~~1~~ | ~~Export
  scripts have no source-digest verification...~~ | Resolved 2026-06-09 (commit 975b401...)` — and
  moves them to `archive/technical_risk_register_resolved.md`. Its header is explicitly capped at
  one sentence, with narrative pushed to a separate `register_changelog.md`, guarded by a test.
- **frames** added a `Status: actionable | awaiting — <precondition>` field in 2026-07, for exactly
  our problem: a raw open-count had stopped distinguishing "do this" from "cannot yet".

**Our 52 open entries carry no such distinction.** Several are blocked on the operator, several on
a measurement that needs the box, several are genuinely actionable — and nothing in the register
separates them.

### ADR lifecycle

We have 38 ADRs, 36 of them under `docs/ADRs/active/`. The status vocabulary in use:

```
 26  Accepted        (in two spellings, with and without trailing space)
  5  Implemented
  3  Proposed
  1  Active
```

**No ADR is marked Superseded or Deprecated.** The only occurrence of those words is the template's
own placeholder. There is an `active/` directory with no counterpart directory, so nothing has ever
been retired — across a period that included the whole ADR-030 representation migration.

frames, by contrast, retains superseded ADRs in place and marks them: *"Accepted — superseded in
part by ADR-028 (2026-08-18); see Amendment"*, and ADR-002 carries a dated in-body amendment
recording that its declared dependency direction had been **wrong** and was corrected to match the
code rather than the other way round.

### What CI gates

Every `run:` step in every workflow here:

```
prevent_merge_when_branch_behind.yml   (3 shell steps — branch topology)
codeql.yml                             (1)
run_pytest.yml                         uv sync · pytest · coverage summary · nbmake
```

**No lint step. No type check.** Compare:

- **frames** — 7 jobs: `ruff check`, `mypy src/`, `pytest --cov-fail-under=100` across Python
  3.10–3.13; a `floor` job running `mypy --strict` and pytest against a pinned `numpy==1.26.4`;
  `ruff format --check`; `validate_docs.sh`; every `examples/*.py` executed; `lint-imports`.
- **datafactory** — `ruff check src/ tests/ scripts/`, `mypy src/`, **`uv lock --check`** before
  sync (so CI cannot silently repair a stale lock), `validate_docs.sh`, and an import-enforcement
  job.

---

## 2. Consistency — we are the outlier by omission

`[tool.ruff]` here contains an exclude and nothing else. With no `lint.select`, ruff runs its four
default rule groups and reports **"All checks passed!"**

Under the ruleset **frames and bayesian both use, character for character** —
`select = ["E", "F", "I", "N", "UP", "B", "A", "C4", "SIM"]`, with `line-length = 88` — the same
tree has **3,718 findings**. datafactory uses the same selection plus one ignore. Three of four
repos agree; this is the platform's house standard and we are outside it by omission, not by
choice.

Breakdown, so the number is not alarming for the wrong reason:

- **3,034 are `E501`** line-length, against a default 88 that was never deliberately set. Choose a
  line length on purpose and most of this evaporates.
- Excluding it: **~684, of which 506 are auto-fixable** (`UP006`/`UP045` typing modernisation,
  `I001` import order).
- Substantive remainder: **22 × `B904`** (`raise` without `from` inside `except` — drops
  `__cause__`, 8 of them in `api.py`), **9 × `B905`** (`zip()` without `strict=`). I read all nine
  `B905` sites: **none is a live bug** — lengths are provably equal at each. But it is the exact
  silent-misalignment class this register is otherwise full of, and `strict=True` is free.
- **14 × `B008`** are FastAPI `Depends()`/`Query()`. Idiomatic; must be ignored, not fixed.

`mypy` is installed (1.19.1) and unconfigured. At non-strict it reports **62 errors in 14 files**.
frames and bayesian both run `strict = true`.

### Error handling

We have at least four idioms live at once: stdlib raises (**99 × `ValueError`**, 8 × `TypeError`),
**36 × `HTTPException`**, five custom exception types (`ProvisioningError`,
`ProvisioningDisabledError`, `NotStreamable`, `ImplausibleArtifact`, `_Halt`), and result objects
(`selection.{Served,Refused,NoRun}`). Plus **67 × broad `except Exception`**.

frames and bayesian each have **zero** custom exception classes and raise stdlib types uniformly.

For a service this is not automatically wrong — an HTTP boundary genuinely needs a different idiom
from a value-object leaf, and `NotStreamable` exists to signal a *precondition failure that should
fall back* rather than an error. But 67 broad catches is the mechanism silent failures ride on, and
none of this is written down as a decision anywhere.

---

## 3. Cohesion — the clearest structural gap

| | src LOC | files | largest file | `utils.py` |
|---|---|---|---|---|
| **crafdapi** | 12,813 | 66 | **1,405** | **1** |
| frames | 3,843 | 36 | 383 | none |
| bayesian | 12,602 | 62 | 548 | none |
| datafactory | 16,587 | 89 | 743 | none |

Our five largest — `grid_dataset.py` 1,405, `api.py` 1,288, `manager.py` 1,077,
`dataset_service.py` 1,043, `forecast_dataset.py` 966 — are **each larger than any file in any
sibling.**

`api.py` is not one concept at any readable grain: **14 route decorators and 17 nested function
definitions, all inside a single `_register_routes` method.** frames' largest file is 383 lines and
holds solely `SpatioTemporalIndex`.

We are also the only one of the four with a `utils.py` (`wandb/utils.py`). The siblings' near-misses
are instructive rather than hypocritical: datafactory's only hit is `sources/_ucdp_common.py`, 36
lines, underscore-private, holding one validator shared by three UCDP harvesters and documented as
to why; frames' is `_common.py`, 22 lines, two functions.

### A file whose name actively misleads

`managers/model.py` is 819 lines. `api.py:1` imports `APIManager, APIPathManager` from it, so it is
firmly on the serving path. It also holds `ModelPathManager` — model-directory scaffolding
inherited from the training-pipeline ancestor — and carries `wandb==0.18.7` as a declared
dependency of a **read-only** API that trains nothing.

A file called `model.py` in a forecast-serving API that turns out to be path management is the
"screaming architecture" failure in its purest form.

---

## 4. Coupling — a live cycle, and nothing was watching

Measured directly by AST-walking every import in `src/`:

```
(root)    -> data, forecast
data      -> forecast
forecast  -> data
managers  -> (root), data, forecast, wandb

MUTUAL (cyclic) subpackage pairs: [('data', 'forecast')]
```

**`data` ↔ `forecast` is cyclic.** The forward edge is heavy — `data/handlers/grid_dataset.py`
pulls 8+ modules out of `forecast/`. The back edge is exactly three imports, and that is the whole
of it:

```
forecast/ingestion/historical_stream.py:59: from views_crafdapi.data.value_format import _VALUE_SCHEMA_VERSION
forecast/ingestion/wire_reader.py:48:       from views_crafdapi.data.value_format import _VALUE_SCHEMA_VERSION
forecast/ingestion/wire_reader.py:49:       from views_crafdapi.data.handlers import ForecastDataset
```

Two of the three are one constant. Narrow enough to break deliberately rather than by refactor.

**All three siblings are acyclic and enforce it**, by three different mechanisms — which is itself
worth noting, because it means there is no single blessed answer:

- **frames** — `[tool.importlinter]` with two layered contracts, run as its own CI job, *plus* a
  deliberately duplicated stricter pytest. The duplication is documented as intentional: *"Do not
  delete the test believing this replaces it."*
- **datafactory** — ~130 lines of stdlib `ast` with an `ALLOWED_INTERNAL_IMPORTS` dict, plus a
  second test asserting **the declared graph itself is acyclic** — guarding the case where the
  declaration is wrong rather than the code. Its stages communicate only through files on disk:
  *"The filesystem is the API between layers. Each dashed line represents zero code coupling."*
- **bayesian** — hand-written AST tests per layer, with each `__init__.py` docstring declaring its
  own may-import/must-not-import list.

This answers open issue **#90** — *"is the import topology declared, enforced, and is the
declaration itself checked?"* — **no, no, and no.**

---

## 5. Where the criticism I expected turned out to be wrong

I went in expecting to find that our tests assert shape and status and never look inside the
payload, because **C-243** and **C-232** both say so. Classified across the whole suite:

```
shape/status-only asserts:  439
content/value asserts:      565     -> 56% content
```

`test_api_endpoints.py` alone is **68% content-asserting**. That is not a shape-only test culture,
and the blanket version of the criticism is **false**. Our test:src ratio is 1.40 — beside frames'
1.44, and more than ten times bayesian's 0.12.

**What is true is narrower and worse.** The only two tests that touch `/data/{category}/latest` are:

```python
def test_historical_latest_returns_200(self, app_client):
    resp = client.get("/data/historical/latest", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "dataframe" in body["data"]

def test_forecast_latest_returns_200(self, app_client):
    resp = client.get("/data/forecast/latest", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["category"] == "forecast"
```

Named `*_returns_200`, and that is precisely all they check. **No test anywhere asserts that a
`/latest` payload carries values.** So the problem is not test culture — it is that the single
most dangerous served surface is the one left untested. That is a more specific and more actionable
finding than "the tests are weak", and C-243 should be narrowed to say it.

---

## 6. Where we are ahead, and where the siblings are not models

The siblings are not uniformly better, and a report that implied otherwise would be useless.

- **views-bayesian's CI is red as configured.** Eleven falsification stubs fail as ordinary
  unmarked tests, so `uv run pytest` returns nonzero. It has no coverage configuration at all, a
  0.12 test:src ratio, and **2.1 MB of `graphify-out/` committed across 116 tracked files**. We are
  substantially ahead of it on testing and on not committing tool output.
- **views-datafactory's README documents a `CLAUDE.md` that does not exist**, and it carries stale
  `dist/` wheels from `1.9.0.dev1` against a `1.12.0` `pyproject`. No coverage threshold anywhere.
- **views-frames' own maintainers warn against its governance weight** — roughly 9,400 lines of
  governance prose against 3,700 lines of source. Its `CLAUDE.md` instructs agents *not* to run
  discovery tooling on it, because *"prose that large is never perfectly self-consistent... that is
  how a one-line CI addition became a day-long sprint."*

That last one is the most important sentence in this report for our purposes. The repo with the
best numbers is on record saying its own process volume generates false work.

---

## 7. What is worth adopting — and what is a trap

Filtered through: does it earn its overhead **on a serving API**?

### Adopt

1. **The house ruff ruleset, and a lint + type job in CI.** Three of four repos already agree on
   the exact selection. Staged so it is reviewable: set `line-length` and the ruleset with `E501`
   deferred → autofix the 506 → hand-fix `B904`/`B905`, ignore `B008` → add the CI steps → mypy
   non-strict first. Cost: minutes of CI, one-time cleanup.
2. **An import-topology check.** We have a cycle and no guard, and #90 asked about this a week ago.
   datafactory's stdlib-`ast` version is the right size — no new dependency, and its second test
   (the declared graph is itself acyclic) catches the failure mode where the declaration is wrong.
   We already have this shape in `test_pandas_ratchet.py`; this points the same idea at a second
   contract. Cost: ~130 lines, plus the architecture conversation needed to write the allow-list —
   which is the point.
3. **A `Status: actionable | awaiting — <precondition>` field on register entries.** frames added
   it for exactly our situation. With 52 open and 9% resolved, the open-count currently carries no
   information. Cost: one field, applied once.

### Decline

- **`--cov-fail-under=100`.** frames earns it as a frozen published leaf whose API cannot move. On
  a service it buys ritual — and would be satisfied by exactly the `*_returns_200` tests that
  missed C-232.
- **A four-version CI matrix.** We deploy one Python to one box.
- **The full ADR/CIC apparatus and cross-repo conformance vectors.** These pay off at more than one
  consumer; we have one. frames' own `CLAUDE.md` is the argument against.
- **Committing falsification stubs as ordinary failing tests** (bayesian's pattern). It leaves CI
  red, and a permanently-red gate stops carrying information — a point datafactory makes explicitly
  about its own non-blocking monitors.

---

## What this report does not claim

- **Not that better governance caused better outcomes.** frames and datafactory are healthier *and*
  carry heavier process. This shows correlation and mechanism, not causation — and frames' own
  warning is evidence the arrow does not always point that way.
- **Not that the sibling practices survive contact with a serving API.** None of the three is one.
- **Not that the 3,718 ruff findings are 3,718 problems.** 3,034 are line length against a default
  nobody chose.
- **Not that our tests are weak.** Measured, they are not. Two specific tests are.
- **Not that the `data ↔ forecast` cycle has caused a defect.** It has not, as far as anything
  recorded shows. It is an unguarded property, not an incident.

## Where it stands

| finding | state |
|---|---|
| register at 9% resolved, no `actionable`/`awaiting` distinction | new here |
| no lint or type gate in CI; ruleset unset | new here |
| `data ↔ forecast` import cycle, topology undeclared | new here — answers **#90** |
| `/latest` tested only for `200` | narrows **C-243**, confirms **C-232** |
| five files larger than any sibling's largest | new here |
| `model.py` / `wandb` vestigial weight | new here |
| no ADR ever marked superseded | new here |

## Cross-references

`reports/ops/same_defect_next_door.md` · `reports/ops/declared_vs_in_effect.md` · **C-232** ·
**C-243** · issues **#90**, **#101**, **#125** · views-frames `pyproject.toml`, `GOVERNANCE.md`,
`CLAUDE.md`, ADR-002/018/028 · views-datafactory `tests/test_import_enforcement.py`, ADR-020,
ADR-037, ADR-041 · views-bayesian `tests/test_layer_boundaries.py`, ADR-002.
