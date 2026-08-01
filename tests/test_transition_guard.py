"""ADR-013 §11.4 — the Hop-B legacy transition guard.

Contract artifacts (`type="sampled_forecast_*"`) upload to `unfao_bucket` under the pinned
consumer name (`un_crafd`), which the legacy selector's `name` filter WILL match (ratified F1).
Without a `type` pin, legacy newest-wins-by-category selection would grab a manifest or arrow
shard as "the forecast". Every legacy `unfao_bucket` document carries `type="model"`
(ground-truthed against live Appwrite, 2026-07-15 — all 13 typed docs), so the guard pins the
legacy type on category selections that do not name a type of their own. Contract-aware
queries (manifest-first selection, ADR-013 §4.3) pass an explicit `type` and are untouched.

Mirrors the Hop-A guard shape: a pinned filter constant + golden-string tests.
"""

from unittest.mock import MagicMock, patch

import pytest

from views_crafdapi.managers.prediction import LEGACY_ARTIFACT_TYPE, PredictionStoreManager

pytestmark = pytest.mark.layer4_infra


def _make_store(captured: dict):
    """A PredictionStoreManager whose metadata search captures the filters it is given."""
    config = MagicMock()
    config.path_manager.model_name = "un_crafd"
    manager = MagicMock()

    def _capture(filters=None, **kwargs):
        captured["filters"] = filters
        result = MagicMock()
        result.to_dict.return_value = {"success": True, "data": {"documents": []}}
        return result

    manager.metadata_manager.search_files_by_metadata.side_effect = _capture
    with patch("views_crafdapi.managers.prediction.manager.AppWriteFileManager", return_value=manager):
        return PredictionStoreManager(config)


def test_legacy_artifact_type_golden_string():
    """The pin is a wire-contract constant (ADR-013 §11.4); changing it is a contract
    amendment, not a refactor."""
    assert LEGACY_ARTIFACT_TYPE == "model"


def test_category_selection_pins_legacy_type():
    captured = {}
    store = _make_store(captured)
    store.get_predictions_by_metadata(filters={"category": "forecast"})
    assert captured["filters"]["type"] == LEGACY_ARTIFACT_TYPE
    assert captured["filters"]["category"] == "forecast"


def test_explicit_type_is_never_overridden():
    """Contract-aware queries (e.g. manifest-first, type='sampled_forecast_manifest')
    must pass through untouched — the guard is for type-blind legacy selection only."""
    captured = {}
    store = _make_store(captured)
    store.get_predictions_by_metadata(
        filters={"category": "forecast", "type": "sampled_forecast_manifest"}
    )
    assert captured["filters"]["type"] == "sampled_forecast_manifest"


def test_non_category_queries_get_no_type_pin():
    """Exact-id lookups must not gain a type constraint (a contract shard fileId
    lookup would otherwise fail)."""
    captured = {}
    store = _make_store(captured)
    store.get_predictions_by_metadata(filters={"fileId": "abc123"})
    assert "type" not in captured["filters"]


def test_name_injection_still_applies_alongside_the_guard():
    """The guard extends, not replaces, the existing name-filter idiom."""
    captured = {}
    store = _make_store(captured)
    store.get_predictions_by_metadata(filters={"category": "historical"})
    assert captured["filters"]["name"] == "un_crafd"
    assert captured["filters"]["type"] == LEGACY_ARTIFACT_TYPE


def test_guard_excludes_contract_artifacts_from_newest_wins():
    """Characterization of the failure the guard prevents: with contract docs newest in
    the collection, a type-honoring search must select the legacy artifact, not the
    manifest. (The filter dict is the server-side contract; this simulates a backend
    that honors it.)"""
    config = MagicMock()
    config.path_manager.model_name = "un_crafd"
    manager = MagicMock()

    docs = [  # newest first — contract wave on top
        {"fileId": "m1", "category": "forecast", "type": "sampled_forecast_manifest", "name": "un_crafd"},
        {"fileId": "s1", "category": "forecast", "type": "sampled_forecast_shard", "name": "un_crafd"},
        {"fileId": "legacy1", "category": "forecast", "type": "model", "name": "un_crafd"},
    ]

    def _search(filters=None, **kwargs):
        kept = [
            d for d in docs
            if all(d.get(k) == v for k, v in (filters or {}).items())
        ]
        result = MagicMock()
        result.to_dict.return_value = {"success": True, "data": {"documents": kept}}
        return result

    manager.metadata_manager.search_files_by_metadata.side_effect = _search
    with patch("views_crafdapi.managers.prediction.manager.AppWriteFileManager", return_value=manager):
        store = PredictionStoreManager(config)

    assert store.get_latest_file_id(filters={"category": "forecast"}) == "legacy1"
