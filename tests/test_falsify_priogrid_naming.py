"""Falsification tests for the claim: 'we understand why priogrid_gid
exists alongside priogrid_id.'

The explanation (ADR-043): VIEWSER emits priogrid_gid, pipeline-core
renames at ingestion, everything downstream uses priogrid_id.

These tests prove the explanation is incomplete:
1. views-datafactory (VIEWSER's replacement) also emits priogrid_gid
2. Multiple downstream repos use priogrid_gid after the rename boundary
3. Model configs embed priogrid_gid as a specification, not data flow
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.xfail(
    strict=False,
    reason="documents open priogrid concerns C-61..C-65 (see #79)",
)

PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.requires_sibling_repos("views-datafactory")
class TestP1_DatafactoryAlsoEmitsPriogridGid:
    """ADR-043 says VIEWSER is the sole source of priogrid_gid.
    But views-datafactory — the VIEWSER replacement — also emits it."""

    def test_datafactory_grid_to_dataframe_uses_priogrid_id(self):
        """grid_to_dataframe.py should create MultiIndex with priogrid_id
        if the platform has standardized on that name."""
        f = PLATFORM_ROOT / "views-datafactory" / "src" / "datafactory_adapters" / "grid_to_dataframe.py"
        source = f.read_text()
        assert "priogrid_gid" not in source, (
            "views-datafactory grid_to_dataframe.py emits priogrid_gid. "
            "This is the VIEWSER replacement — if it also uses the old name, "
            "ADR-043's resolution path ('when VIEWSER is updated') is self-defeating."
        )


class TestP2_DownstreamReposViolateRenameBoundary:
    """ADR-043 says all downstream code uses priogrid_id.
    Multiple repos disagree."""

    @pytest.mark.requires_sibling_repos("views-stepshifter")
    def test_stepshifter_error_message_mentions_priogrid_id(self):
        """views-stepshifter validation error message says 'must be priogrid_gid'
        but should say priogrid_id (or both) per ADR-043."""
        f = PLATFORM_ROOT / "views-stepshifter" / "views_stepshifter" / "models" / "validation.py"
        source = f.read_text()
        error_msgs = re.findall(r"must be.*priogrid_gid", source)
        assert len(error_msgs) == 0, (
            f"stepshifter validation.py has {len(error_msgs)} error message(s) "
            "that say 'must be priogrid_gid' — downstream of the rename boundary, "
            "this should reference priogrid_id."
        )

    @pytest.mark.requires_sibling_repos("views-postprocessing")
    def test_postprocessing_tests_use_priogrid_id(self):
        """views-postprocessing integration tests construct DataFrames with
        priogrid_gid — should use priogrid_id (post-boundary)."""
        f = PLATFORM_ROOT / "views-postprocessing" / "tests" / "test_integration.py"
        source = f.read_text()
        refs = re.findall(r"priogrid_gid", source)
        assert len(refs) == 0, (
            f"views-postprocessing test_integration.py has {len(refs)} priogrid_gid "
            "references. Postprocessing is downstream of the rename boundary."
        )


@pytest.mark.requires_sibling_repos("views-models")
class TestP4_ModelConfigsEmbedPreBoundaryName:
    """HydraNet model configs in views-models specify priogrid_gid as
    id_col, identity_cols, and index_names — embedding the pre-boundary
    name into model specifications."""

    _HYDRANET_MODELS = [
        "blazing_meteor", "blue_stranger", "bold_comet",
        "bright_starship", "heavy_freighter", "pink_pirate",
        "purple_alien", "violet_visitor",
    ]

    def test_no_hydranet_config_specifies_priogrid_gid(self):
        """Model config_hyperparameters should not hardcode priogrid_gid
        as the expected column name."""
        models_root = PLATFORM_ROOT / "views-models" / "models"
        offenders = []
        for model in self._HYDRANET_MODELS:
            hp = models_root / model / "configs" / "config_hyperparameters.py"
            if hp.exists() and "priogrid_gid" in hp.read_text():
                offenders.append(model)
        assert len(offenders) == 0, (
            f"{len(offenders)} HydraNet model config(s) embed 'priogrid_gid' as "
            f"id_col/index_names: {offenders}. A global rename must update these "
            "configs AND the data they receive simultaneously."
        )
