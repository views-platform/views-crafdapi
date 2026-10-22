"""The register header must describe the register.

Added 2026-08-24, after the header was found reading 54 open / 11 resolved against a document
holding 22 and 43. The counts had been maintained by hand and decremented by whoever remembered,
and the only check ever applied was that they summed to the total -- which they did, while both
numbers were wrong. A sum is not a measurement of the thing.

The register is the input to prioritisation. A header claiming 54 open concerns describes a
different project from one with 22, and the cost of the error lands on whoever reads it to decide
what to work on next.
"""

import re
from pathlib import Path

import pytest

REGISTER = Path(__file__).resolve().parent.parent / "reports" / "technical_risk_register.md"

pytestmark = pytest.mark.skipif(
    not REGISTER.exists(), reason="register is not present in this checkout"
)


def _entries():
    """Every entry, with its status derived from the entry's OWN text.

    Section membership is deliberately ignored. The register's headings are decorative: entries
    have been appended to the end of the file for months regardless of heading, so 39 concerns sit
    under `## Register Conventions` and 11 under `## Disagreements`. An earlier version of this
    guard treated the headings as boundaries and therefore enforced a header that was wrong in both
    directions -- see C-298. A guard that codifies an untested assumption makes it harder to
    question, not easier.

    An entry is resolved if it says so: `(RESOLVED)` in the heading, a `| Status |` row naming
    RESOLVED, or a bold `**RESOLVED` marker in the body.
    """
    lines = REGISTER.read_text().split("\n")
    heads = [
        (i, re.match(r"### (C-\d+)(.*)", line))
        for i, line in enumerate(lines)
        if re.match(r"### C-\d+", line)
    ]
    out = []
    for n, (i, m) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = "\n".join(lines[i:end])
        resolved = (
            "RESOLVED" in m.group(2).upper()
            or re.search(r"\|\s*Status\s*\|[^|]*RESOLVED", body) is not None
            or re.search(r"\*\*RESOLVED\b", body) is not None
        )
        out.append((m.group(1), resolved))
    return out


def _header():
    head = REGISTER.read_text()[:2000]
    return {
        field: int(re.search(rf"{field} Concerns\s*\|\s*(\d+)", head)[1])
        for field in ("Total", "Open", "Resolved")
    }


def test_header_counts_match_the_entries():
    entries = _entries()
    resolved = [c for c, r in entries if r]
    header = _header()

    assert header["Total"] == len(entries)
    assert header["Resolved"] == len(resolved)
    assert header["Open"] == len(entries) - len(resolved)


def test_the_counts_are_internally_consistent():
    """The check that used to be the only one. Kept, because it is still necessary -- it was
    never sufficient."""
    header = _header()
    assert header["Open"] + header["Resolved"] == header["Total"]


def test_no_entry_id_appears_twice():
    """A duplicated id makes every count ambiguous and breaks cross-references silently."""
    all_ids = [c for c, _ in _entries()]
    duplicates = {c for c in all_ids if all_ids.count(c) > 1}
    assert not duplicates, f"duplicate register ids: {sorted(duplicates)}"
