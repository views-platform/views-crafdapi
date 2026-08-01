# Class Intent Contracts README

This directory contains **Intent Contracts** as defined in ADR-006.

An Intent Contract is a human-readable, unambiguous declaration of:

- what a non-trivial class is meant to do,
- what it must never do,
- its invariants,
- and its failure semantics.

Intent Contracts are architectural artifacts.
They are not implementation documentation.

---

## When Is an Intent Contract Required?

An Intent Contract is mandatory for:

- Core domain classes
- Architectural boundary classes
- Orchestration components
- State-owning components
- Classes that enforce invariants
- Classes that modify semantics or transformation

Trivial value objects and pure utility functions do not require one.

---

## Structure of an Intent Contract

Each contract must define:

1. Purpose
2. Responsibility Boundary
3. Invariants
4. Explicit Non-Responsibilities
5. Failure Semantics
6. Observable Effects (if applicable)

Contracts must be clear enough that:

- Tests (ADR-005) can be derived from them.
- Architectural violations can be detected.
- Silicon-based agents cannot reinterpret intent (ADR-007).

---

## Active Contracts

- [`_GridDataset`](_GridDataset.md) — DataFrame-backed dataset with time x entity x feature tensor semantics (formerly `_ViewsDataset`)
- [`FAO_PGMDataset`](FAO_PGMDataset.md) — VIEWS PRIO-GRID Monthly dataset with geographic metadata and aggregation
- [`FAOApiManager`](FAOApiManager.md) — FastAPI service orchestration, multi-tier caching, endpoint routing
- [`AppWriteFileManager`](AppWriteFileManager.md) — Appwrite-backed file storage, metadata management, and caching
- [`PredictionStoreManager`](PredictionStoreManager.md) — prediction-specific file CRUD with metadata validation
- [`BulkParquetWriter`](BulkParquetWriter.md) — the ADR-025 admin-1 bulk artifact (33-column wide parquet; forecast quantities + historical `actual`)

## Superseded Contracts

- [`PosteriorDistributionAnalyzer`](PosteriorDistributionAnalyzer.md) — the v1 hand-rolled MAP/HDI estimator; **class removed 2026-07-24** (epic #222 / S1), superseded by the views-frames tower (`forecast/summarize/estimator.tower_collapse`). Retained for historical reference only.

---

## Governance Relationship

Intent Contracts are governed by:

- ADR-006 (Intent Contracts for Non-Trivial Classes)
- ADR-003 (Authority of Declarations)
- ADR-005 (Testing Doctrine)

If a class changes meaning, its Intent Contract must be updated.
