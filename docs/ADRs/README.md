# ADR README and Governance Map

This repository uses Architectural Decision Records (ADRs) to govern
structural, semantic, and operational behavior.

ADRs are divided into two categories:

1. **Constitutional ADRs (000-009)**  
   Foundational architectural rules that apply across the system.

2. **Project-Specific ADRs (010+)**  
   Domain, implementation, or feature-level decisions.

---

## Lifecycle Folder Organization

ADRs are organized into lifecycle folders:

- **`active/`** — Currently accepted and enforced ADRs
- **`proposed/`** — ADRs under discussion, not yet accepted
- **`archive/`** — Superseded or deprecated ADRs, retained for historical reference

All ADR file references below use the `active/` prefix for currently enforced decisions.

---

## Constitutional ADRs

These ADRs define system philosophy and governance:

- **[ADR-000](active/000_use_of_adrs.md)** — Use of Architecture Decision Records  
  Establishes the ADR practice for views-faoapi.

- **[ADR-001](active/001_ontology_of_the_repository.md)** — Ontology of views-faoapi  
  Defines what concepts exist in the repository.

- **[ADR-002](active/002_topology_and_dependency_rules.md)** — Topology and Dependency Rules  
  Defines structural dependency direction for views-faoapi.

- **[ADR-003](active/003_authority_of_declarations_over_inference.md)** — Authority of Declarations Over Inference  
  Defines where semantic authority lives.

- **[ADR-004](active/004_rules_for_evolution_and_stability.md)** — Rules for Evolution and Stability (Deferred)

- **[ADR-005](active/005_testing_as_mandatory_critical_infrastructure.md)** — Testing as Mandatory Critical Infrastructure  
  Defines red / beige / green test doctrine.

- **[ADR-006](active/006_intent_contracts_for_non_trivial_classes.md)** — Intent Contracts for Non-Trivial Classes  
  Requires declared class-level purpose.

- **[ADR-007](active/007_silicon_based_agents_as_untrusted_contributors.md)** — Silicon-Based Agents as Untrusted Contributors  
  Governs automated modification.

- **[ADR-008](active/008_observability_and_explicit_failure.md)** — Observability and Explicit Failure  
  Defines fail-loud + log requirements.

- **[ADR-009](active/009_boundary_contracts_and_configuration_validation.md)** — Boundary Contracts and Configuration Validation  
  Defines explicit interface contracts and configuration validation.

These ADRs form the architectural constitution of the repository.

---

## Project-Specific ADRs

ADRs numbered 010 and above define:

- Domain-specific evaluation strategy
- Implementation details
- Infrastructure decisions
- Feature-level trade-offs

These must comply with the constitutional ADRs above.

### Active Project-Specific ADRs

- **[ADR-010](active/010_technical_risk_register.md)** — Technical Risk Register  
  Establishes and governs the project's technical risk register at `reports/technical_risk_register.md`.

- **[ADR-011](active/011_caching_strategy_and_eviction_policy.md)** — Caching Strategy and Eviction Policy  
  Mandates bounded, TTL-aware, versioned caching across all three tiers. Addresses C-05, C-07.

- **[ADR-012](active/012_module_import_discipline.md)** — Module Import Discipline and Startup Side Effects  
  Prohibits module-level initialization; requires safe importability for testing. Addresses C-02, C-04, C-10.

- **[ADR-013](active/013_environment_variable_validation.md)** — Environment Variable Validation and Fail-Fast Configuration  
  Requires startup validation of all required environment variables. Addresses C-03.

- **[ADR-014](active/014_dead_code_removal_policy.md)** — Dead Code Removal Policy  
  Prohibits committed commented-out code blocks; git history is the archive. Addresses C-08.

- **[ADR-015](active/015_dependency_hygiene.md)** — Dependency Hygiene  
  Requires all declared dependencies to be actually imported. Addresses C-09.

- **[ADR-016](active/016_concurrency_safety.md)** — Concurrency Safety for Shared Stateful Objects  
  Requires shared mutable state to be immutable, per-request, or synchronized. Generalizes C-01.

- **[ADR-017](active/017_reference_data_in_repository.md)** — Reference Data in Repository  
  Commits small, stable reference data (GAUL shapefiles) directly to the repository for reproducibility.

- **[ADR-018](active/018_sdk_response_normalization.md)** — SDK Response Normalization at Appwrite Boundary  
  All Appwrite SDK responses normalized to plain dicts at the boundary via `_as_dict()`. Governs the dual-SDK (13.x/19.x) compatibility layer.

- **[ADR-019](active/019_appwrite_sdk_version_pinning.md)** — Appwrite SDK Version Pinning  
  Pins `appwrite==19.2.0` to prevent accidental upgrade past `list_documents` removal. Migration to `tablesDB.list_rows` blocked until Appwrite ships replacement.

- **[ADR-020](active/020_build_and_package_management_tooling.md)** — Build and Package Management Tooling  
  Uses hatchling (build backend) and uv (package manager). Commits `uv.lock` for reproducible builds. Aligns with platform direction.

- **[ADR-021](active/021_dense_grid_fill_value_semantics.md)** — Dense Grid Fill Value Semantics  
  Zero-fill for missing grid cells is the correct default (matching views-datafactory ADR-024). Fill value is configurable via constructor parameter. Resolves D-04, C-41.

- **[ADR-022](active/022_deployment_strategy.md)** — Deployment Strategy  
  Deployed as a pip-installable package via `views-models/apis/un_fao/run.sh`, not as a container. Docker artifacts removed. Documents the actual production deployment path.

- **[ADR-023](active/023_rebaselining_published_forecasts_governance.md)** — Governance Gate for Re-baselining Published Forecasts  
  Changes that move *published* forecast values (estimator, aggregation, fill, source switch) reach `main` only with sign-off, a before/after diff, a methodology version bump, and FAO-facing communication. Gate applies at `development`→`main` cutover. Resolves C-84; gates PR #93 (M1 cutover).

- **[ADR-024](active/024_raw_count_serving_contract.md)** — faoapi Serves Forecasts in Raw Count Space  
  faoapi is the terminal consumer of the platform raw-space contract: it serves raw fatality counts and **neither applies nor inverts** target transforms (pipeline-core ADR-055 Clause 4). Scale is never inferred from a column-name prefix (`pred_ln_*` is not a log signal — ADR-055 Clause 5 / ADR-012); the estimator collapses in raw space; ingestion guards raw-scale (trust-but-verify, extends C-72). Links pipeline-core ADR-055, views-models ADR-012, hydranet ADR-063.

- **[ADR-025](active/025_fao_output_schema_and_naming.md)** — FAO Output Schema and Column Naming  
  The FAO bulk artifact is a wide admin-1 parquet, 36 columns: 6 identity (GAUL admin-1 + GAUL admin-0 country + ISO3, `month_id`) + per series (`sb`/`ns`/`os`) `map`, three nested HDIs (50/90/95), `severe_scenario` (mean of the worst 5% — `expected_shortfall`, not max), `bimodality_flag`, and `actual`. Consumer-facing names — no `ged_`/`lr_`/`ln_`/`pred_` prefixes; country is GAUL admin-0 (not M49); `lat`/`lon` at pgm grain only. All raw counts (ADR-024). **Amends Release Note 01** (format + naming). Closes the "no output-schema/naming ADR" gap.

---

## Recommended Adoption Order

When bootstrapping governance for a new project or onboarding contributors,
adopt ADRs in this order:

1. **ADR-000** — Establishes that ADRs exist and are authoritative
2. **ADR-001** — Defines what exists (ontology)
3. **ADR-002** — Defines structural rules (topology)
4. **ADR-003** — Defines semantic authority (declarations over inference)
5. **ADR-008** — Defines failure semantics (observability)
6. **ADR-005** — Defines testing doctrine
7. **ADR-006** — Defines intent contracts
8. **ADR-007** — Defines silicon-based agent constraints
9. **ADR-009** — Defines boundary contracts

ADR-004 (Evolution and Stability) is deferred and may be adopted when the
project reaches sufficient maturity.

---

## Cross-repo platform contracts (referenced, never copied)

Contracts that govern the *seam* between repos live in their home repo and are referenced here **by
pinned commit** — never copied (þing-01 verdict D1).

- **PLATFORM-001 — Identity, Secrets & Configuration Contract (VIEWS Appwrite seam)**  
  Home: `views-appwrite`. The canonical coordinate **registry** (non-secret ids; secrets as slots)
  and the identity/secret rules faoapi consumes. Pinned reference:  
  <https://github.com/views-platform/views-appwrite/blob/60674b2c4421086af146627df26ed946b77b73a8/docs/ADRs/platform/coordinate_registry.toml>  
  faoapi's startup preflight (`_validate_appwrite_env` → `_validate_env_against_registry`, #278)
  validates its resolved environment against this registry when `APPWRITE_REGISTRY` points at a
  checkout of it.

---

## Governance Structure (Conceptual Map)

- **Ontology (001)** defines what exists.
- **Topology (002)** defines structural direction.
- **Authority (003)** defines who owns meaning.
- **Boundary Contracts (009)** define interaction rules.
- **Observability (008)** enforces failure semantics.
- **Testing (005)** verifies system integrity.
- **Intent Contracts (006)** bind class-level behavior.
- **Automation Governance (007)** constrains silicon-based agents.

Together, these define the invariant layer of the system.
