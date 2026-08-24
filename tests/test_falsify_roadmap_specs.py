"""Falsification audit of `reports/ROADMAP.md` (2026-08-24).

Claim under attack: *"This roadmap contains no hand waving, no ambiguity, and no decision gap —
neither explicit nor implicit."*

**Verdict: FALSIFIED** — 4 hard, 4 soft.

Each test below pins one finding. They are `xfail(strict=True)`, so they are recorded without
breaking the build and will fail loudly as XPASS the moment the roadmap is corrected — which is the
signal to remove the marker. Written by the author of the roadmap, which is itself a weakness of
this audit: the auditor was not independent.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer4_infra

def _flat(p: Path) -> str:
    """Collapse the hard wrapping. Both documents wrap at ~98 chars, so a phrase that reads as
    one sentence is split across lines and a naive substring match silently misses it — which is
    exactly how two of these tests first passed for the wrong reason."""
    return " ".join(p.read_text().split())


_ROADMAP = _flat(Path(__file__).parent.parent / "reports" / "ROADMAP.md")
_REFRESH = _flat(Path(__file__).parent.parent / "deployment" / "MONTHLY_REFRESH.md")


@pytest.mark.xfail(strict=True, reason="HARD 1: the goal says 'without anyone remembering to'; step 4 decides Hop A stays manual. Someone must remember to run Hop A every month, so the definition of done is unreachable under the roadmap's own decision. Neither section acknowledges the other.")
def test_the_goal_does_not_contradict_step_4():
    wants_no_memory = "without anyone remembering to" in _ROADMAP
    keeps_a_manual = "Hop A stays manual" in _ROADMAP
    assert not (wants_no_memory and keeps_a_manual)


@pytest.mark.xfail(strict=True, reason="HARD 2: step 4 calls Hop B 'deterministic' and uses that as the justification for automating it. MONTHLY_REFRESH.md:55-57 says the opposite — 'when you run it determines what you get' — because Hop B resolves the newest manifested run and clips history to the producer's boundary.")
def test_hop_b_is_not_called_deterministic_when_its_own_runbook_says_otherwise():
    claims_deterministic = "deterministic" in _ROADMAP
    runbook_says_time_dependent = "when* you run it determines *what* you get" in _REFRESH
    assert not (claims_deterministic and runbook_says_time_dependent)


@pytest.mark.xfail(strict=True, reason="HARD 3: the definition of done requires naming 'which pgm and cm ensembles produced it', but the roadmap itself records that no cm baseline ensemble exists and ADR-013 lists cm as an explicit non-goal. Whether completing #81 is required for done is never stated — so the goal is either unreachable or cm is out of scope, and the document does not say which.")
def test_the_goal_states_whether_cm_is_in_scope():
    requires_cm = "which pgm and cm ensembles produced it" in _ROADMAP
    records_no_cm = "no cm baseline" in _ROADMAP.lower()
    says_which = "cm is out of scope" in _ROADMAP or "#81 is required" in _ROADMAP
    assert not (requires_cm and records_no_cm) or says_which


@pytest.mark.xfail(strict=True, reason="HARD 4: 'used by no known consumer' is the justification for a breaking change to a public route, and it is unverified. The register says only that CRAF'd *uses* bulk and subset — an inference about their usage, not evidence about their calls. The nginx access-log read that would settle it has never been done.")
def test_the_breaking_change_is_not_justified_by_an_unverified_claim():
    asserts_no_consumer = "used by no known consumer" in _ROADMAP
    marks_it_unverified = "unverified" in _ROADMAP.lower() or "never been checked" in _ROADMAP.lower()
    assert not asserts_no_consumer or marks_it_unverified


@pytest.mark.xfail(strict=True, reason="SOFT 5: the goal is 'every month' (~30 days); the only automated detector fires at the 45-day freshness SLA. A 44-day gap satisfies the monitor and violates the goal. No tolerance is stated and nothing reconciles the two numbers.")
def test_every_month_is_reconciled_with_the_45_day_sla():
    says_monthly = "every month" in _ROADMAP
    # A real reconciliation states what gap counts as a missed month. The phrase below is the
    # marker a fix would add; "missed month" alone matches incidental prose, which is how an
    # earlier version of this test passed while the gap was still there.
    reconciles = "counts as a missed month" in _ROADMAP.lower()
    assert not says_monthly or reconciles


@pytest.mark.xfail(strict=True, reason="SOFT 6: step 2 marks 'who runs it, and on what machine' as SPEC NEEDED, while step 4 states 'Decided — it runs on the Hetzner box'. Either step 2's question is stale or step 4 decided something step 2 says is open.")
def test_step_2_and_step_4_agree_about_the_machine():
    step2_open = "who runs it, and on what machine" in _ROADMAP
    step4_decided = "it runs on the Hetzner box" in _ROADMAP
    assert not (step2_open and step4_decided)


@pytest.mark.xfail(strict=True, reason="SOFT 7: step 1 says of the columns 'Nothing more to decide there', then the ADR-034 decision says those columns get renamed and step 3 says METHODOLOGY_VERSION bumps at that rename. At least two decisions about the columns are pending.")
def test_nothing_more_to_decide_is_not_contradicted_later():
    claims_settled = "Nothing more to decide there" in _ROADMAP
    later_decisions = "renamed" in _ROADMAP and "bumps once" in _ROADMAP
    assert not (claims_settled and later_decisions)


@pytest.mark.xfail(strict=True, reason="SOFT 8: 'All three conflict targets... More targets will follow' has no closure condition. If a fourth target arrives, does the roadmap regress from done to not-done? Unstated, so 'done' is not assessable at a point in time.")
def test_the_target_set_has_a_closure_condition():
    open_ended = "More targets" in _ROADMAP or "more targets" in _ROADMAP
    has_closure = "as of" in _ROADMAP.lower() and "target" in _ROADMAP.lower() and "frozen" in _ROADMAP.lower()
    assert not open_ended or has_closure
