"""Decision-record guards for ADR-032 (uptime monitoring).

Origin: a 2026-07-20 /falsify audit FALSIFIED the claim "we know the best poller for
faoapi." These began as xfail stubs recording the gaps; ADR-032 (Accepted 2026-07-20)
closed them, so they are now live guards — the monitoring decision record MUST keep its
criteria, its commercial-use/licensing check, its evaluation of a self-hosted option,
and its privacy/residency weighing. If a future edit strips any of these, these fail.

Findings map (all now closed by ADR-032):
  F1  decision criteria written down     -> test_monitoring_decision_record_exists / _records_selection_criteria
  F2  commercial-use licensing verified  -> test_decision_verifies_commercial_use_licensing
  F3  self-hosted (Uptime Kuma) evaluated-> test_decision_evaluated_self_hosted_option
  F4  privacy/residency vs C-79 weighed  -> test_decision_weighs_privacy_and_residency
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.layer4_infra

_ROOT = Path(__file__).parent.parent
# The monitoring decision record — ADR-032 (Accepted 2026-07-20).
_DECISION_CANDIDATES = [
    _ROOT / "docs" / "ADRs" / "active" / "032_uptime_monitoring_and_alerting.md",
    _ROOT / "reports" / "ops" / "monitoring_decision.md",
]


def _decision_text():
    for p in _DECISION_CANDIDATES:
        if p.exists():
            return p.read_text().lower()
    return None


def test_monitoring_decision_record_exists():
    assert _decision_text() is not None


def test_decision_records_selection_criteria():
    t = _decision_text() or ""
    assert "criteria" in t and ("interval" in t or "alert" in t)


def test_decision_verifies_commercial_use_licensing():
    t = _decision_text() or ""
    assert "commercial" in t or "terms of service" in t or "licen" in t


def test_decision_evaluated_self_hosted_option():
    t = _decision_text() or ""
    assert "uptime kuma" in t or "self-host" in t or "self host" in t


def test_decision_weighs_privacy_and_residency():
    t = _decision_text() or ""
    assert any(k in t for k in ("residency", "gdpr", "c-79", "privacy"))
