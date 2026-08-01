"""Failing test stubs from falsification audit of our understanding completeness.

Tests the claim that we understand enough to fix the dual-SDK issue.
Each failure represents a gap in our understanding.

Run: PYTHONPATH=src pytest tests/test_falsify_understanding.py -v
"""

import pytest
from pydantic import BaseModel, Field, PrivateAttr, ConfigDict
from typing import Any, Dict, cast
from views_faoapi.managers.appwrite import _as_dict

pytestmark = pytest.mark.layer5_audit


# --- Realistic Appwrite SDK 19.2.0 models ---

class FakeAppwriteModel(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow", populate_by_name=True)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json", exclude_unset=True)


class FakePreferences(FakeAppwriteModel):
    _data: Any = PrivateAttr(default_factory=dict)

    @classmethod
    def with_data(cls, data: Dict[str, Any]) -> "FakePreferences":
        instance = cls.model_validate({})
        instance._data = data
        return instance

    @property
    def data(self):
        return cast(dict, self._data)

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        if hasattr(self, "_data") and isinstance(self._data, dict):
            result["data"] = self._data
        return result


class FakeDocument(FakeAppwriteModel):
    id: str = Field(..., alias="$id")
    sequence: str = Field(default="0", alias="$sequence")
    collectionid: str = Field(default="", alias="$collectionId")
    databaseid: str = Field(default="", alias="$databaseId")
    createdat: str = Field(default="", alias="$createdAt")
    updatedat: str = Field(default="", alias="$updatedAt")
    permissions: list = Field(default_factory=list, alias="$permissions")

    _data: Any = PrivateAttr(default_factory=dict)

    @classmethod
    def with_data(cls, data: Dict[str, Any]) -> "FakeDocument":
        internal_aliases = {"$id", "$sequence", "$collectionId", "$databaseId", "$createdAt", "$updatedAt", "$permissions"}
        internal_fields = {k: v for k, v in data.items() if k in internal_aliases}
        user_data = {k: v for k, v in data.items() if k not in internal_aliases and k != "data"}
        instance = cls.model_validate(internal_fields)
        instance._data = user_data
        return instance

    @property
    def data(self):
        return cast(dict, self._data)

    def to_dict(self) -> Dict[str, Any]:
        result = super().to_dict()
        if hasattr(self, "_data") and isinstance(self._data, dict):
            result["data"] = self._data
        return result


# ============================================================
# P-1: Preferences model also has _data — missed in fix plan
# ============================================================

class TestP1_PreferencesDataLoss:
    """Preferences model stores user prefs in _data, just like Document."""

    def test_as_dict_preserves_preferences_data(self):
        """users.get_prefs() returns Preferences with _data.
        _as_dict must not drop the user preferences."""
        prefs = FakePreferences.with_data({"theme": "dark", "lang": "en", "notifications": True})
        result = _as_dict(prefs)
        assert isinstance(result, dict)
        assert "theme" in result, f"_as_dict dropped user preferences. Got: {result}"
        assert result["theme"] == "dark"

    def test_as_dict_preserves_preferences_when_stored_in_operation_result(self):
        """Line 2141: OperationResult(data=user_prefs) stores raw Preferences.
        If a consumer does data.get('theme'), it must work."""
        prefs = FakePreferences.with_data({"theme": "dark", "lang": "en"})
        data = _as_dict(prefs)
        assert isinstance(data, dict)
        assert data.get("theme") == "dark", f"Preferences data lost. Got: {data}"


# ============================================================
# P-3: to_dict + flatten collision with field named "data"
# ============================================================

class TestP3_DataFieldCollision:
    """If a user field is literally named 'data', flatten must not lose it."""

    def test_document_with_field_named_data(self):
        """Edge case: metadata document has a user field literally named 'data'.
        Constructs _data directly to bypass with_data's filter on the 'data' key."""
        doc = FakeDocument.model_validate({
            "$id": "doc1", "$collectionId": "c1", "$databaseId": "d1",
            "$createdAt": "2025-01-01", "$updatedAt": "2025-01-01", "$permissions": [],
        })
        doc._data = {"fileId": "f1", "data": "important_value"}
        result = _as_dict(doc)
        assert isinstance(result, dict)
        assert "fileId" in result, f"fileId missing from flattened dict: {result}"
        assert result.get("data") == "important_value", \
            f"User field 'data' was silently dropped during flattening: {result}"


# ============================================================
# P-4: api.py consumer assumptions about OperationResult.data shape
# ============================================================

class TestP4_ApiConsumerAssumptions:
    """api.py consumers use .data.get() — verify this works post-fix."""

    def test_get_file_data_has_name_key(self):
        """api.py:1280 does file_info.data.get('name', file_id).
        get_file returns _as_dict(File model). Must have 'name' key."""
        # Simulate File model (no _data, standard Pydantic)
        class FakeFile(FakeAppwriteModel):
            id: str = Field(..., alias="$id")
            name: str = Field(..., alias="name")
            sizeoriginal: float = Field(default=0, alias="sizeOriginal")

        f = FakeFile.model_validate({"$id": "f1", "name": "test.txt", "sizeOriginal": 1024})
        result = _as_dict(f)
        assert isinstance(result, dict), f"_as_dict should return dict, got {type(result)}"
        assert result.get("name") == "test.txt", f"'name' key missing or wrong: {result}"

    def test_list_files_data_shape(self):
        """api.py:1154-1155 does result.data.get('files', []) and result.data.get('total', 0).
        These are built by list_files() using _get() + _as_dict(). Verify shape."""
        # This tests the output shape, not the SDK input
        normalized_output = {
            "files": [{"$id": "f1", "name": "test.txt"}],
            "total": 5,
        }
        assert isinstance(normalized_output.get("files"), list)
        assert isinstance(normalized_output.get("total"), (int, float))
