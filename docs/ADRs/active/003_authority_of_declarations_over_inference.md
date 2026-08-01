# ADR-003: Authority of Declarations Over Inference

**Status:** Accepted  
**Date:** 2026-05-27  
**Deciders:** Project maintainers  

---

## Context

In complex systems, the same concept often appears in multiple representations:
- raw vs transformed data
- configuration vs artifact metadata
- intended vs observed behavior

In views-faoapi, this manifests concretely: prediction types appear in both column name prefixes and metadata fields, geographic aggregation levels are encoded in both DataFrame structure and explicit configuration, and credibility intervals are implied by sample counts and declared in configuration.

When these representations diverge, systems often attempt to **infer intent** after the fact.

Such inference leads to:
- silent errors,
- irreproducible results,
- post-hoc rationalization,
- and ambiguity about what the system actually believes.

A clear rule is required to define **where semantic authority lives**, and how ambiguity is resolved.

---

## Decision

In this repository:

> **All meaningful semantics must be explicitly declared.  
> Inference of semantics across component boundaries is forbidden.**

When multiple representations of the same concept exist, **a single source of truth must be designated**.

If required semantics are missing, ambiguous, or contradictory, the system **must not guess**.

---

## Global Invariant: Fail Loud on Semantic Ambiguity

In this repository, **silent failure is considered a bug**.

Whenever required semantics are:
- missing,
- ambiguous,
- contradictory,
- or inconsistent across representations,

the system **must fail loudly and immediately**.

This includes, but is not limited to:
- raising explicit runtime errors,
- failing validation or consistency checks,
- refusing to proceed without explicit declaration.

Warning-only behavior, implicit fallbacks, or "best-effort" inference are **forbidden**
for any decision-relevant semantics.

This rule applies regardless of environment:
development, experimentation, evaluation, or production.

---

## Rules of Semantic Authority

The following rules apply throughout the repository:

- Semantics must be **declared**, not inferred.
- Transformations are owned by the component that performs them.
- Metadata overrides naming conventions.
- Evaluation consumes **declared semantics only**.
- No component may guess another component's intent.

Inference is permitted **only within a component's internal logic**, never across component boundaries.

---

## Examples of Forbidden Behavior

The following are concrete examples of inference-based behavior that is forbidden in views-faoapi:

- **Inferring prediction type from `pred_*` column name prefixes** instead of reading the declared prediction type from metadata. Column naming conventions are not semantic authority --- typed metadata is.

- **Inferring geographic aggregation level from DataFrame shape or index type** instead of reading the explicitly declared GAUL level (L1, L2) or PRIO-GRID designation from configuration or metadata. The number of rows or the index dtype is not a reliable indicator of geographic resolution.

- **Inferring HDI credibility interval from sample count** rather than reading the interval specification from explicit configuration. The number of posterior samples does not determine the credibility interval --- the declared interval parameter does.

- **Proceeding with Appwrite operations when `APPWRITE_*` environment variables are `None`** instead of failing immediately. Missing infrastructure configuration must cause a loud, immediate failure, not a deferred error during a storage operation.

- **Guessing GAUL level from the number of geographic metadata columns present** in a DataFrame or shapefile join result. The geographic level must be declared explicitly, not reverse-engineered from data structure.

If behavior matters, it must be declared.

---

## Consequences

### Positive
- Eliminates silent semantic drift across prediction, aggregation, and statistical layers
- Improves reproducibility and debuggability of forecast outputs
- Makes disagreements explicit and resolvable
- Enables principled failure under uncertainty

### Negative
- Requires more explicit configuration and metadata at every boundary
- Some convenience patterns are disallowed
- Errors may surface earlier and more frequently

These costs are accepted intentionally.

---

## Notes

This ADR does not define:
- what concepts exist (ADR-001),
- or how components depend on each other (ADR-002).

It defines **who is allowed to say what something means**,  
and mandates **loud failure over silent misinterpretation**.
