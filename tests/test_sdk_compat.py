"""Unit tests for the _as_dict() / _get() SDK compatibility layer.

Uses REAL Appwrite SDK 19.2.0 model classes — not fakes.
These tests define the contract: after _as_dict(), every SDK response
is a flat dict with $-prefixed system keys and user-defined keys at
the same level, matching SDK 13.3.0 dict shape.
"""

import pytest
from types import SimpleNamespace

from appwrite.models.document import Document
from appwrite.models.document_list import DocumentList
from appwrite.models.file import File
from appwrite.models.file_list import FileList
from appwrite.models.preferences import Preferences
from appwrite.models.bucket import Bucket

from views_faoapi.managers.appwrite import _as_dict, _get

pytestmark = pytest.mark.layer4_infra


# --------------- helpers ---------------

def _make_document(**user_fields):
    base = {
        "$id": "doc_abc",
        "$sequence": "1",
        "$collectionId": "coll1",
        "$databaseId": "db1",
        "$createdAt": "2026-01-01T00:00:00.000+00:00",
        "$updatedAt": "2026-01-01T00:00:00.000+00:00",
        "$permissions": [],
    }
    base.update(user_fields)
    return Document.with_data(base)


def _make_file():
    return File.model_validate({
        "$id": "file_xyz",
        "bucketId": "bucket1",
        "$createdAt": "2026-01-01T00:00:00.000+00:00",
        "$updatedAt": "2026-01-01T00:00:00.000+00:00",
        "$permissions": [],
        "name": "forecast.parquet",
        "signature": "abc123",
        "mimeType": "application/octet-stream",
        "sizeOriginal": 1024,
        "sizeActual": 512,
        "chunksTotal": 1,
        "chunksUploaded": 1,
        "encryption": False,
        "compression": "none",
    })


# =============== _as_dict tests ===============

class TestAsDict:

    def test_dict_passthrough(self):
        d = {"$id": "x", "fileId": "f1", "total": 5}
        assert _as_dict(d) is d

    def test_document_preserves_user_data(self):
        doc = _make_document(fileId="f1", category="forecast", filename="test.parquet")
        result = _as_dict(doc)
        assert isinstance(result, dict)
        assert result["fileId"] == "f1"
        assert result["category"] == "forecast"
        assert result["filename"] == "test.parquet"

    def test_document_has_system_fields(self):
        doc = _make_document(fileId="f1")
        result = _as_dict(doc)
        assert result["$id"] == "doc_abc"
        assert result["$collectionId"] == "coll1"
        assert result["$databaseId"] == "db1"
        assert result["$createdAt"] == "2026-01-01T00:00:00.000+00:00"

    def test_document_flat_shape(self):
        """Result must be flat — no nested 'data' key."""
        doc = _make_document(fileId="f1", category="forecast")
        result = _as_dict(doc)
        assert "data" not in result or not isinstance(result.get("data"), dict)

    def test_preferences_preserves_data(self):
        prefs = Preferences.with_data({"theme": "dark", "lang": "en", "notifications": True})
        result = _as_dict(prefs)
        assert isinstance(result, dict)
        assert result.get("theme") == "dark"
        assert result.get("lang") == "en"
        assert result.get("notifications") is True

    def test_file_model(self):
        f = _make_file()
        result = _as_dict(f)
        assert isinstance(result, dict)
        assert result["$id"] == "file_xyz"
        assert result["name"] == "forecast.parquet"
        assert result["sizeOriginal"] in (1024, 1024.0)

    def test_bucket_model(self):
        b = Bucket.model_validate({
            "$id": "bucket1",
            "$createdAt": "2026-01-01",
            "$updatedAt": "2026-01-01",
            "$permissions": [],
            "name": "unfao_bucket",
            "fileSecurity": False,
            "enabled": True,
            "maximumFileSize": 50000000,
            "allowedFileExtensions": [],
            "compression": "none",
            "encryption": False,
            "antivirus": False,
            "transformations": False,
            "totalSize": 0,
        })
        result = _as_dict(b)
        assert isinstance(result, dict)
        assert result["$id"] == "bucket1"
        assert result["name"] == "unfao_bucket"

    def test_simple_namespace(self):
        ns = SimpleNamespace(**{"$id": "ns1", "documents": [], "total": 0})
        result = _as_dict(ns)
        assert isinstance(result, dict)
        assert result["$id"] == "ns1"

    def test_document_list_items(self):
        """Each document in a DocumentList should convert correctly."""
        dl = DocumentList.with_data({
            "total": 2,
            "documents": [
                {"$id": "d1", "$sequence": "1", "$collectionId": "c1", "$databaseId": "db1",
                 "$createdAt": "", "$updatedAt": "", "$permissions": [],
                 "fileId": "f1", "category": "forecast"},
                {"$id": "d2", "$sequence": "2", "$collectionId": "c1", "$databaseId": "db1",
                 "$createdAt": "", "$updatedAt": "", "$permissions": [],
                 "fileId": "f2", "category": "historical"},
            ],
        })
        for doc in dl.documents:
            result = _as_dict(doc)
            assert "fileId" in result, f"fileId missing from {result}"
            assert "$id" in result, f"$id missing from {result}"


# =============== _get tests ===============

class TestGet:

    def test_dollar_id_on_document(self):
        doc = _make_document(fileId="f1")
        assert _get(doc, "$id") == "doc_abc"

    def test_dollar_id_on_dict(self):
        d = {"$id": "dict1", "name": "test"}
        assert _get(d, "$id") == "dict1"

    def test_camel_case_alias(self):
        doc = _make_document(fileId="file123")
        assert _get(doc, "fileId") == "file123"

    def test_regular_field_on_document_list(self):
        dl = DocumentList.with_data({
            "total": 7,
            "documents": [],
        })
        assert _get(dl, "total") in (7, 7.0)

    def test_documents_field(self):
        dl = DocumentList.with_data({
            "total": 1,
            "documents": [
                {"$id": "d1", "$sequence": "1", "$collectionId": "c1", "$databaseId": "db1",
                 "$createdAt": "", "$updatedAt": "", "$permissions": [],
                 "fileId": "f1"},
            ],
        })
        docs = _get(dl, "documents")
        assert docs is not None
        assert len(docs) == 1

    def test_files_field_on_file_list(self):
        fl = FileList.model_validate({
            "total": 1,
            "files": [{
                "$id": "f1", "bucketId": "b1",
                "$createdAt": "", "$updatedAt": "", "$permissions": [],
                "name": "test.txt", "signature": "", "mimeType": "",
                "sizeOriginal": 0, "sizeActual": 0,
                "chunksTotal": 0, "chunksUploaded": 0,
                "encryption": False, "compression": "none",
            }],
        })
        files = _get(fl, "files")
        assert files is not None
        assert len(files) == 1

    def test_missing_key_returns_none(self):
        doc = _make_document(fileId="f1")
        assert _get(doc, "nonexistent") is None

    def test_dollar_created_at(self):
        doc = _make_document(fileId="f1")
        assert _get(doc, "$createdAt") == "2026-01-01T00:00:00.000+00:00"

    def test_nested_document_id_extraction(self):
        """The pattern from line 916: extract $id from first document in a search result."""
        dl = DocumentList.with_data({
            "total": 1,
            "documents": [
                {"$id": "target_doc", "$sequence": "1", "$collectionId": "c1", "$databaseId": "db1",
                 "$createdAt": "", "$updatedAt": "", "$permissions": [],
                 "fileId": "f1"},
            ],
        })
        docs = _get(dl, "documents")
        first_doc_dict = _as_dict(docs[0])
        assert first_doc_dict["$id"] == "target_doc"
        assert first_doc_dict["fileId"] == "f1"


# ============================================================
# C-23: list_documents deprecation tripwire tests
# ============================================================


class TestListDocumentsDeprecation:
    """Tripwire tests for C-23: databases.list_documents deprecated in SDK 19.2.0.
    Replacement (tablesDB.list_rows) does not yet exist in any released SDK.
    These tests break when the SDK upgrade makes migration acute."""

    def test_list_documents_method_exists(self):
        """Tripwire: fails when SDK removes list_documents, pointing to C-23."""
        from appwrite.services.databases import Databases
        assert hasattr(Databases, "list_documents"), (
            "Databases.list_documents has been removed. "
            "See risk register C-23: migrate 5 call sites in appwrite.py "
            "to the replacement API (tablesDB.list_rows or equivalent)."
        )

    def test_list_documents_call_sites_inventory(self):
        """Structural: count of list_documents calls in the appwrite package must match inventory.
        Scans every module in managers/appwrite/ so it survives the package split."""
        import ast
        from pathlib import Path

        pkg = Path("src/views_faoapi/managers/appwrite")
        count = 0
        for py in sorted(pkg.glob("*.py")):
            tree = ast.parse(py.read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "list_documents"
                ):
                    count += 1
        assert count == 5, (
            f"Expected 5 list_documents call sites (C-23 inventory), found {count}. "
            "Update the migration inventory in the risk register."
        )

    def test_list_documents_emits_deprecation_warning(self):
        """Verifies SDK emits DeprecationWarning for list_documents.
        When this stops emitting (method removed), test_list_documents_method_exists fails first."""
        import warnings
        from unittest.mock import MagicMock
        from appwrite.services.databases import Databases

        mock_client = MagicMock()
        db = Databases(mock_client)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            try:
                db.list_documents("db", "coll")
            except Exception:
                pass
            deprecation_warnings = [
                x for x in w if issubclass(x.category, DeprecationWarning)
            ]
            assert len(deprecation_warnings) > 0, "No DeprecationWarning emitted"
