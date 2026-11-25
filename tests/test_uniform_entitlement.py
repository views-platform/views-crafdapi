"""Every partner is served one loaded copy of the forecast (ADR-044 §4). That is right only while
the service specification promises every partner the same data. If the promise changes, this
test fails, so the change cannot go unnoticed."""

from pathlib import Path

SPECIFICATION = Path(__file__).resolve().parents[1] / "docs" / "SERVICE_SPECIFICATION.md"
PROMISE = "**Same data for everyone, always.** Separate keys exist only so that one partner can be switched off."


def test_the_specification_still_promises_every_partner_the_same_data():
    assert PROMISE in SPECIFICATION.read_text(), (
        "The service specification no longer promises every partner the same data. The service "
        "serves all partners one loaded copy (ADR-044 §4), and the platform's storage contract "
        "accepts that only under this promise ('uniform entitlement'). Before changing it, keep "
        "each partner's data apart again (ADR-027) and tell views-appwrite."
    )
