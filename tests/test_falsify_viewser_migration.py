"""Falsification tests for the viewser → Appwrite historical-backend migration, and the
resolution of the `priogrid_gid` shim contradiction (register C-61, epic #325 S7).

Original claim under test: "switching the historical backend from viewser to
views-datafactory is 100% done."

Resolution (2026-07-31): the migration is done, but the `priogrid_gid → priogrid_id`
normalization shim in `handlers.py` is **PERMANENT, not dead code**. faoapi's own bucket
serves `priogrid_id`, but the upstream platform still emits a mixed vocabulary — 774 platform
parquet files bake `priogrid_gid` into their Arrow schema (register C-62/C-63). The shim
normalizes that at faoapi's input boundary and must not be removed. These tests now guard that
permanence (the shim is present; no comment treats it as temporary), resolving the prior
contradiction where the code comment said "temporary" while the CICs documented it as a stable
contract.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
APPWRITE_CACHE = REPO_ROOT / "appwrite_cache"


class TestAppwriteDataUsesPriogridId:
    """faoapi's own Appwrite bucket serves `priogrid_id`. Note this does NOT prove the
    handlers.py shim is dead: the shim normalizes the *upstream* mixed vocabulary (C-62/C-63)
    at the input boundary, and the platform still emits `priogrid_gid`."""

    @pytest.fixture
    def cached_parquet_files(self):
        files = sorted(APPWRITE_CACHE.rglob("*.parquet"))
        if not files:
            pytest.skip("No cached Appwrite parquet files available")
        return files

    def test_no_priogrid_gid_in_appwrite_data(self, cached_parquet_files):
        """faoapi's own cached parquet files use priogrid_id — scoped to this bucket, not a
        proof about the platform-wide vocabulary (which still carries priogrid_gid, C-62/C-63)."""
        pd = pytest.importorskip("pandas")
        for f in cached_parquet_files:
            df = pd.read_parquet(f)
            all_names = set(df.index.names) | set(df.columns)
            assert "priogrid_gid" not in all_names, (
                f"{f.name} contains 'priogrid_gid' — the shim may still be needed"
            )


class TestPriogridShimIsPermanent:
    """C-61 resolved to PERMANENT (epic #325 S7): the `priogrid_gid` normalization shim is a
    stable input contract, not a temporary hack. These guards keep the record consistent — no
    longer xfail, because the contradiction is settled rather than open."""

    def test_no_temporary_removal_comment(self):
        """handlers.py must not treat Viewser as a future dependency ('when Viewser is
        updated') — the shim is permanent, so no comment may schedule its removal."""
        handlers = SRC_ROOT / "views_faoapi" / "data" / "handlers" / "grid_dataset.py"
        source = handlers.read_text()
        viewser_future_refs = re.findall(
            r"#.*(?:when|until|after).*[Vv]iewser.*(?:updated|fixed|changed)",
            source,
        )
        assert not viewser_future_refs, (
            f"Found comment(s) treating Viewser as a future dependency: {viewser_future_refs}. "
            "The priogrid shim is permanent (C-61/C-62/C-63) — do not schedule its removal."
        )

    def test_priogrid_gid_shim_is_present(self):
        """The `priogrid_gid → priogrid_id` normalization shim must remain in
        `_GridDataset._init_dataframe`: upstream still emits `priogrid_gid` (774 platform parquet
        files, C-62/C-63), so removing it would break ingestion of that data. Guards against a
        well-meaning 'dead code' deletion of a load-bearing permanent contract."""
        handlers = SRC_ROOT / "views_faoapi" / "data" / "handlers" / "grid_dataset.py"
        source = handlers.read_text()
        assert "priogrid_gid" in source, (
            "the permanent priogrid_gid normalization shim is missing from handlers/grid_dataset.py — "
            "upstream still emits priogrid_gid (C-62/C-63); the shim must not be removed."
        )


class TestClaimFramingAccuracy:
    """P3: The claim says 'switched to views-datafactory' but the actual
    backend is Appwrite. views-datafactory is not imported anywhere."""

    def test_no_views_datafactory_import(self):
        """Verify views-datafactory is not imported — confirming the backend
        is Appwrite, not views-datafactory. This test documents the actual
        architecture so the claim can be corrected."""
        py_files = list(SRC_ROOT.rglob("*.py"))
        datafactory_imports = []
        for f in py_files:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"(?:from|import)\s+views_datafactory", line):
                    datafactory_imports.append(f"{f.relative_to(REPO_ROOT)}:{i}")
        assert len(datafactory_imports) == 0, (
            "views-datafactory is imported in source — update this test if "
            "the architecture has changed to actually use it as a backend."
        )
