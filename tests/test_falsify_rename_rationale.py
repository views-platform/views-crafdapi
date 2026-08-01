"""Falsification tests for the claim: 'we understand why we rename
priogrid_gid to priogrid_id.'

Key finding: every source file in views-pipeline-core cites 'ADR-034'
as the rationale for the rename. ADR-034 is about log level standards.
The actual rationale is in ADR-043, which zero source files cite.
"""
import re
from pathlib import Path

import pytest

pytestmark = [
    # Needs views-pipeline-core source on disk; skips cleanly without it (S1 / C-74).
    pytest.mark.requires_sibling_repos("views-pipeline-core"),
    # When the sibling IS present, these document still-open priogrid concerns.
    pytest.mark.xfail(
        strict=False,
        reason="documents open priogrid concerns C-61..C-65 (see #79)",
    ),
]

PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_CORE = PLATFORM_ROOT / "views-pipeline-core"


class TestP5_ADRCitationsPointToCorrectDocument:
    """Every code comment citing the priogrid rename rationale references
    ADR-034 (log levels). The actual ADR is 043 (priogrid naming)."""

    _RENAME_FILES = [
        "views_pipeline_core/data/handlers.py",
        "views_pipeline_core/modules/validation/core_data_sniffer.py",
        "views_pipeline_core/modules/aggregation/aggregator.py",
        "views_pipeline_core/managers/ensemble/ensemble.py",
        "views_pipeline_core/managers/ensemble/dataframe_ensemble.py",
        "views_pipeline_core/managers/prediction/prediction_frame_converter.py",
    ]

    def test_no_wrong_adr_citations(self):
        """Source files should not cite ADR-034 for the priogrid rename.
        ADR-034 is 'Log Level Standards' — unrelated to naming."""
        wrong_citations = []
        for rel_path in self._RENAME_FILES:
            f = PIPELINE_CORE / rel_path
            if f.exists():
                for i, line in enumerate(f.read_text().splitlines(), 1):
                    if re.search(r"ADR.034", line) and "priogrid" in line.lower():
                        wrong_citations.append(f"{rel_path}:{i}")
        assert len(wrong_citations) == 0, (
            f"{len(wrong_citations)} source file(s) cite ADR-034 for the priogrid "
            f"rename. ADR-034 is about log levels. The correct ADR is 043. "
            f"Files: {wrong_citations}"
        )

    def test_correct_adr_is_cited(self):
        """At least one source file should cite ADR-043 for the rename."""
        correct_citations = 0
        for rel_path in self._RENAME_FILES:
            f = PIPELINE_CORE / rel_path
            if f.exists():
                source = f.read_text()
                if re.search(r"ADR.043", source):
                    correct_citations += 1
        assert correct_citations > 0, (
            f"Zero source files cite ADR-043 (the actual priogrid naming ADR). "
            f"All {len(self._RENAME_FILES)} files cite ADR-034 (log levels) instead."
        )


class TestP3_SpatialLevelIndexOrder:
    """spatial.py defines PGM index as (priogrid_gid, month_id) but
    actual data is (month_id, priogrid_gid). Latent bug in dead code."""

    def test_pgm_index_names_time_first(self):
        """SpatialLevel.PGM.index_names should be (month_id, entity)
        to match actual DataFrame convention."""
        spatial = PIPELINE_CORE / "views_pipeline_core" / "domain" / "spatial.py"
        source = spatial.read_text()
        match = re.search(
            r'SpatialLevel\.PGM:\s*\("([^"]+)",\s*"([^"]+)"\)', source
        )
        assert match is not None, "Could not find PGM index_names tuple"
        first, _second = match.group(1), match.group(2)
        assert first == "month_id", (
            f"PGM index_names[0] is '{first}', expected 'month_id'. "
            f"Actual DataFrames use (month_id, entity) order."
        )
