# Instantiation Checklist

Use this checklist when bootstrapping a new project from base_docs templates.

---

## Before You Start

- [x] Decide which adoption phase you're targeting (see `ADRs/README.md` — Recommended Adoption Order)
- [x] Identify your project's ontological categories (data types, models, configs, artifacts, etc.)

---

## ADR Adaptation

### All adopted ADRs
- [x] Update Status from `--template--` to `Proposed` or `Accepted`
- [x] Fill in Date, Deciders, Consulted, Informed fields

### Per-ADR adaptation notes
- [x] **ADR-000:** Update the `ADRs/` path reference if your project uses a different location
- [x] **ADR-001:** Define your project's ontological categories and stability levels
- [x] **ADR-002:** Define your project's layering and forbidden dependency patterns
- [x] **ADR-003:** Adapt forbidden behavior examples to your domain
- [x] **ADR-005:** No domain adaptation needed (taxonomy is universal)
- [x] **ADR-006:** No domain adaptation needed (criteria are universal)
- [x] **ADR-007:** Verify contributor protocol paths match your project structure
- [x] **ADR-009:** Adapt boundary examples to your project's boundaries

---

## CICs

- [x] Replace placeholder active contracts list in `CICs/README.md` with your project's contracts
- [x] Create intent contracts for your project's non-trivial classes using `CICs/cic_template.md`

---

## Contributor Protocols

- [x] Review and adapt `docs/contributor_protocols/silicon_based_agents.md` for your tooling
- [x] Review and adapt `docs/contributor_protocols/carbon_based_agents.md` for your team
- [x] Adapt or remove `docs/contributor_protocols/hardened_protocol_template.md` (domain-specific)

---

## Standards

- [x] Review `standards/logging_and_observability_standard.md` — adapt scope expectations to your domain
- [ ] Review `standards/physical_architecture_standard.md` — adapt directory ontology to your project
  *(Skipped — project does not follow 1-class-1-file convention)*

---

## Risk Register

- [x] Technical risk register exists at `reports/technical_risk_register.md`
- [x] ADR-010 governs the risk register

---

## Final Verification

- [x] No files still have Status `--template--` (unless intentionally deferred like ADR-004)
- [x] No phantom references to non-existent files
- [x] All cross-ADR references resolve correctly
- [x] Run `validate_docs.sh` to check internal consistency
