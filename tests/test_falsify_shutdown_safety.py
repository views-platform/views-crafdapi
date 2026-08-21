"""Falsification audit of the claim, 2026-08-18:

    "it is now safe to shutdown this session."

Repo state, branch/tag sync, the live service, every load-bearing measurement, cross-repo
filings and register/issue linkage all survived. Two things did not, and both are in
`deployment/RELEASE_RUNBOOK.md` — the one document read under time pressure, by someone with
sudo, against production.

Both are `xfail(strict=True)`, matching `test_falsify_done_claim.py`: they fail today, and the
moment the runbook is fixed they XPASS — which strict mode turns into a suite failure, forcing
whoever fixed it to delete the marker and the entry. A guard that goes quiet when the finding is
fixed is how a register entry outlives its cause.

Delete a test only when its finding is genuinely fixed, never to make the suite green.
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer5_audit

RUNBOOK = Path(__file__).resolve().parent.parent / "deployment" / "RELEASE_RUNBOOK.md"


# ── HARD — the runbook states something about current behaviour that is false ─────────
# Step 7 tells the reader the forecast/historical checks will report 503 and that this is
# "expected and correct until the producer's first delivery", and that "what must pass now is
# ping and version = 0.1.0". The first delivery happened on 2026-07-27; the deployed version is
# 0.4.0 and smoke.py returns ALL PASS. A reader following Step 7 to verify a deploy today is
# told to expect a failure state that would now indicate a real outage — and told to accept it.
#
# This was identified as stale during the v0.4.0 deploy and explicitly promised as a follow-up
# ("I'll fix that stale text in the runbook separately"). It was not done, and nothing outside
# that conversation recorded the promise — which is the part that makes it a shutdown-safety
# finding rather than only a documentation one.


# RESOLVED in v0.5.0: Step 7 is now explicitly framed as the record of the 2026-08-02 first
# stand-up, with the current expectation (ALL PASS, and a 503 meaning a real outage) stated
# alongside it. The xfail is gone; this is an ordinary guard now, and it fails if the stale
# wording ever comes back.
def test_runbook_does_not_tell_the_reader_to_expect_the_pre_delivery_failure_state():
    """The bucket is no longer empty, so 503s are no longer the correct expectation."""
    text = RUNBOOK.read_text()
    assert "What must pass now is `ping` and `version = 0.1.0`" not in text, (
        "RELEASE_RUNBOOK.md Step 7 still says the current expectation is `version = 0.1.0` and "
        "that the forecast/historical 503s are 'expected and correct until the producer's first "
        "delivery'. That delivery landed 2026-07-27; the service serves 0.4.0 and smoke.py "
        "returns ALL PASS. Either mark Step 7 explicitly as the historical first stand-up, or "
        "restate its expectations for a service that has data."
    )


# ── SOFT — the fix for the stale-tag trap left the trap's mechanism in place ──────────
# The "Every future release" block previously named `v0.2.0` in two places, two releases after
# that stopped being current: pasting it would silently roll production back while looking like
# a deploy. It was rewritten to a single `TAG=` variable, and the runbook now claims "One
# variable, changed once, is why it is written this way" — presenting the problem as solved.
#
# It is reduced, not solved. `TAG=v0.4.0` is still a real-looking tag that a reader in a hurry
# can paste unedited, and it will be stale at the next release exactly as `v0.2.0` was. A
# placeholder (`TAG=vX.Y.Z`) fails loudly at the deploy gate instead of succeeding at deploying
# the wrong version. This is the same defect class the rewrite was meant to close.


# RESOLVED in v0.5.0: the block reads `TAG=vX.Y.Z`. An unedited paste now fails at the deploy
# gate (`checkout-deploy-tag.sh` cannot resolve `refs/tags/vX.Y.Z`) instead of quietly deploying
# a stale real tag. The guard stays, un-xfailed, so the next release cannot reintroduce it —
# which is the whole point, since v0.4.0 was itself the reintroduction of the v0.2.0 trap.
def test_runbook_release_block_does_not_hardcode_a_real_looking_tag():
    """A stale placeholder fails safe; a stale real tag deploys the wrong version silently."""
    text = RUNBOOK.read_text()
    hardcoded = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("TAG=v") and "X.Y.Z" not in line
    ]
    assert not hardcoded, (
        f"RELEASE_RUNBOOK.md's copy-paste release block hardcodes {hardcoded!r}. That is the "
        f"same shape as the `v0.2.0` it replaced — a real-looking tag that pastes cleanly and "
        f"silently deploys the wrong version. Use a placeholder such as `TAG=vX.Y.Z` so an "
        f"unedited paste fails at the deploy gate instead of succeeding wrongly."
    )


# ── Every runbook block that needs the box must say so ────────────────────────────────
# The recurring release block labelled which USER to be (`as your own user`, `as the deploy
# user`) and never which HOST. Run from a laptop it fails with `sudo: unknown user
# views-crafdapi-deploy` — safe, but only after the reader has been told to paste it. This is
# the third defect of the same shape after C-265 and C-266: runbook text that reads correctly
# and misleads in use.


def test_runbook_blocks_naming_the_deploy_user_say_they_run_on_the_box():
    """`views-crafdapi-deploy` exists only on the box, so any block invoking it must say so."""
    text = RUNBOOK.read_text()
    lines = text.splitlines()
    # Only lines that INVOKE the account, not prose that mentions it — `sudo -u` / `sudo -iu`.
    invokes = ("sudo -u views-crafdapi-deploy", "sudo -iu views-crafdapi-deploy")
    unmarked = []
    for i, line in enumerate(lines):
        if not any(tok in line for tok in invokes):
            continue
        # look back for a host marker inside the enclosing block / its preamble
        window = "\n".join(lines[max(0, i - 25):i])
        if "ON THE BOX" not in window:
            unmarked.append((i + 1, line.strip()[:80]))
    assert not unmarked, (
        "RELEASE_RUNBOOK.md invokes the deploy user without an 'ON THE BOX' marker within the "
        f"preceding lines: {unmarked}. A reader pasting that on a laptop gets "
        "`sudo: unknown user views-crafdapi-deploy`."
    )
