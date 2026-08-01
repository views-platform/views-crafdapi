"""Falsification tests for the claim: 'we understand the splash zone
of renaming priogrid_gid to priogrid_id.'

Key findings:
- 1,071 data files embed a priogrid column name; 774 use priogrid_gid
- 6 distinct names exist for the same concept (pgid, priogrid_gid,
  priogrid_id, pg_id, cell_id, gid)
- Source-code rename without data regeneration creates schema mismatch
"""
from pathlib import Path

import pytest

pytestmark = pytest.mark.xfail(
    strict=False,
    reason="documents open priogrid concerns C-61..C-65 (see #79)",
)

PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.requires_sibling_repos("views-datafactory")
class TestP1_SerializedDataSchemaRisk:
    """774 parquet files embed priogrid_gid in their Arrow schema.
    A source-code-only rename creates an immediate schema mismatch."""

    @pytest.fixture
    def datafactory_parquets(self):
        # The repo is guaranteed present by the class marker; the data/ subdir
        # is a finer dependency that may be absent even when the repo is checked out.
        data_dir = PLATFORM_ROOT / "views-datafactory" / "data"
        if not data_dir.exists():
            pytest.skip("views-datafactory data not available locally")
        return sorted(data_dir.rglob("*.parquet"))

    def test_no_priogrid_gid_in_datafactory_parquet_schemas(self, datafactory_parquets):
        """After a global rename, datafactory parquet files must be
        regenerated — they currently embed priogrid_gid in their schema."""
        pd = pytest.importorskip("pandas")
        gid_files = []
        for f in datafactory_parquets[:20]:  # sample first 20 to keep fast
            try:
                df = pd.read_parquet(f)
                all_names = set(df.columns) | set(df.index.names)
                if "priogrid_gid" in all_names:
                    gid_files.append(f.name)
            except Exception:
                pass
        assert len(gid_files) == 0, (
            f"{len(gid_files)} datafactory parquet file(s) embed 'priogrid_gid' in "
            "their schema. Source-code rename alone creates a schema mismatch — "
            "these files must be regenerated."
        )


@pytest.mark.requires_sibling_repos("views-pipeline-core", "views-datafactory")
class TestP3_NamingFragmentation:
    """The same integer has 6 names across the platform.
    Standardization is a 6-way consolidation, not a 2-name rename."""

    _CORE_REPOS = {
        "views-pipeline-core": "views_pipeline_core",
        "views-datafactory": "src",
    }

    def test_only_one_canonical_name_in_source(self):
        """After standardization, source code should use exactly one name
        for the PRIO-GRID cell identifier."""
        import re
        names = {"priogrid_gid", "priogrid_id", "pgid", "pg_id", "cell_id"}
        found_names = set()
        for repo, src_dir in self._CORE_REPOS.items():
            repo_path = PLATFORM_ROOT / repo / src_dir
            if not repo_path.exists():
                continue
            for f in repo_path.rglob("*.py"):
                source = f.read_text()
                for name in names:
                    if re.search(rf'\b{name}\b', source):
                        found_names.add(name)
        assert len(found_names) <= 1, (
            f"Found {len(found_names)} distinct names for the PRIO-GRID cell ID "
            f"in core repos: {sorted(found_names)}. Standardization must address "
            "all variants, not just priogrid_gid → priogrid_id."
        )
