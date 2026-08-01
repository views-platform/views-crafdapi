# ADR-022: Deployment Strategy

**Status:** Accepted  
**Date:** 2026-06-04  
**Deciders:** Project maintainers  

---

## Context

views-faoapi is a FastAPI application that serves conflict forecast predictions to FAO. It runs as a long-lived HTTP server on a single Hetzner VPS (CPX52, `faoapi.viewsforecasting.org`).

During a sprint in May 2026, a risk register entry (C-16: "No deployment automation or infrastructure-as-code") was resolved by adding a Dockerfile, docker-compose.yml, Makefile, .dockerignore, and 19 structural tests. This was done without an ADR and without considering whether Docker was the appropriate deployment mechanism for this project.

In fact, the production deployment has never used Docker. The deployment path is:

1. The `views-models` repository contains the deployment entrypoint at `apis/un_fao/run.sh`.
2. `run.sh` creates a conda environment, installs views-faoapi from GitHub via `pip install git+https://...@main`, and runs `apis/un_fao/main.py`.
3. `main.py` instantiates `FAOApiManager` and calls `manager.run()`, which starts uvicorn internally.

This is consistent with how all other VIEWS platform services are deployed: conda/pip environment on the server, direct process execution, no containerization. Docker was an undocumented architectural commitment that contradicted the actual deployment mechanism.

---

## Decision

1. **views-faoapi is deployed as a pip-installable Python package, not a container image.** The deployment entrypoint is `views-models/apis/un_fao/run.sh`, which installs the package from GitHub and starts it via `main.py`.

2. **Docker artifacts are removed from this repository.** Dockerfile, docker-compose.yml, .dockerignore, Makefile, and `tests/test_deployment.py` are deleted. They were never used in production and represented an undocumented deployment decision.

3. **The server process is managed by the deployment host's process supervisor** (currently: manual uvicorn via `manager.run()`; recommended: systemd unit — see Evolution Notes).

4. **The deployment branch is `main`.** `run.sh` installs from `@main`. The `development` branch is merged to `main` when ready to deploy.

**In scope:** How views-faoapi reaches production, what artifacts this repository provides, what it does not provide.

**Out of scope:** The views-models deployment orchestration (owned by that repository), Hetzner server provisioning, Appwrite infrastructure.

---

## Rationale

### Why not Docker

Docker solves problems this project does not have:

- **Environment isolation:** The server runs alone on a dedicated VPS. There is no multi-service orchestration, no conflicting system dependencies, no need for container-level isolation.
- **Reproducible builds:** `uv.lock` (ADR-020) pins all 93 transitive dependencies deterministically. `pip install .` on the server installs the same versions as local development.
- **Deployment automation:** The deployment is a single `pip install` + process start. Docker adds a build step, image registry (or build-on-server), and container runtime without simplifying the deployment.
- **Platform consistency:** No other VIEWS platform service uses Docker in production. Adding it here would introduce an operational outlier requiring different monitoring, different debugging workflows, and different deployment procedures.

The Docker artifacts added during C-16 were a solution looking for a problem. The actual C-16 concern ("no deployment automation") is better addressed by documenting the existing deployment path than by introducing a new one.

### Why pip install from GitHub

This is the established VIEWS platform pattern:

```bash
pip install git+https://${GITHUB_TOKEN}@github.com/views-platform/views-faoapi.git@main
```

- Uses the standard PEP 517 build interface (hatchling, per ADR-020)
- Pins to the `main` branch for production stability
- Requires only pip (available in any Python environment)
- Includes non-Python package data (shapefiles, logging.yaml) via hatchling's wheel configuration (ADR-017)
- No image registry, no build cache, no container runtime required

---

## Considered Alternatives

### Alternative A: Keep Docker as an optional deployment path

Maintain the Dockerfile alongside the pip-install path, giving operators a choice.

- **Pros:** Flexibility for future environments. Docker-native hosting platforms would be supported.
- **Cons:** Two deployment paths means two things to test, two things to document, and two things that can diverge. The Docker path was untested in production — the Dockerfile could not even be built on the development machine (Docker not installed). Maintaining an untested deployment path is worse than having none.
- **Reason for rejection:** An untested optional path provides false confidence, not flexibility.

### Alternative B: Migrate fully to Docker

Make Docker the primary deployment mechanism, update `run.sh` to use `docker compose up`.

- **Pros:** Container isolation, immutable deployments, standard cloud-native pattern.
- **Cons:** Requires Docker on the Hetzner VPS (not currently installed), requires solving `.env` file injection into containers, requires operational knowledge the team doesn't currently maintain, diverges from every other VIEWS platform deployment. Solving a problem that doesn't exist.
- **Reason for rejection:** Disproportionate operational complexity for a single-server, single-service deployment with a small user base.

### Alternative C: Systemd unit file in this repository

Add a `views-faoapi.service` systemd unit to automate process management.

- **Pros:** Auto-restart on crash, log management via journald, proper process supervision.
- **Cons:** Systemd units are host-specific; they reference absolute paths, user accounts, and environment file locations that vary per deployment. Including one in the repository suggests a single canonical deployment layout that may not match reality.
- **Reason for rejection (for now):** The deployment host configuration is owned by the operator, not the application repository. However, a reference systemd unit in the README would be valuable — see Evolution Notes.

---

## Consequences

### Positive

- Deployment mechanism matches reality — no phantom Docker path to maintain
- Consistent with VIEWS platform conventions (pip install + direct execution)
- Removes 4 files + 19 tests that guarded non-production artifacts
- Eliminates the undocumented architectural commitment introduced by C-16

### Negative

- No process supervision out of the box — if uvicorn crashes, it stays down until manually restarted (or until `run.sh` is re-invoked). Mitigated by the healthcheck endpoint at `/health`.
- No immutable deployment artifact — the server installs from a git branch, not a versioned release. Mitigated by the `@main` pin and `uv.lock` determinism.
- Operators who expected Docker will need to use the pip-install path instead.

---

## Implementation Notes

1. **Removed files:** `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `Makefile`, `tests/test_deployment.py`
2. **Updated ADRs:** ADR-020 and ADR-012 Docker references corrected
3. **Risk register:** C-16 resolution updated to reflect documentation-based approach instead of Docker artifacts
4. **README:** Deployment Notes section updated to document the actual deployment path

### What C-16 should have done

The original concern was valid: "No deployment automation or infrastructure-as-code." The correct resolution was to document the existing deployment mechanism (run.sh + pip install + uvicorn) and ensure it worked reliably — not to introduce a new deployment mechanism (Docker) that was never tested or used.

---

## Evolution Notes

### Expected to change

- **Process supervision:** A systemd unit file or equivalent is recommended for production. The README should include a reference unit. This is operator responsibility, not application responsibility, but a template reduces friction.
- **PyPI publishing:** If views-faoapi is published to PyPI (see issue #69), `run.sh` would change from `pip install git+https://...` to `pip install views-faoapi==X.Y.Z`. The deployment *mechanism* stays the same.
- **Conda-to-uv migration:** The views-models `run.sh` currently creates a conda environment. When the platform-wide conda-to-uv migration completes, it will likely use `uv venv` + `uv pip install` instead. The views-faoapi deployment is unaffected — it's pip-installable regardless of how the environment is created.

### Considered stable

- pip-install-from-GitHub as the deployment mechanism
- `main` branch as the deployment target
- `views-models/apis/un_fao/` as the deployment entrypoint owner

### Changes requiring contract revision

- Moving to a container-based deployment (Kubernetes, Docker Swarm, etc.)
- Moving the deployment entrypoint out of views-models into this repository
- Adding a CI/CD pipeline that auto-deploys on merge to main

---

## Validation & Monitoring

- **Healthcheck:** `GET /health` endpoint confirms API is running and Appwrite is reachable
- **Staleness detection:** `_check_staleness()` (C-50 resolution) logs WARNING when prediction data is older than expected
- **Reconsider if:** The VIEWS platform adopts container orchestration, or the API needs to run on multiple servers behind a load balancer

---

## References

- ADR-012: Module Import Discipline (factory pattern for uvicorn)
- ADR-020: Build and Package Management Tooling (hatchling + uv)
- ADR-017: Reference Data in Repository (shapefiles in wheel)
- C-16 (resolved): No deployment automation — original risk register entry
- `views-models/apis/un_fao/run.sh`: Production deployment entrypoint
- `views-models/apis/un_fao/main.py`: Application startup script
- Hetzner deployment: `faoapi.viewsforecasting.org` (CPX52; server IP omitted from the public repo)
