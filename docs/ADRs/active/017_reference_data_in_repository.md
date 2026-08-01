# ADR-017: Reference Data in Repository

**Status:** Accepted  
**Date:** 2026-05-27  
**Deciders:** Project maintainers  
**Consulted:** —  
**Informed:** —  

---

## Context

The VIEWS CRAF'd API aggregates PRIO-GRID cell-level conflict forecasts to administrative boundary levels (country, GAUL Level 1, GAUL Level 2). This requires authoritative geographic reference data — specifically, the FAO Global Administrative Unit Layers (GAUL) 2024 shapefiles — to map grid cells to administrative units.

These shapefiles are:
- Published annually by FAO and not available via a stable programmatic API
- Required at runtime for geographic aggregation (currently GAUL codes are used in `data/handlers.py:1146-1156,1207-1209`; the shapefiles themselves will be consumed when spatial joins are implemented)
- Binary files (`.shp`, `.dbf`, `.shx`, `.sbn`, `.sbx`, `.prj`, `.cpg`) plus metadata (`.shp.xml`, `.docx`, `.xlsx`)
- Small: 672KB total across both levels

The question is whether binary reference data belongs in the git repository or should be distributed through another mechanism.

---

## Decision

**Static reference data that is small (<10MB), required at runtime, and changes infrequently (annually or less) is committed directly to the repository** under `src/views_crafdapi/shapefiles/`.

Specifically:
- `GAUL_2024_L1/` — Level 1 (first-order administrative divisions), 128KB
- `GAUL_2024_L2/` — Level 2 (second-order administrative divisions), 540KB

Reference data in the repository must:
1. Be placed under `src/views_crafdapi/shapefiles/` (or a similarly scoped directory under `src/`)
2. Include provenance metadata (changelogs, metadata spreadsheets) alongside the data files
3. Be versioned by year in the directory name (e.g., `GAUL_2024_L1`, not `GAUL_L1`)
4. Not exceed 10MB per dataset without explicit re-evaluation of this ADR

---

## Rationale

- **Reproducibility:** Pinning reference data in version control ensures every checkout produces identical geographic aggregation results. There is no risk of a download failing, a URL changing, or a provider silently updating the data.
- **Simplicity:** No download scripts, no cloud storage configuration, no network dependency at startup or test time. The data is simply there.
- **Size is negligible:** At 672KB, the shapefiles are smaller than many individual Python source files in the repository. Git handles this efficiently.
- **Change frequency is low:** GAUL is updated annually. A once-a-year commit replacing the shapefiles is trivial.
- **Provenance is preserved:** The included changelogs and metadata spreadsheets document exactly which GAUL release this is, making audits straightforward.

---

## Considered Alternatives

### Alternative A: Download at startup from a remote URL
- **Pros:** Keeps the repo lean; always gets latest data
- **Cons:** Introduces network dependency at startup; FAO does not provide a stable download API; "always latest" is a reproducibility hazard — aggregation results would silently change when FAO publishes an update
- **Reason for rejection:** Violates reproducibility requirements and adds fragile infrastructure

### Alternative B: Git LFS (Large File Storage)
- **Pros:** Keeps repo clone size small for contributors who don't need the data
- **Cons:** Adds LFS infrastructure dependency; 672KB does not warrant it; complicates CI/CD; LFS has per-bandwidth costs on GitHub
- **Reason for rejection:** Overhead disproportionate to file size. Revisit if reference data grows past 10MB.

### Alternative C: Package the shapefiles as a separate Python package
- **Pros:** Clean separation of code and data; version-pinned via dependency
- **Cons:** Massive overhead for 672KB of data; creates a second package to maintain, release, and version; slows iteration
- **Reason for rejection:** Engineering cost far exceeds benefit at this scale

### Alternative D: Store in Appwrite alongside prediction data
- **Pros:** Consistent with existing data storage pattern
- **Cons:** Reference data is not prediction data — different lifecycle, different access pattern; would require Appwrite credentials and network access for every test or local dev run; conflates two distinct data categories
- **Reason for rejection:** Wrong abstraction. Reference data is a build-time/package-time artifact, not a runtime-fetched resource.

---

## Consequences

### Positive
- Zero-configuration access to reference data for all developers and CI
- Exact reproducibility of geographic aggregation across all environments
- Provenance metadata committed alongside data files

### Negative
- Repository size increases by 672KB (negligible)
- Binary files produce opaque diffs — reviewers must trust the commit message and provenance metadata when shapefiles are updated
- Annual update requires a manual commit replacing the directory contents

---

## Implementation Notes

- The shapefiles are already committed under `src/views_crafdapi/shapefiles/`
- When spatial join code is implemented, it should reference these paths relative to the package root (e.g., via `importlib.resources` or `pathlib.Path(__file__).parent / "shapefiles"`)
- When GAUL 2025 is published, create a new directory `GAUL_2025_L1/` etc. and update the consuming code. Keep the old version in a separate commit for clean git history.
- If a third GAUL level or additional reference datasets are added, re-evaluate the 10MB threshold

---

## Validation & Monitoring

- The 10MB threshold is the trigger for reconsidering this decision
- If download-at-startup is ever adopted (counter to this ADR), it must be gated behind a feature flag and the committed files must remain as fallback
- `du -sh src/views_crafdapi/shapefiles/` should be checked when updating reference data

---

## Open Questions

- Should the shapefiles be included in the built Python package (via `pyproject.toml` `[tool.hatch.build.targets.wheel]`), or only available from a source checkout? Currently they are included by hatchling by default since they are under `src/`.

---

## References

- ADR-001: Ontology — defines Reference Data as a first-class category
- FAO GAUL documentation: included as `GAUL_2024_L1_Metadata.xlsx` and `GAUL_2024_L2_Metadata.xlsx`
- `data/handlers.py:1146-1156` — GAUL code column definitions (`_METADATA_COLS`)
- `data/handlers.py:1207-1209` — geographic aggregation level-to-code mappings
