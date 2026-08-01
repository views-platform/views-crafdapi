"""Falsification tests for the claim: 'we know why the viewser migration
tests failed — the priogrid_gid shim is dead code.'

These tests prove the 'dead code' diagnosis is wrong. The shim is:
1. Tested as expected behavior by existing test suites
2. Documented as a contract guarantee in CICs
3. Present in two independent classes/methods
4. Referenced in api.py (not covered by the original test)
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.xfail(
    strict=False,
    reason="documents open priogrid concerns C-61..C-65 (see #79)",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
DOCS_ROOT = REPO_ROOT / "docs"


class TestP1_MigrationTestCoverageGap:
    """test_priogrid_gid_shim_removed only greps handlers.py, but
    priogrid_gid also appears in managers/api.py."""

    def test_api_py_also_references_priogrid_gid(self):
        """api.py:587 has priogrid_gid in the index exclusion set.
        The migration test misses this file entirely."""
        api_py = SRC_ROOT / "views_faoapi" / "managers" / "api.py"
        source = api_py.read_text()
        refs = re.findall(r"priogrid_gid", source)
        assert len(refs) == 0, (
            f"managers/api.py has {len(refs)} priogrid_gid reference(s) "
            "not covered by test_priogrid_gid_shim_removed (which only checks handlers.py)"
        )


class TestP2_ShimIsTestedBehavior:
    """Existing tests assert priogrid_gid acceptance WORKS — removing the
    shim breaks them. It is not dead code."""

    def test_existing_tests_exercise_priogrid_gid_rename(self):
        """At least one test outside the falsification suite constructs
        FAO_PGMDataset with priogrid_gid and asserts it gets renamed."""
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
        """FAO_PGMDataset.__init__ (around line 1181) accepts priogrid_gid
        as a flat column for auto-indexing — independent from the
        _GridDataset._init_dataframe shim at line 92."""
        # handlers.py was split into a package (S11, #336); the two independent
        # priogrid_gid paths now live in grid_dataset.py (_GridDataset) and
        # forecast_dataset.py (ForecastDataset/FAO_PGMDataset). Scan the whole package.
        pkg = SRC_ROOT / "views_faoapi" / "data" / "handlers"
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

    def test_fao_pgm_cic_does_not_guarantee_priogrid_gid(self):
        """The FAO_PGMDataset CIC should not document priogrid_gid acceptance
        if it is truly dead code to be removed."""
        cic = DOCS_ROOT / "CICs" / "FAO_PGMDataset.md"
        if not cic.exists():
            pytest.skip("CIC not found")
        content = cic.read_text()
        guarantees = re.findall(r"priogrid_gid", content)
        assert len(guarantees) == 0, (
            f"FAO_PGMDataset CIC mentions priogrid_gid {len(guarantees)} time(s) as "
            "accepted input. Removing the shim violates the contract."
        )
