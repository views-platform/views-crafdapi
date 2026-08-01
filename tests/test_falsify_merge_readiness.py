"""Failing test stubs from falsification audit of merge readiness.

Claim: All 6 QA issues (#28–#33) are correctly resolved and
integration/qa-hardening is 100% ready to merge into development.

Verdict: CONTESTED — 1 soft falsification (stale documentation counts).

Run: PYTHONPATH=src pytest tests/test_falsify_merge_readiness.py -v
"""

import pytest

pytestmark = pytest.mark.layer5_audit


class TestF3_StaleDocumentationCounts:
    """TESTING.md and risk register C-27 document 430 total tests and
    Layer 3 at 52 tests. Actual counts are 433 and 55 respectively,
    because issue #33 added 3 parametrized tests to test_api_endpoints.py
    without updating the documentation."""

    @pytest.mark.skip(reason="F-3: Fixed — TESTING.md and C-27 counts updated (430→433, 52→55, 45→48)")
    def test_testing_md_total_matches_actual(self):
        pytest.fail(
            "TESTING.md line 3 says 430 tests, actual is 433. "
            "Line 35 says Layer 3 has 52, actual is 55. "
            "Line 41 says test_api_endpoints.py has 45, actual is 48."
        )
