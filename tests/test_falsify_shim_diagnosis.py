"""Falsification tests for the claim: 'we know why the viewser migration
tests failed — the priogrid_gid shim is dead code.'

These tests prove the 'dead code' diagnosis is wrong. The shim is:
1. Tested as expected behavior by existing test suites
2. Documented as a contract guarantee in CICs
3. Present in two independent classes/methods
4. Referenced by the serving layer (not covered by the original test)

C-274 (2026-10-16): probe 4 used to read `managers/api.py` only; the reference moved to
`managers/dataset_service.py` and the probe xpassed against an empty file. It now reads the
whole `managers/` package. A fifth probe — "the ForecastDataset CIC should not promise
`priogrid_gid` if the shim is dead code" — was removed: `_GridDataset.md` now documents the shim
as permanent, which answers it, and under `strict=False` its xpass reported nothing.
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.xfail(
    strict=False,
    reason="documents open priogrid concerns C-61..C-65 (register entries)",
)
# The reason once cited a bare '#79'; in this repo that resolves to an unrelated issue (C-241).

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
DOCS_ROOT = REPO_ROOT / "docs"


class TestP1_MigrationTestCoverageGap:
    """test_priogrid_gid_shim_removed only greps handlers.py, but
    the serving layer also references priogrid_gid."""

    def test_serving_layer_also_references_priogrid_gid(self):
        """`managers/dataset_service.py` lists priogrid_gid in its index exclusion sets
        (it moved there from api.py). The migration test misses this package entirely."""
        managers = SRC_ROOT / "views_crafdapi" / "managers"
        refs = {
            str(f.relative_to(SRC_ROOT)): len(re.findall(r"priogrid_gid", f.read_text()))
            for f in managers.rglob("*.py")
        }
        refs = {f: n for f, n in refs.items() if n}
        assert not refs, (
            f"managers/ references priogrid_gid ({refs}), "
            "not covered by test_priogrid_gid_shim_removed (which only checks handlers.py)"
        )


class TestP2_ShimIsTestedBehavior:
    """Existing tests assert priogrid_gid acceptance WORKS — removing the
    shim breaks them. It is not dead code."""

    def test_existing_tests_exercise_priogrid_gid_rename(self):
        """At least one test outside the falsification suite constructs
        ForecastDataset with priogrid_gid and asserts it gets renamed."""
        test_files = [
            REPO_ROOT / "tests" / "test_dataset_validation.py",
            REPO_ROOT / "tests" / "test_falsify_sprint2_completeness.py",
        ]
        shim_tests = []
        for f in test_files:
            if f.exists():
                source = f.read_text()
                if "priogrid_gid" in source and "accepted_and_renamed" in source:
                    shim_tests.append(f.name)
        assert len(shim_tests) == 0, (
            f"Found {len(shim_tests)} test file(s) that assert priogrid_gid acceptance "
            f"as intended behavior: {shim_tests}. The shim is not dead code — "
            "removing it would break these tests."
        )


class TestP3_TwoIndependentShims:
    """handlers.py has two independent priogrid_gid acceptance paths
    in two different classes."""

    def test_fao_pgm_init_has_own_priogrid_gid_path(self):
        """ForecastDataset.__init__ (around line 1181) accepts priogrid_gid
        as a flat column for auto-indexing — independent from the
        _GridDataset._init_dataframe shim at line 92."""
        # handlers.py was split into a package (S11, #336); the two independent
        # priogrid_gid paths now live in grid_dataset.py (_GridDataset) and
        # forecast_dataset.py (ForecastDataset/ForecastDataset). Scan the whole package.
        pkg = SRC_ROOT / "views_crafdapi" / "data" / "handlers"
        source = "\n".join(p.read_text() for p in sorted(pkg.glob("*.py")))
        lines = source.splitlines()
        shim_locations = []
        for i, line in enumerate(lines, 1):
            if "priogrid_gid" in line and not line.strip().startswith("#"):
                shim_locations.append(i)
        classes = set()
        for loc in shim_locations:
            for i in range(loc - 1, 0, -1):
                if lines[i - 1].strip().startswith("class "):
                    classes.add(lines[i - 1].strip().split("(")[0].replace("class ", ""))
                    break
        assert len(classes) <= 1, (
            f"priogrid_gid acceptance exists in {len(classes)} different classes: "
            f"{classes}. These are independent compatibility layers, not one shim."
        )


class TestP4_CICDocumentsShimAsContract:
    """CICs promise priogrid_gid acceptance — removing the shim would
    violate documented contracts."""

    def test_viewsdataset_cic_does_not_guarantee_priogrid_gid(self):
        """The _GridDataset CIC should not document priogrid_gid acceptance
        if it is truly dead code to be removed."""
        cic = DOCS_ROOT / "CICs" / "_GridDataset.md"
        if not cic.exists():
            pytest.skip("CIC not found")
        content = cic.read_text()
        guarantees = re.findall(r"priogrid_gid", content)
        assert len(guarantees) == 0, (
            f"_GridDataset CIC mentions priogrid_gid {len(guarantees)} time(s) as a "
            "documented guarantee. Removing the shim violates the contract."
        )
