"""Failing test stubs from falsification audit of dual-SDK compatibility layer.

Each test demonstrates a concrete failure mode where _get()/_as_dict() either
silently returns wrong data or crashes, proving the compatibility layer is incomplete.

Run: PYTHONPATH=src pytest tests/test_falsify_dual_sdk.py -v
"""

import pytest
from pydantic import BaseModel, Field
from types import SimpleNamespace

from views_crafdapi.managers.appwrite import _get, _as_dict

pytestmark = pytest.mark.layer5_audit


# --- Fake Appwrite SDK 19.2.0 Pydantic models ---

class FakeDocument(BaseModel):
    id: str = Field(alias="$id")
    collection_id: str = Field(alias="$collectionId", default="")
    database_id: str = Field(alias="$databaseId", default="")
    file_id: str = Field(alias="fileId", default="")

    model_config = {"populate_by_name": True}


class FakeDocumentList(BaseModel):
    total: int = 0
    documents: list = []


class FakeFile(BaseModel):
    id: str = Field(alias="$id")
    name: str = ""
    size_original: int = Field(alias="sizeOriginal", default=0)

    model_config = {"populate_by_name": True}


class FakeFileList(BaseModel):
    total: int = 0
    files: list = []


class FakeSession(BaseModel):
    id: str = Field(alias="$id")
    user_id: str = Field(alias="userId", default="")
    created_at: str = Field(alias="$createdAt", default="")

    model_config = {"populate_by_name": True}


class FakeUser(BaseModel):
    id: str = Field(alias="$id")
    email: str = ""
    name: str = ""

    model_config = {"populate_by_name": True}



# ============================================================
# P-1: _get() fails on Pydantic $-prefixed aliases
# ============================================================

class TestP1_GetAliasFailure:
    """_get(pydantic_model, "$id") must return the ID, not None."""

    def test_get_dollar_id_on_pydantic_model(self):
        doc = FakeDocument(**{"$id": "doc123", "fileId": "f1"})
        result = _get(doc, "$id")
        assert result == "doc123", f"_get returned {result!r} instead of 'doc123'"

    def test_get_dollar_id_on_dict(self):
        doc = {"$id": "doc123", "fileId": "f1"}
        result = _get(doc, "$id")
        assert result == "doc123"

    def test_get_dollar_created_at_on_pydantic(self):
        session = FakeSession(**{"$id": "s1", "userId": "u1", "$createdAt": "2025-01-01"})
        result = _get(session, "$createdAt")
        assert result == "2025-01-01", f"_get returned {result!r}"

    def test_get_camel_case_alias_on_pydantic(self):
        doc = FakeDocument(**{"$id": "doc1", "fileId": "file1"})
        result = _get(doc, "fileId")
        assert result == "file1", f"_get returned {result!r} for fileId alias"

    def test_line_916_document_id_extraction(self):
        """Reproduces line 916: document_id = _get(_get(search_result, "documents")[0], "$id")"""
        search_result = FakeDocumentList(
            total=1,
            documents=[FakeDocument(**{"$id": "doc456", "fileId": "f1"})],
        )
        docs = _get(search_result, "documents")
        assert docs is not None and len(docs) > 0
        document_id = _get(docs[0], "$id")
        assert document_id == "doc456", f"document_id is {document_id!r}, expected 'doc456'"


# ============================================================
# P-2: Unconverted raw dict access on SDK responses
# ============================================================

class TestP2_RawDictAccess:
    """Raw dict subscript on Pydantic models must not crash."""

    @pytest.mark.xfail(reason="Pydantic models are not subscriptable — normalization via _as_dict() at call sites is the fix")
    def test_session_subscript_crashes_on_pydantic(self):
        """Reproduces line 204: session['$id']"""
        session = FakeSession(**{"$id": "s1", "userId": "u1", "$createdAt": "2025-01-01"})
        # This is what the code does — it should work but currently crashes
        try:
            session["$id"]
        except TypeError:
            pytest.fail("session['$id'] crashes on Pydantic model — line 204 is broken on SDK 19.2.0")

    @pytest.mark.xfail(reason="Pydantic models are not subscriptable — normalization via _as_dict() at call sites is the fix")
    def test_existing_docs_subscript_crashes_on_pydantic(self):
        """Reproduces line 1155: existing_docs['total']"""
        existing_docs = FakeDocumentList(total=3, documents=[])
        try:
            existing_docs["total"]
        except TypeError:
            pytest.fail("existing_docs['total'] crashes on Pydantic model — line 1155 is broken on SDK 19.2.0")

    @pytest.mark.xfail(reason="Pydantic models are not subscriptable — normalization via _as_dict() at call sites is the fix")
    def test_user_subscript_crashes_on_pydantic(self):
        """Reproduces line 2115: user['$id']"""
        user = FakeUser(**{"$id": "u1", "email": "a@b.com", "name": "Test"})
        try:
            user["$id"]
        except TypeError:
            pytest.fail("user['$id'] crashes on Pydantic model — line 2115 is broken on SDK 19.2.0")


# ============================================================
# P-3: OperationResult.data stores un-normalized Pydantic models
# ============================================================

class TestP3_PropagationCrash:
    """OperationResult.data holding a Pydantic model breaks downstream dict access."""

    @pytest.mark.xfail(reason="Pydantic models are not subscriptable — normalization via _as_dict() at call sites is the fix")
    def test_upload_result_data_subscript(self):
        """Reproduces line 1817: file_id = upload_result.data['$id']
        where upload_result.data is a raw Pydantic model from storage.create_file()"""
        raw_sdk_response = FakeFile(**{"$id": "file1", "name": "test.txt", "sizeOriginal": 1024})

        # Simulate OperationResult.data = raw_sdk_response
        data = raw_sdk_response
        try:
            data["$id"]
        except TypeError:
            pytest.fail("data['$id'] crashes when OperationResult.data is a Pydantic model")

    @pytest.mark.xfail(reason="Pydantic models are not subscriptable — normalization via _as_dict() at call sites is the fix")
    def test_upload_result_data_get(self):
        """Reproduces line 1441: uploaded_file_id = upload_result.data.get('$id')"""
        raw_sdk_response = FakeFile(**{"$id": "file1", "name": "test.txt", "sizeOriginal": 1024})
        data = raw_sdk_response
        try:
            data.get("$id")
        except AttributeError:
            pytest.fail("data.get('$id') crashes when OperationResult.data is a Pydantic model")

    @pytest.mark.xfail(reason="Pydantic models are not subscriptable — normalization via _as_dict() at call sites is the fix")
    def test_upload_result_data_mutation(self):
        """Reproduces line 1837: upload_result.data['metadata'] = metadata_result.data"""
        raw_sdk_response = FakeFile(**{"$id": "file1", "name": "test.txt", "sizeOriginal": 1024})
        data = raw_sdk_response
        try:
            data["metadata"] = {"key": "value"}
        except TypeError:
            pytest.fail("data['metadata'] = ... crashes when OperationResult.data is a Pydantic model")

    def test_as_dict_normalizes_before_storage(self):
        """OperationResult(data=result) should store _as_dict(result), not raw result."""
        raw_sdk_response = FakeFile(**{"$id": "file1", "name": "test.txt", "sizeOriginal": 1024})
        normalized = _as_dict(raw_sdk_response)
        assert isinstance(normalized, dict)
        assert normalized["$id"] == "file1"
        assert normalized["sizeOriginal"] == 1024


# ============================================================
# P-4: Test mock shape fidelity
# ============================================================

class TestP4_MockShapeDivergence:
    """SimpleNamespace doesn't match either SDK version for $-prefixed keys."""

    def test_simple_namespace_is_not_subscriptable(self):
        """SimpleNamespace (used in test mocks) fails dict subscript like Pydantic."""
        ns = SimpleNamespace(**{"$id": "test"})
        try:
            _ = ns["$id"]
            pytest.fail("SimpleNamespace should not be subscriptable")
        except TypeError:
            pass  # Expected — proves SimpleNamespace != dict

    def test_simple_namespace_getattr_dollar_id_works(self):
        """SimpleNamespace supports getattr($id) unlike Pydantic aliases."""
        ns = SimpleNamespace(**{"$id": "test"})
        assert getattr(ns, "$id") == "test"

    def test_pydantic_getattr_dollar_id_fails(self):
        """Pydantic does NOT support getattr($id) — alias is not a Python attribute."""
        doc = FakeDocument(**{"$id": "test"})
        result = getattr(doc, "$id", "MISSING")
        # This will be "MISSING" — proving SimpleNamespace and Pydantic behave differently
        assert result == "MISSING", "If this passes, Pydantic aliases aren't real Python attributes"
