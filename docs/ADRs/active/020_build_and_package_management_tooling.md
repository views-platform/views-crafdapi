# ADR-020: Build and Package Management Tooling

**Status:** Accepted  
**Date:** 2026-05-29  
**Deciders:** Project maintainers  

---

## Context

views-faoapi was bootstrapped in 2024 with poetry-core as its build backend and Poetry as its package manager. This was consistent with the majority of VIEWS platform repositories at the time.

In practice, Poetry was barely used. The project had no `poetry.lock` file, the `[tool.poetry]` section contained only a single `packages` directive, and the Makefile invoked pytest directly via `PYTHONPATH=src pytest` rather than through `poetry run`. The CI workflow (`run_pytest.yml`) was the only component that meaningfully depended on Poetry — installing it via curl on every CI run to execute `poetry install` and `poetry run pytest`.

A second workflow (`publish_package.yml`) was copied from another repository at bootstrap time but never adapted: it read version metadata from `tool.poetry.version` (a field that did not exist in this project's `pyproject.toml`), compared versions against the wrong PyPI package (`views-pipeline-core` instead of `views-faoapi`), and used `poetry publish --build`. This workflow was non-functional from the day it was committed. views-faoapi has never been published to PyPI.

The immediate trigger for this decision was a practical problem: tests failed when run outside the base development environment because domain-specific conda environments (used for other VIEWS model libraries) lacked views-faoapi's dependencies (fastapi, appwrite). The project needed a dedicated, reproducible environment with a lock file — something Poetry could provide via `poetry.lock`, but which had never been set up.

Meanwhile, 4 of the 13+ VIEWS platform repositories had already migrated to hatchling + uv (views-datafactory, views-lab00, views-bayesian, views-metric-lab). A broader conda-to-uv migration for views-models is also under investigation. Rather than invest in configuring Poetry properly for the first time, the decision was to align with the direction the platform is heading.

---

## Decision

This repository uses **hatchling** as its build backend and **uv** as its package manager.

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Developer commands use `uv` directly:

```bash
uv sync              # Install/update all dependencies
uv run pytest tests/ # Run tests
uv run ruff check .  # Lint
uv build             # Build wheel and sdist
```

Dependencies are declared using PEP 621 (`[project.dependencies]`) and PEP 735 (`[dependency-groups]`). The `uv.lock` file is committed to version control.

**In scope:** Build backend, package manager, dependency declaration format, lock file, CI workflow.

**Out of scope:** Deployment strategy (governed by ADR-022), dependency pinning policy (governed by ADR-015 and ADR-019).

---

## Rationale

### Why not fix Poetry instead of migrating

The effort to "fix" the Poetry setup (generate `poetry.lock`, ensure `poetry install` works cleanly, update CI) was comparable to migrating to uv+hatchling. The migration additionally provides:

1. **A lock file from day one.** `uv.lock` is generated deterministically and pins all 93 transitive dependencies. The project had operated for its entire lifetime without a lock file.

2. **Faster CI.** `uv sync` resolves and installs in seconds. The previous workflow spent time downloading and installing Poetry itself before it could install the project.

3. **Standards-based metadata.** The project already used PEP 621 `[project]` for its metadata. The only Poetry-specific artifact was `[tool.poetry] packages`, which translated to a single hatchling directive. No dependency syntax needed translation — the project used PEP 508 specifiers, not Poetry's `^` syntax.

4. **Platform alignment.** 4 repositories already use this stack. The migration investigation for views-models (the deployment consumer of views-faoapi) assumes uv as the target. Aligning now removes one variable from that future migration.

### Why hatchling specifically

views-faoapi is a single-package project (`src/views_faoapi/`). It does not need hatchling's multi-package capabilities (which motivated the choice in views-bayesian). The choice is pragmatic: hatchling is the build backend used by the other uv-based VIEWS repositories, it handles the `src/` layout cleanly, and it includes non-Python files (shapefiles, logging.yaml) in the wheel by default — which this project requires (ADR-017).

### What was removed

The broken `publish_package.yml` workflow was deleted rather than rewritten. It was non-functional from inception and views-faoapi is not on PyPI. If PyPI publishing is needed in the future, a new workflow should be written from scratch using `uv build` + `uv publish` or the `pypa/gh-action-pypi-publish` action.

---

## Considered Alternatives

### Alternative A: Fix and keep Poetry

Generate `poetry.lock`, verify `poetry install` works, keep the existing CI pattern.

- **Pros:** No migration effort. Consistent with 8 other VIEWS Poetry repos.
- **Cons:** Does not align with the platform direction. Poetry was effectively unused in this project — the local dev workflow bypassed it entirely. Fixing Poetry would mean adopting a tool the project had passively opted out of.
- **Reason for rejection:** Comparable effort to migrate, fewer long-term benefits.

### Alternative B: pip + requirements.txt

Generate `requirements.txt` via `pip freeze`, use bare pip for installation.

- **Pros:** Zero tooling dependencies. Universally understood.
- **Cons:** No resolver — `pip freeze` captures the current environment but does not resolve conflicts. No dependency groups (dev vs runtime). No `src/` layout awareness without `pip install -e .`.
- **Reason for rejection:** A step backward from any modern package manager. Does not solve the reproducibility problem that triggered the decision.

### Alternative C: Conda environment with environment.yml

Create a dedicated conda environment specification for views-faoapi.

- **Pros:** Matches the current views-models deployment pattern. Familiar to the team.
- **Cons:** Conda environments are heavyweight (5-8 GB each), duplicate shared packages, and do not use `pyproject.toml` for dependency declaration. The views-models migration investigation explicitly targets moving away from conda. Would create a new conda environment at the moment the platform is deprecating them.
- **Reason for rejection:** Directly contradicts the platform migration direction documented in `views-models/reports/conda_to_uv_migration_investigation.md`.

---

## Consequences

### Positive

- Reproducible dependency resolution via `uv.lock` (93 packages pinned)
- Faster CI: no Poetry download/install step; `uv sync` completes in seconds
- `pyproject.toml` uses only PEP standards — portable to any PEP 517 tool
- Production deployment unchanged — `pip install .` calls hatchling through the standard PEP 517 interface
- Consistent with 4 other VIEWS platform repositories
- Non-Python package data (shapefiles, logging.yaml) included in wheel by default

### Negative

- Contributors working across VIEWS platform repos now encounter two tooling stacks (Poetry and uv). Mitigation: command equivalence is 1:1 (`poetry run` → `uv run`, `poetry install` → `uv sync`).
- `uv` must be available on any machine running the project. Mitigation: single binary, one-line install (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- The broken publish workflow was deleted rather than fixed, meaning PyPI publishing requires new work if ever needed.

---

## Implementation Notes

The migration was performed in a single commit on the `chore/migrate-to-uv-hatchling` branch:

1. **`pyproject.toml`**: Replaced `poetry-core` with `hatchling` in `[build-system]`. Replaced `[tool.poetry] packages` with `[tool.hatch.build.targets.wheel]` and `[tool.hatch.build.targets.sdist]`. Fixed dev dependency syntax from Poetry parentheses to PEP 508. Added `httpx` to dev dependencies (required by FastAPI's TestClient, previously available only as a transitive dependency).

2. **`.github/workflows/run_pytest.yml`**: Replaced Poetry installation with `astral-sh/setup-uv@v4`. Replaced `poetry install` with `uv sync`. Replaced `poetry run pytest` with `uv run pytest`. Enabled uv cache for faster repeat runs. Bumped `actions/checkout` to v4 and `actions/setup-python` to v5.

3. **`.github/workflows/publish_package.yml`**: Deleted.

4. **`uv.lock`**: Generated via `uv lock` and committed.

### Verification performed

- `uv lock` resolved 93 packages
- `uv sync` installed cleanly into `.venv/`
- `uv run pytest tests/ -v` — 367 passed, 6 skipped, 8 xfailed (identical to pre-migration)
- `uv build` — produced correct wheel containing shapefiles and logging.yaml
- `pip install .` — installs cleanly via hatchling PEP 517 interface (production install path)

---

## Validation & Monitoring

- **CI enforces uv.** The GitHub Actions workflow (`run_pytest.yml`) uses `astral-sh/setup-uv` and `uv sync`. If uv or hatchling break, CI fails immediately.
- **Lock file freshness.** `uv.lock` is committed to version control. Stale lock files cause `uv sync` warnings when dependencies in `pyproject.toml` change.
- **Production install.** `pip install .` on the deployment server invokes hatchling through the standard PEP 517 interface, same as `uv sync` locally.
- **Reconsider if:** uv development stalls or Astral discontinues it; the VIEWS platform decides to standardize on a single stack and chooses a different tool.

---

## References

- [PEP 517 — Build system interface](https://peps.python.org/pep-0517/)
- [PEP 621 — Project metadata in pyproject.toml](https://peps.python.org/pep-0621/)
- [PEP 735 — Dependency groups](https://peps.python.org/pep-0735/)
- ADR-015 — Dependency Hygiene (governs declared vs. used dependencies)
- ADR-017 — Reference Data in Repository (requires non-Python files in wheel)
- ADR-019 — Appwrite SDK Version Pinning (governs dependency pinning policy)
- `views-models/reports/conda_to_uv_migration_investigation.md` — Platform-wide migration context
