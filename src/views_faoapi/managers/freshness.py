"""Forecast freshness — the SLA and the verdict (S3/#246, ADR-033 §4; used by S4/#249 too).

A served forecast older than the SLA is *stale*: `/health` reports `degraded`, `/provenance`
flags it (S3), and the bounded fallback refuses to serve a last-good run past it (S4). Shared by
`api.py` (the endpoints) and `dataset_service.py` (the fallback), so the rule lives in one place.

Pure: `os`/`datetime` only — no pandas, no Appwrite, safe to import anywhere.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ENV = "FAOAPI_FORECAST_FRESHNESS_SLA_DAYS"
_DEFAULT_SLA_DAYS = 45.0


def freshness_sla_days() -> float:
    """The forecast freshness SLA in days. Default 45 (a monthly forecast is expected roughly
    monthly, with slack); override with the ``FAOAPI_FORECAST_FRESHNESS_SLA_DAYS`` env var."""
    raw = os.getenv(_ENV, "").strip()
    try:
        return float(raw) if raw else _DEFAULT_SLA_DAYS
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", _ENV, raw, _DEFAULT_SLA_DAYS)
        return _DEFAULT_SLA_DAYS


def forecast_freshness(created_at: Optional[str], sla_days: float) -> Dict[str, Any]:
    """Freshness verdict for a forecast, from its artifact ``created_at`` (ISO-8601).

    Returns ``{"sla_days", "created_at", "age_days", "is_stale"}``. When ``created_at`` is missing
    or unparseable, ``age_days``/``is_stale`` are ``None`` — *unknown*, never asserted-stale: we do
    not flip health red (or refuse a fallback) on a signal we cannot compute (no false positives)."""
    verdict: Dict[str, Any] = {
        "sla_days": sla_days, "created_at": created_at, "age_days": None, "is_stale": None,
    }
    if not created_at:
        return verdict
    try:
        ts = datetime.fromisoformat(str(created_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return verdict
    verdict["age_days"] = round(age_days, 2)
    verdict["is_stale"] = age_days > sla_days
    return verdict
