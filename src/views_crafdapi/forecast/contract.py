"""The wire-contract dialect this build renders — the deploy/serve capability gate (S5/#250,
ADR-033 §7, C-171).

A run's manifest declares the wire-contract version it was written in (vpp ADR-013 `contract_version`,
e.g. ``"1.5"``). A deployed build implements a specific dialect; serving a run written in a dialect
the build cannot render would silently degrade the output (the C-171 deploy-skew risk). This module
is the single source of the build's capability and the compatibility rule, so both the ingest gate
(`dataset_service`) and the remote capability surface (`version.py` → `GET /version`) agree.

Pure: stdlib only (no pandas / views-frames), safe to import from the dependency-light `version.py`.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# The wire-contract dialect (vpp ADR-013) this build can render. Bump this when crafdapi implements
# support for a new dialect (and only then) — it is the served-schema *capability*, asserted at
# `/version` and enforced at ingest. Deployed builds and `main` may differ; that skew is exactly
# what C-171 governs, and `/version` makes it remotely visible.
SERVED_CONTRACT_VERSION = "1.5"


def _parse(version: str) -> Optional[Tuple[int, int]]:
    """Parse a ``"major.minor"`` contract version to an ``(int, int)`` tuple; ``None`` if
    unparseable. A bare ``"1"`` is treated as ``(1, 0)``."""
    try:
        parts = str(version).strip().split(".")
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError, AttributeError):
        return None


def can_render_contract(required: str) -> bool:
    """Whether this build can faithfully render a run declaring wire-contract ``required``
    (ADR-033 §7, C-171).

    - **Unstamped** (empty/whitespace) ⇒ ``True`` (transition-safe): the producer does not yet
      stamp `contract_version` everywhere (cross-repo vpp ADR-013 / views-postprocessing#133), so an
      absent version is surfaced, not gated. Tighten to fail-closed once stamping is guaranteed.
    - **Stamped** ⇒ rendered iff it shares the served MAJOR and its MINOR does not exceed the served
      MINOR — i.e. this build understands every field the run may carry. A newer minor or a different
      major would render degraded, so it is refused.
    - **Stamped but unparseable** ⇒ ``False``: compatibility cannot be established, so refuse rather
      than serve a possibly-degraded output.
    """
    if not str(required).strip():
        return True  # unstamped — transition-safe pass
    served = _parse(SERVED_CONTRACT_VERSION)
    req = _parse(required)
    if req is None or served is None:
        return False
    return req[0] == served[0] and req[1] <= served[1]
