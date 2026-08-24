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


def _sections():
    """Entry ids grouped by the section they sit in, plus which are titled (RESOLVED).

    An entry counts as resolved if it sits under `## Resolved Concerns` **or** carries
    `(RESOLVED)` in its heading -- the register uses both, marking in place when an entry's
    narrative is still load-bearing for the entries that cross-reference it.
    """
    lines = REGISTER.read_text().split("\n")
    open_at = next(i for i, line in enumerate(lines) if line.startswith("## Open Concerns"))
    res_at = next(i for i, line in enumerate(lines) if line.startswith("## Resolved Concerns"))

    def ids(lo, hi):
        return [
            re.match(r"### (C-\d+)", line)[1]
            for line in lines[lo:hi]
            if re.match(r"### C-\d+", line)
        ]

    in_open = ids(open_at, res_at)
    in_resolved = ids(res_at, len(lines))
    marked = {
        re.match(r"### (C-\d+)", line)[1]
        for line in lines
        if re.match(r"### C-\d+", line) and "(RESOLVED)" in line
    }
    return in_open, in_resolved, marked


def _header():
    head = REGISTER.read_text()[:2000]
    return {
        field: int(re.search(rf"{field} Concerns\s*\|\s*(\d+)", head)[1])
        for field in ("Total", "Open", "Resolved")
    }


def test_header_counts_match_the_entries():
    in_open, in_resolved, marked = _sections()
    actual_open = [c for c in in_open if c not in marked]
    actual_resolved = len(in_resolved) + (len(in_open) - len(actual_open))
    header = _header()

    assert header["Total"] == len(in_open) + len(in_resolved)
    assert header["Open"] == len(actual_open)
    assert header["Resolved"] == actual_resolved


def test_the_counts_are_internally_consistent():
    """The check that used to be the only one. Kept, because it is still necessary -- it was
    never sufficient."""
    header = _header()
    assert header["Open"] + header["Resolved"] == header["Total"]


def test_no_entry_id_appears_twice():
    """A duplicated id makes every count ambiguous and breaks cross-references silently."""
    in_open, in_resolved, _ = _sections()
    all_ids = in_open + in_resolved
    duplicates = {c for c in all_ids if all_ids.count(c) > 1}
    assert not duplicates, f"duplicate register ids: {sorted(duplicates)}"
