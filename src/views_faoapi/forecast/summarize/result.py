"""`CollapseResult` — the value object returned by the posterior-sample collapse.

One per-row summary of posterior samples (raw counts, ADR-024): the MAP point estimate,
the nested HDIs at each requested credible mass, and the `severe` tail summary. Holding these
in one named object (rather than a positional tuple) is what lets a new statistic be *added*
without editing every call site (OCP) — callers read `.map` / `.hdi[mass]` / `.severe`, never a
tuple position. Arrays are float64 (the served collapse dtype, ADR-030) and shape ``(N,)``,
aligned to the collapse input rows; ``hdi[mass]`` is ``(N, 2)`` as ``[lower, upper]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np


@dataclass(frozen=True)
class CollapseResult:
    """MAP + nested HDIs + severe tail for a batch of posterior-sample rows (raw counts).

    - ``map``: ``(N,)`` MAP point estimate (tower mode).
    - ``hdi``: ``{mass: (N, 2)}`` — nested highest-density intervals ``[lower, upper]`` per
      credible mass (e.g. ``0.50 / 0.90 / 0.95``); nested by construction (mass↑ ⇒ interval⊇).
    - ``severe``: ``(N,)`` mean of the worst tail of draws (``expected_shortfall``).
    - ``bimodality``: ``(N,)`` conservative 0/1 flag — 1 where the sample distribution has a
      clearly separated, materially populated secondary mode (so a single MAP/HDI is read with
      care). ADR-025 §A.3.
    """

    map: np.ndarray
    hdi: Dict[float, np.ndarray]
    severe: np.ndarray
    bimodality: np.ndarray

    def lower(self, mass: float) -> np.ndarray:
        """HDI lower bound ``(N,)`` at ``mass``."""
        return self.hdi[mass][:, 0]

    def upper(self, mass: float) -> np.ndarray:
        """HDI upper bound ``(N,)`` at ``mass``."""
        return self.hdi[mass][:, 1]

    @property
    def masses(self) -> tuple[float, ...]:
        """The credible masses present, in insertion order."""
        return tuple(self.hdi.keys())
