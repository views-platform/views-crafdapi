"""Falsification tests for the claim: 'we understand the splash zone
of renaming priogrid_id BACK TO priogrid_gid.'

Key finding: this direction breaks the external CRAF'd API contract.
The API response JSON includes priogrid_id as a key — renaming it
is a breaking change for external consumers.
"""
import re
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.requires_sibling_repos("views-pipeline-core")
class TestP4_EntityIdConventionProgrammatic:
    """The {entity}_id convention is enforced programmatically,
    not just aesthetically. Breaking it for priogrid creates
    an inconsistency in lookup tables and validators."""

    def test_prediction_frame_converter_uses_entity_id_pattern(self):
        """_LEVEL_TO_ENTITY_COL maps level → entity column name.
        All values should follow {entity}_id pattern."""
        converter = (PLATFORM_ROOT / "views-pipeline-core" /
                     "views_pipeline_core" / "managers" / "prediction" /
                     "prediction_frame_converter.py")
        source = converter.read_text()
        # Extract all values from the _LEVEL_TO_ENTITY_COL dict
        values = re.findall(r'"(?:cm|pgm)":\s*"([^"]+)"', source)
        for v in values:
            assert v.endswith("_id"), (
                f"Entity column '{v}' in _LEVEL_TO_ENTITY_COL does not follow "
                "the {entity}_id naming convention. priogrid_gid would break this."
            )

    def test_cdataset_validates_priogrid_id(self):
        """CDataset.validate_indices() hardcodes 'priogrid_id' as required.
        Renaming to priogrid_gid would cause all CDataset construction to
        raise ValueError."""
        handlers = (PLATFORM_ROOT / "views-pipeline-core" /
                    "views_pipeline_core" / "data" / "handlers.py")
        source = handlers.read_text()
        assert re.search(r'!= "priogrid_id"', source), (
            "CDataset no longer validates priogrid_id — "
            "check if validation was removed or changed"
        )
