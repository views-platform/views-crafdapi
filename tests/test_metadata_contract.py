"""Cross-repo metadata contract tests (C-51).

Validates that PredictionMetadata field names, types, validation rules,
and to_dict() output structure match the canonical schema constants.
When views-pipeline-core is available locally, also verifies structural
equivalence with FileMetadata (the pipeline-core counterpart).

These are tripwire tests: a metadata field rename in either repo
causes a test failure here, preventing silent cross-repo drift.
"""

import ast
import inspect
from pathlib import Path

import pytest

from views_crafdapi.managers.prediction import (
    PREDICTION_METADATA_FIELDS,
    SYSTEM_METADATA_FIELDS,
    PredictionMetadata,
)

pytestmark = pytest.mark.layer5_audit

APPWRITE_PKG = Path(__file__).resolve().parent.parent / "src" / "views_crafdapi" / "managers" / "appwrite"


def _extract_fixed_attributes_from_ast():
    """Parse the appwrite package to extract fixed_attributes key-value structures.

    Walks the AST of every module in ``managers/appwrite/`` looking for
    ``fixed_attributes = [...]`` and extracts each dict literal's key/type/size/
    required values. Scanning the whole package (not one file) keeps this robust as
    the package is split into submodules.
    """
    for _py in sorted(APPWRITE_PKG.glob("*.py")):
        tree = ast.parse(_py.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [t for t in node.targets if isinstance(t, ast.Name) and t.id == "fixed_attributes"]
            if not targets or not isinstance(node.value, ast.List):
                continue

            attrs = []
            for elt in node.value.elts:
                if not isinstance(elt, ast.Dict):
                    continue
                entry = {}
                for k, v in zip(elt.keys, elt.values):
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                        entry[k.value] = v.value
                attrs.append(entry)
            return attrs

    raise RuntimeError("fixed_attributes not found in the appwrite package AST")


# ---------------------------------------------------------------------------
# Class 1: PredictionMetadata ↔ contract constants (always runs)
# ---------------------------------------------------------------------------

class TestPredictionMetadataContract:

    def test_to_dict_keys_match_contract_fields(self):
        meta = PredictionMetadata(
            loa="pgm", name="test", type="ensemble",
            targets=["pred_ln_sb_best"], category="forecast",
            description="test description",
        )
        assert set(meta.to_dict().keys()) == set(PREDICTION_METADATA_FIELDS.keys())

    def test_to_dict_without_description_matches_required_fields(self):
        meta = PredictionMetadata(
            loa="pgm", name="test", type="ensemble",
            targets=["pred_ln_sb_best"], category="forecast",
        )
        required = {k for k, v in PREDICTION_METADATA_FIELDS.items() if v["required"]}
        assert set(meta.to_dict().keys()) == required

    def test_category_allowed_values_match_contract(self):
        allowed = PREDICTION_METADATA_FIELDS["category"]["allowed"]
        for val in allowed:
            PredictionMetadata(
                loa="pgm", name="test", type="ensemble",
                targets=["t"], category=val,
            )
        with pytest.raises(ValueError):
            PredictionMetadata(
                loa="pgm", name="test", type="ensemble",
                targets=["t"], category="not_a_valid_category",
            )

    def test_constructor_params_match_contract_fields(self):
        sig = inspect.signature(PredictionMetadata.__init__)
        params = set(sig.parameters.keys()) - {"self"}
        assert params == set(PREDICTION_METADATA_FIELDS.keys())


# ---------------------------------------------------------------------------
# Class 2: System metadata ↔ appwrite.py fixed_attributes (always runs)
# ---------------------------------------------------------------------------

class TestSystemMetadataContract:

    def test_system_fields_match_appwrite_fixed_attributes(self):
        attrs = _extract_fixed_attributes_from_ast()
        extracted_keys = {a["key"] for a in attrs}
        assert extracted_keys == set(SYSTEM_METADATA_FIELDS.keys())

    def test_system_field_types_match_appwrite(self):
        attrs = _extract_fixed_attributes_from_ast()
        for attr in attrs:
            key = attr["key"]
            contract = SYSTEM_METADATA_FIELDS[key]
            assert attr["type"] == contract["type"], f"{key}: type mismatch"
            assert attr["required"] == contract["required"], f"{key}: required mismatch"
            if "size" in contract:
                assert attr.get("size") == contract["size"], f"{key}: size mismatch"


# ---------------------------------------------------------------------------
# Class 3: Cross-repo equivalence (skips when pipeline-core unavailable)
# ---------------------------------------------------------------------------

class TestCrossRepoEquivalence:

    @pytest.fixture(autouse=True)
    def _import_pipeline_core(self):
        mod = pytest.importorskip(
            "views_pipeline_core.modules.datastore.datastore",
            reason="views-pipeline-core not available",
        )
        self.FileMetadata = mod.FileMetadata

    def test_filemetadata_constructor_matches_prediction_metadata(self):
        pred_sig = inspect.signature(PredictionMetadata.__init__)
        file_sig = inspect.signature(self.FileMetadata.__init__)

        pred_params = {
            k: v.default for k, v in pred_sig.parameters.items() if k != "self"
        }
        file_params = {
            k: v.default for k, v in file_sig.parameters.items() if k != "self"
        }
        assert set(pred_params.keys()) == set(file_params.keys()), (
            f"Parameter name mismatch: "
            f"PredictionMetadata has {set(pred_params.keys())}, "
            f"FileMetadata has {set(file_params.keys())}"
        )
        for key in pred_params:
            assert pred_params[key] == file_params[key], (
                f"Default mismatch for '{key}': "
                f"PredictionMetadata={pred_params[key]}, FileMetadata={file_params[key]}"
            )

    def test_filemetadata_to_dict_output_identical(self):
        inputs = dict(
            loa="pgm", name="test_model", type="ensemble",
            targets=["pred_ln_sb_best", "pred_ln_ns_best"],
            category="forecast", description="test",
        )
        pred_dict = PredictionMetadata(**inputs).to_dict()
        file_dict = self.FileMetadata(**inputs).to_dict()
        assert pred_dict == file_dict, (
            f"Wire format mismatch:\n"
            f"  PredictionMetadata: {pred_dict}\n"
            f"  FileMetadata:       {file_dict}"
        )
