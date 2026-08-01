"""Typed forecast-selection decision (Epic #244 / S1, ADR-033 §1).

The forecast serving path resolves every request to exactly one of these results
instead of a ``dataframe | None`` — a return shape that conflated *"no run exists"*,
*"a run exists but cannot be served"*, and *"served"* into the same ``None`` and
threw away the one useful fact: **why**.

- ``Served`` — a servable run was resolved (carries the dataframe/dataset it built).
- ``Refused(reason, …)`` — a run was present but cannot be served; ``reason`` is a
  first-class, machine-readable value (surfaced in logs today; in ``/health`` and
  ``/provenance`` as later stories attach policy).
- ``NoRun`` — no manifested run exists for the configured line (not an error).

Gates are a **composable sequence** (``run_gates``): each gate maps a ``Served`` to
either the same ``Served`` or a ``Refused``. New gates (freshness — S3; capability —
S5; a future value-drift gate) are added by appending to the sequence, with no change
to this module or to the caller (ADR-033 §1). Pure data + one helper: no pandas, no
numpy, safe to import anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence, Union


@dataclass(frozen=True)
class Served:
    """The selection resolved to a servable run.

    ``dataframe``/``dataset`` are kept untyped (``Any``) so this module stays
    dependency-light; they are a ``pandas.DataFrame`` and a ``ForecastDataset``.
    ``mode`` is the serving path that produced it (``"wire"`` / ``"legacy"``).
    """

    dataframe: Any
    dataset: Any
    file_id: str
    mode: str
    provenance: Optional[dict] = None


@dataclass(frozen=True)
class Refused:
    """A run was present but cannot/should not be served. ``reason`` is machine-readable
    (e.g. ``"ingest_failed"``, ``"manifest_malformed"``); ``detail`` is human context."""

    reason: str
    detail: Optional[str] = None
    file_id: Optional[str] = None


@dataclass(frozen=True)
class NoRun:
    """No manifested run exists for the configured line — not an error, not a refusal."""


SelectionResult = Union[Served, Refused, NoRun]
Gate = Callable[[Served], SelectionResult]


def run_gates(result: SelectionResult, gates: Sequence[Gate]) -> SelectionResult:
    """Apply ``gates`` in order to a ``Served`` result, short-circuiting on the first
    ``Refused``. Non-``Served`` results pass through unchanged. Adding a gate is purely
    additive — append it to ``gates`` (ADR-033 §1). The gate sequence is empty in S1;
    freshness (S3) and capability (S5) attach later."""
    for gate in gates:
        if not isinstance(result, Served):
            break
        result = gate(result)
    return result
