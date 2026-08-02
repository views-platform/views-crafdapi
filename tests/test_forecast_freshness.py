"""S3 (#246, ADR-033 §4): forecast freshness SLA — the verdict helper + config (C-50).

The verdict feeds `/provenance` (surfaced) and `/health` (a stale forecast → `status="degraded"`),
replacing the old log-only staleness that let a 139-day artifact serve with green health.
"""

from datetime import datetime, timedelta, timezone

import pytest

from views_crafdapi.managers.freshness import forecast_freshness, freshness_sla_days

pytestmark = pytest.mark.layer4_infra


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_fresh_forecast_is_not_stale():
    v = forecast_freshness(_iso(10), sla_days=45)
    assert v["is_stale"] is False and v["age_days"] < 45


def test_stale_forecast_is_flagged():
    v = forecast_freshness(_iso(139), sla_days=45)  # the live-incident age
    assert v["is_stale"] is True and v["age_days"] > 45
    assert v["sla_days"] == 45


def test_unknown_created_at_is_never_asserted_stale():
    for bad in (None, "", "not-a-date"):
        v = forecast_freshness(bad, sla_days=45)
        assert v["is_stale"] is None and v["age_days"] is None  # unknown, not asserted-stale


def test_naive_timestamp_is_treated_as_utc():
    naive = (datetime.now(timezone.utc) - timedelta(days=100)).replace(tzinfo=None).isoformat()
    assert forecast_freshness(naive, sla_days=45)["is_stale"] is True


def test_sla_default_is_45_days(monkeypatch):
    monkeypatch.delenv("CRAFDAPI_FORECAST_FRESHNESS_SLA_DAYS", raising=False)
    assert freshness_sla_days() == 45.0


def test_sla_env_override_and_invalid_fallback(monkeypatch):
    monkeypatch.setenv("CRAFDAPI_FORECAST_FRESHNESS_SLA_DAYS", "7")
    assert freshness_sla_days() == 7.0
    monkeypatch.setenv("CRAFDAPI_FORECAST_FRESHNESS_SLA_DAYS", "banana")
    assert freshness_sla_days() == 45.0
