"""Endpoint smoke tests via FastAPI TestClient (C-06)."""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tests.conftest import make_fao_df
from views_crafdapi.managers.api import CrafdApiManager
from views_crafdapi.managers.appwrite import OperationResult
from views_crafdapi.data.handlers import ForecastDataset

pytestmark = pytest.mark.layer3_http


@pytest.fixture
def app_client(tmp_path):
    """CrafdApiManager with FastAPI app, routes, and mocked dependencies."""
    mgr = CrafdApiManager.from_config(
        {"deployment": {"host": "0.0.0.0", "port": 80}},
        cache_dir=tmp_path / "cache",
    )
    mgr._prediction_bucket_id = "test-bucket"
    mgr.app = FastAPI()
    mgr._register_routes()

    # Pre-populate cache with a valid dataset. Conforming series targets so the forecast
    # endpoint's consumer rename (schema.series_of) resolves sb/ns/os (#222/S4).
    df = make_fao_df(targets=("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os"))
    dataset = ForecastDataset(df)
    api_key_hash = mgr._get_api_key_hash("test-api-key")
    mgr._dataframe_cache[api_key_hash] = {
        "historical": {
            "data": dataset.dataframe,
            "file_id": "file_001",
            "timestamp": time.time(),
            "dataset": dataset,
        },
        # S1 (#264): a forecast is served from cache only as a WIRE entry whose identity matches the
        # current manifest — so the warm entry is tagged source_kind="wire" and mock_pm below returns
        # a manifest with the matching fileId. (A bare/legacy forecast entry would now fail visible.)
        "forecast": {
            "data": dataset.dataframe,
            "file_id": "file_002",
            "timestamp": time.time(),
            "dataset": dataset,
            "source_kind": "wire",
        },
    }

    # Mock _validate_api_key to be a no-op returning a mock manager
    mock_appwrite = MagicMock()
    mock_appwrite.list_buckets.return_value = MagicMock(success=True)
    mock_appwrite.get_cache_stats.return_value = {"hits": 0, "misses": 0}
    mgr._validate_api_key = MagicMock(return_value=mock_appwrite)

    # Override dependencies so they don't hit real Appwrite
    mock_pm = MagicMock()
    # S1 (#264): the current manifest identifies the warm forecast entry (fileId "file_002"), so the
    # warm-cache identity check passes and the forecast serves without a wire re-ingest.
    mock_pm.get_latest_manifest.return_value = {
        "fileId": "file_002", "filename": "run__manifest.json",
        "type": "sampled_forecast_manifest", "category": "forecast", "name": "un_crafd",
    }
    mgr.app.dependency_overrides[mgr._get_prediction_manager] = lambda: mock_pm
    mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite

    client = TestClient(mgr.app)
    return client, mgr, mock_pm


HEADERS = {"X-API-Key": "test-api-key"}


# ============================================================
# Static endpoints
# ============================================================


class TestRootEndpoint:

    def test_root_returns_endpoint_catalog(self, app_client):
        client, _, _ = app_client
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert "endpoints" in body
        assert "message" in body


class TestHealthEndpoint:

    def test_health_degraded_when_forecast_stale(self, app_client):
        """S3 (#246, C-50): a served forecast older than the SLA degrades health (service up)."""
        client, _, mock_pm = app_client
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        mock_pm.get_latest_provenance.return_value = SimpleNamespace(created_at=old)
        resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 200  # service is up; only the *data* is stale
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["forecast_freshness"]["is_stale"] is True

    def test_health_healthy_when_forecast_fresh(self, app_client):
        client, _, mock_pm = app_client
        fresh = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        mock_pm.get_latest_provenance.return_value = SimpleNamespace(created_at=fresh)
        resp = client.get("/health", headers=HEADERS)
        assert resp.json()["status"] == "healthy"
        assert resp.json()["forecast_freshness"]["is_stale"] is False

    def test_health_freshness_uses_served_run_not_legacy_store_record(self, app_client):
        """#290 sibling: /health freshness must reflect the SERVED run, not the store's newest
        metadata record. A fresh served wire run (rusty_bucket) reads healthy even when the store's
        newest record is a stale superseded artifact (the March orange_ensemble)."""
        client, mgr, mock_pm = app_client
        stale = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()  # legacy March record
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()     # served rusty run
        mock_pm.get_latest_provenance.return_value = SimpleNamespace(created_at=stale)
        mgr._dataset_service._last_forecast_provenance = {
            "mode": "wire", "source": "rusty_bucket", "created_at": fresh,
        }
        mgr._dataset_service._forecast_serving_state = {"degraded": False}
        body = client.get("/health", headers=HEADERS).json()
        assert body["forecast_freshness"]["is_stale"] is False  # served run, not the March record
        assert body["status"] == "healthy"

    def test_health_returns_200(self, app_client):
        client, _, _ = app_client
        resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body

    def test_health_degraded_when_serving_grace_fallback(self, app_client):
        """S4 (#249, ADR-033 §6): an active bounded grace fallback (newest run refused, last-good
        served) degrades health and surfaces the fallback state — even when the served forecast is
        itself fresh (200; the service is up, the newest run is not)."""
        client, mgr, mock_pm = app_client
        fresh = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        mock_pm.get_latest_provenance.return_value = SimpleNamespace(created_at=fresh)
        mgr._dataset_service._forecast_serving_state = {
            "degraded": True, "reason": "ingest_failed", "fallback_available": True,
            "serving": "last_good_manifested", "file_id": "mani_good", "age_days": 5.0,
            "sla_days": 45.0,
        }
        resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["forecast_serving_state"]["degraded"] is True
        assert body["forecast_serving_state"]["fallback_available"] is True


class TestCacheStatsEndpoint:

    def test_cache_stats_returns_200(self, app_client):
        client, _, _ = app_client
        resp = client.get("/cache/stats", headers=HEADERS)
        assert resp.status_code == 200


# ============================================================
# Data endpoints
# ============================================================


class TestLatestDataEndpointsAreRetired:
    """`/data/{category}/latest` was retired 2026-08-24 (register C-232).

    It answered 200 with rows carrying no values, and these two tests were the only ones that
    touched it — named `*_returns_200`, asserting exactly that and nothing about the payload.
    They are the reason a Tier 1 defect sat open from 2026-08-10 behind a green suite."""

    @pytest.mark.parametrize("category", ["historical", "forecast"])
    def test_latest_is_gone(self, app_client, category):
        client, _, _ = app_client
        assert client.get(f"/data/{category}/latest", headers=HEADERS).status_code == 404

    def test_missing_api_key_returns_422(self, app_client):
        """Re-pointed off `/latest` at a surviving keyed route — the behaviour under test is
        FastAPI's missing-header handling, which was never specific to that endpoint."""
        client, _, _ = app_client
        resp = client.get("/pg/data/historical/subset")
        assert resp.status_code == 422


# ============================================================
# Subset endpoints
# ============================================================


class TestSubsetEndpoints:

    def test_pg_subset_returns_200(self, app_client):
        client, _, _ = app_client
        resp = client.get("/pg/data/historical/subset", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True

    def test_country_subset_returns_200(self, app_client):
        client, _, _ = app_client
        resp = client.get("/country/data/historical/subset", headers=HEADERS)
        assert resp.status_code == 200


# ============================================================
# HDI-MAP endpoints
# ============================================================


class TestHdiMapEndpoints:

    def test_pg_hdi_map_returns_200(self, app_client):
        client, _, _ = app_client
        resp = client.get("/pg/analysis/historical/hdi-map", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "hdi_map" in body["data"]


# ============================================================
# Parametrized subset endpoint matrix (C-06 Sprint 5)
# ============================================================

_LEVELS = ["pg", "country", "gaul0", "gaul1", "gaul2"]
_CATEGORIES = ["historical", "forecast"]

_LEVEL_CATEGORY_PARAMS = [
    pytest.param(level, cat, id=f"{level}-{cat}")
    for level in _LEVELS
    for cat in _CATEGORIES
]


class TestSubsetEndpointsMatrix:

    @pytest.mark.parametrize("level,category", _LEVEL_CATEGORY_PARAMS)
    def test_subset_returns_200(self, app_client, level, category):
        client, _, _ = app_client
        resp = client.get(f"/{level}/data/{category}/subset", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["level"] == level
        assert body["data"]["category"] == category

    def test_subset_entity_ids_gaul0(self, app_client):
        client, _, _ = app_client
        resp = client.get(
            "/gaul0/data/historical/subset",
            params={"entity_ids": "10"},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["parameters"]["entity_ids"] == [10]

    def test_subset_entity_ids_gaul2(self, app_client):
        client, _, _ = app_client
        resp = client.get(
            "/gaul2/data/historical/subset",
            params={"entity_ids": "100"},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        shape = resp.json()["data"]["shape"]
        assert shape[0] == 2  # 1 cell × 2 months

    def test_subset_aggregate_gaul1(self, app_client):
        client, _, _ = app_client
        resp = client.get(
            "/gaul1/data/historical/subset",
            params={"aggregate": "true"},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        shape = resp.json()["data"]["shape"]
        assert shape[0] == 4  # 2 gaul1 regions × 2 months


# ============================================================
# Parametrized HDI-MAP endpoint matrix (C-06 Sprint 5)
# ============================================================

class TestHdiMapEndpointsMatrix:

    @pytest.mark.parametrize("level,category", _LEVEL_CATEGORY_PARAMS)
    def test_hdi_map_returns_200(self, app_client, level, category):
        client, _, _ = app_client
        resp = client.get(f"/{level}/analysis/{category}/hdi-map", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "hdi_map" in body["data"]

    def test_hdi_map_aggregate_gaul0(self, app_client):
        client, _, _ = app_client
        resp = client.get(
            "/gaul0/analysis/historical/hdi-map",
            params={"aggregate": "true"},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        columns = body["data"]["columns"]
        # #222/S4: raw min/max dropped; the reduction now serves MAP + 50/90/95 HDIs + severe.
        assert any("_severe_scenario" in col for col in columns)
        assert any("_hdi95_lower" in col for col in columns)
        assert not any(c.endswith(("_min", "_max")) for c in columns)

    def test_hdi_map_entity_ids_gaul2_forecast(self, app_client):
        client, _, _ = app_client
        resp = client.get(
            "/gaul2/analysis/forecast/hdi-map",
            params={"entity_ids": "200"},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True


# ============================================================
# OperationResult.data consumer site tests (C-06 Sprint 5)
# ============================================================


class TestOperationResultDataConsumers:

    def test_missing_file_bytes_on_download_returns_500(self, app_client):
        """Site 1 (api.py:627): download_result.data missing 'file_bytes' key."""
        client, mgr, mock_pm = app_client
        api_key_hash = mgr._get_api_key_hash("test-api-key")
        mgr._dataframe_cache.pop(api_key_hash, None)
        mock_pm.download_prediction.return_value = OperationResult(
            success=True, data={}
        )
        mock_pm.get_latest_file_id.return_value = OperationResult(
            success=True, data={"file_id": "file_001"}
        )
        # Re-pointed off the retired `/latest` onto a subset route: the behaviour under test is
        # the INGEST error path (`download_prediction` returning data with no `file_bytes`), which
        # every route reaches via `get_latest_dataset` → `get_latest_dataframe`.
        resp = client.get(
            "/pg/data/historical/subset",
            params={"force_refresh": "true"},
            headers=HEADERS,
        )
        assert resp.status_code == 500

    def test_missing_file_bytes_on_file_download_serves_empty(self, app_client):
        """Site 2 (api.py:1158): result.data missing 'file_bytes' silently serves empty response.
        io.BytesIO(None) returns empty bytes on Python 3.10+ — no error raised."""
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.get_file.return_value = OperationResult(
            success=True, data={"name": "test.txt"}
        )
        mock_appwrite.download_file.return_value = OperationResult(
            success=True, data={}
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket/file_001/download", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.content == b""

    def test_missing_cache_path_returns_500(self, app_client):
        """Site 3 (api.py:1201): result.data missing 'cache_path' key."""
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.cache_manager.get_cached_file_path.return_value = OperationResult(
            success=True, data={}
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket/file_001/cached", headers=HEADERS)
        assert resp.status_code == 500


# ============================================================
# File + cache endpoint smoke tests (C-06 Sprint 5)
# ============================================================


class TestFileEndpoints:

    def test_list_files_returns_200(self, app_client):
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.list_files.return_value = OperationResult(
            success=True, data={"files": [], "total": 0}
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_file_info_returns_200(self, app_client):
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.get_file.return_value = OperationResult(
            success=True, data={"name": "test.parquet", "$id": "file_001"}
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket/file_001/info", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["file_id"] == "file_001"

    def test_cache_delete_returns_200(self, app_client):
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.clear_cache.return_value = OperationResult(
            success=True, data={"cleared": 0}
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.delete("/cache", headers=HEADERS)
        assert resp.status_code == 200


# ============================================================
# File endpoint error paths — P-6 .get() bug regression (Sprint 7)
# ============================================================


class TestFileEndpointErrorPaths:

    def test_list_files_failure_returns_404(self, app_client):
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.list_files.return_value = OperationResult(
            success=False, error="Bucket not found", code="NOT_FOUND"
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket", headers=HEADERS)
        assert resp.status_code == 404
        assert "Bucket not found" in resp.json()["detail"]

    def test_get_file_info_failure_returns_404(self, app_client):
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.get_file.return_value = OperationResult(
            success=False, error="File not found", code="NOT_FOUND"
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket/file_001/info", headers=HEADERS)
        assert resp.status_code == 404
        assert "File not found" in resp.json()["detail"]

    def test_download_file_not_found_returns_404(self, app_client):
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.get_file.return_value = OperationResult(
            success=False, error="File not found", code="NOT_FOUND"
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket/file_001/download", headers=HEADERS)
        assert resp.status_code == 404
        assert "File not found" in resp.json()["detail"]

    def test_download_file_download_failure_returns_500(self, app_client):
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.get_file.return_value = OperationResult(
            success=True, data={"name": "test.bin"}
        )
        mock_appwrite.download_file.return_value = OperationResult(
            success=False, error="Storage unavailable", code="STORAGE_ERROR"
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket/file_001/download", headers=HEADERS)
        assert resp.status_code == 500
        assert "Storage unavailable" in resp.json()["detail"]

    def test_cached_file_failure_returns_404(self, app_client):
        client, mgr, _ = app_client
        mock_appwrite = MagicMock()
        mock_appwrite.cache_manager.get_cached_file_path.return_value = OperationResult(
            success=False, error="Not in cache", code="CACHE_MISS"
        )
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/files/test-bucket/file_001/cached", headers=HEADERS)
        assert resp.status_code == 404
        assert "Not in cache" in resp.json()["detail"]


class TestHTTPExceptionPassthrough:

    @pytest.mark.parametrize("method,path", [
        pytest.param("GET", "/cache/stats", id="cache-stats"),
        pytest.param("DELETE", "/cache", id="clear-cache"),
        pytest.param("GET", "/health", id="health"),
    ])
    def test_auth_failure_returns_401(self, app_client, method, path):
        client, mgr, _ = app_client

        def reject_key():
            raise HTTPException(status_code=401, detail="Invalid key")

        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = reject_key
        resp = client.request(method, path, headers=HEADERS)
        assert resp.status_code == 401


class TestProvenanceEndpoint:
    """C-86: /provenance/{category} exposes the lineage record of the served artifact."""

    def test_provenance_returns_record(self, app_client):
        from views_crafdapi.managers.prediction import PredictionProvenance

        client, _, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="file_002",
            source="views-datafactory",
            created_at="2025-10-22T12:00:00.000Z",
            filename="forecast.parquet",
            file_hash="deadbeef",
        )
        resp = client.get("/provenance/forecast", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["source"] == "views-datafactory"
        assert body["data"]["file_id"] == "file_002"
        assert body["data"]["file_hash"] == "deadbeef"
        mock_pm.get_latest_provenance.assert_called_once()

    def test_provenance_invalid_category_422(self, app_client):
        client, _, _ = app_client
        resp = client.get("/provenance/bogus", headers=HEADERS)
        assert resp.status_code == 422

    def test_provenance_not_found_404(self, app_client):
        client, _, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = None
        resp = client.get("/provenance/historical", headers=HEADERS)
        assert resp.status_code == 404

    def test_provenance_reports_a_manifested_run_with_no_legacy_record(self, app_client):
        """#60 regression, reproduced from production 2026-08-14.

        `crafd_bucket` is greenfield on the wire contract, so a type-less category query — which
        ADR-013 §11.4 correctly pins to `type="model"` — matches nothing. The route used to stop
        there and 404 while the API was serving that very run: `/health` reported a forecast
        created 18:35:54 and smoke.py returned 1,030 IDN cells, but `/provenance/forecast`
        answered "No forecast prediction files found in the bucket: crafd_bucket".

        Nothing here is served by this worker, so the served record is absent too — the manifest
        is the ONLY source that can answer, which is exactly the production shape.

        The manifest lineage is built by the REAL `_provenance_from` from a real manifest store
        document, not hand-stubbed — otherwise the method the fix adds never executes and the
        test cannot fail for the reason it claims.
        """
        from views_crafdapi.managers.prediction.manager import PredictionStoreManager

        # A manifest document exactly as views-postprocessing's wire sink uploads it:
        # PredictionMetadata.to_dict() emits {loa,name,type,targets,category,description} —
        # note there is NO `source`/`pipeline` field, which is why `source` reads "unknown".
        manifest_doc = {
            "fileId": "6a7f5fda000d0f4e1c22",
            "filename": "rusty_bucket_forecasting_20260727_095355__manifest.json",
            "$createdAt": "2026-08-14T18:35:54.962+00:00",
            "name": "un_crafd",
            "type": "sampled_forecast_manifest",
            "category": "forecast",
            "loa": "pgm",
            "targets": ["lr_ged_sb", "lr_ged_ns", "lr_ged_os"],
        }

        client, mgr, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = None          # no legacy doc: greenfield
        mock_pm.get_latest_manifest_provenance.side_effect = (
            lambda: PredictionStoreManager._provenance_from(manifest_doc)
        )
        mgr._dataset_service._last_forecast_provenance = None      # cold worker

        resp = client.get("/provenance/forecast", headers=HEADERS)
        assert resp.status_code == 200, "a served run must never be reported as absent"
        data = resp.json()["data"]
        assert data["file_id"] == "6a7f5fda000d0f4e1c22"
        assert data["name"] == "un_crafd"
        assert data["targets"] == ["lr_ged_sb", "lr_ged_ns", "lr_ged_os"]
        assert "freshness" in data
        # Honest about what a manifest alone cannot say. The ensemble identity lives in the
        # shard header, not the manifest's store metadata; and nothing here has been served, so
        # this describes the newest manifested run rather than a verified-servable artifact.
        assert data["source"] == "unknown"  # no producer stamps source on a manifest
        assert data["mode"] is None

    def test_provenance_prefers_the_manifest_over_a_legacy_record(self, app_client):
        """The route must fetch BOTH store sources and let the module rank them.

        Gating the manifest lookup on `stored is None` inverts the documented precedence: a
        legacy record beside a newer manifested run would win, and /provenance would name an
        artifact this build refuses to serve (S1/#264) while /health reports the manifest.
        """
        from views_crafdapi.managers.prediction import PredictionProvenance

        client, mgr, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="legacy_001", source="orange_ensemble", name="orange_ensemble",
            created_at="2026-03-01T00:00:00.000Z", filename="old.parquet", file_hash="cafe",
        )
        mock_pm.get_latest_manifest_provenance.return_value = PredictionProvenance(
            file_id="manifest_001", source="unknown", name="un_crafd",
            created_at="2026-08-13T00:00:00.000Z", filename="run__manifest.json",
        )
        mgr._dataset_service._last_forecast_provenance = None

        data = client.get("/provenance/forecast", headers=HEADERS).json()["data"]
        assert data["file_id"] == "manifest_001"
        assert data["source"] != "orange_ensemble"
        assert mock_pm.get_latest_manifest_provenance.called, "the manifest must always be consulted"

    def test_provenance_degrades_to_legacy_when_the_manifest_lookup_raises(self, app_client):
        """An Appwrite blip must not 500 the endpoint an operator uses to diagnose it.

        `/health` and `_load_wire_run` both wrap this same call defensively; so does this.
        """
        from views_crafdapi.managers.prediction import PredictionProvenance

        client, mgr, mock_pm = app_client
        mock_pm.get_latest_manifest_provenance.side_effect = RuntimeError("appwrite is down")
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="legacy_001", source="orange_ensemble",
            created_at="2026-03-01T00:00:00.000Z", filename="old.parquet",
        )
        mgr._dataset_service._last_forecast_provenance = None

        resp = client.get("/provenance/forecast", headers=HEADERS)
        assert resp.status_code == 200, "must degrade to the legacy record, not 500"
        assert resp.json()["data"]["file_id"] == "legacy_001"

    def test_provenance_404s_when_the_store_answers_with_nothing_usable(self, app_client):
        """An unstubbed MagicMock's `to_dict()` collapses to `{}` — a truthy object carrying no
        record. The 404 must be decided by production semantics, not by mock configuration."""
        client, mgr, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = None
        mgr._dataset_service._last_forecast_provenance = None
        # deliberately NOT stubbing get_latest_manifest_provenance

        assert client.get("/provenance/forecast", headers=HEADERS).status_code == 404

    def test_provenance_404s_only_when_there_is_genuinely_no_forecast(self, app_client):
        """The 404 must survive the fix — an empty bucket is still an empty bucket."""
        client, mgr, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = None
        mock_pm.get_latest_manifest_provenance.return_value = None
        mgr._dataset_service._last_forecast_provenance = None

        assert client.get("/provenance/forecast", headers=HEADERS).status_code == 404

    def test_provenance_exposes_full_served_decision(self, app_client):
        """S7 (#252, ADR-033 observability): /provenance/forecast reports the full *served* decision
        — artifact_id, mode, status, freshness, and refusal_reason when degraded — sourced from the
        served run (authoritative), not merely the store's newest record."""
        from views_crafdapi.managers.prediction import PredictionProvenance

        client, mgr, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="store_newest", source="datafactory",
            created_at="2026-01-01T00:00:00.000Z", filename="f.parquet", file_hash="d",
        )
        fresh = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        mgr._dataset_service._last_forecast_provenance = {
            "file_id": "wire_run_9", "mode": "wire", "status": "candidate",
            "source": "rusty_bucket", "created_at": fresh,
        }
        mgr._dataset_service._forecast_serving_state = {
            "degraded": True, "reason": "schema_capability_mismatch", "fallback_available": True,
        }
        data = client.get("/provenance/forecast", headers=HEADERS).json()["data"]
        assert data["artifact_id"] == "wire_run_9"     # served run, not "store_newest"
        assert data["mode"] == "wire"
        assert data["status"] == "candidate"
        assert data["source"] == "rusty_bucket"
        assert data["freshness"]["is_stale"] is False  # from the served run's created_at
        assert data["refusal_reason"] == "schema_capability_mismatch"

    def test_provenance_overlays_served_identity_labels_not_legacy(self, app_client):
        """#290: when a wire run is served, name/filename/created_at must reflect THAT run — not
        the store's newest legacy record. Otherwise stale labels (e.g. "orange_ensemble") bleed
        through a live rusty_bucket serve and read as still-serving-the-placeholder."""
        from views_crafdapi.managers.prediction import PredictionProvenance

        client, mgr, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="store_newest", source="orange_ensemble", name="orange_ensemble",
            created_at="2026-03-10T10:52:03.762Z",
            filename="forecast_dataset_20260310_114703.parquet", file_hash="d",
        )
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        mgr._dataset_service._last_forecast_provenance = {
            "file_id": "wire_run_9", "run_id": "rusty_bucket_forecasting_20260727_095355",
            "mode": "wire", "source": "rusty_bucket", "name": "rusty_bucket",
            "filename": "rusty_bucket_forecasting_20260727_095355__manifest.json",
            "created_at": fresh, "targets": ["lr_ged_sb", "lr_ged_ns", "lr_ged_os"],
        }
        mgr._dataset_service._forecast_serving_state = {"degraded": False}
        data = client.get("/provenance/forecast", headers=HEADERS).json()["data"]
        assert data["name"] == "rusty_bucket"                    # not "orange_ensemble"
        assert data["filename"].startswith("rusty_bucket_")      # not the legacy filename
        assert data["created_at"] == fresh                       # served run, not 2026-03-10
        assert data["run_id"] == "rusty_bucket_forecasting_20260727_095355"
        # #290 full reconcile: the descriptive fields describe the served wire run, not the
        # legacy test record.
        assert data["file_id"] == "wire_run_9"                   # served id, not the legacy file_id
        assert data["targets"] == ["lr_ged_sb", "lr_ged_ns", "lr_ged_os"]  # wire targets, not pred_ln_*
        assert data["file_hash"] is None                         # wire = many shards, no single hash
        assert "rusty_bucket_forecasting_20260727_095355" in data["description"]  # not "test DataFrame"

    def test_provenance_self_heals_wire_run_cached_before_290(self, app_client):
        """#290 hardening: a wire run cached by a PRE-fix build carries no name/filename in its
        served provenance (only source/run_id/mode). /provenance must still reconstruct them from
        source/run_id so the legacy label never survives — self-heals without a force_refresh."""
        from views_crafdapi.managers.prediction import PredictionProvenance

        client, mgr, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="store_newest", source="orange_ensemble", name="orange_ensemble",
            created_at="2026-03-10T10:52:03.762Z",
            filename="forecast_dataset_20260310_114703.parquet", file_hash="d",
        )
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        # Pre-#290 cached shape: source/run_id/mode/created_at, but NO name/filename.
        mgr._dataset_service._last_forecast_provenance = {
            "file_id": "wire_run_9", "run_id": "rusty_bucket_forecasting_20260727_095355",
            "mode": "wire", "source": "rusty_bucket", "created_at": fresh,
        }
        mgr._dataset_service._forecast_serving_state = {"degraded": False}
        data = client.get("/provenance/forecast", headers=HEADERS).json()["data"]
        assert data["name"] == "rusty_bucket"                    # reconstructed from source
        assert data["filename"] == "rusty_bucket_forecasting_20260727_095355"  # from run_id
        assert data["created_at"] == fresh
        assert "orange_ensemble" not in (data["name"], data["filename"])
        # a pre-fix cache carries no targets — honest-absent (None), never the legacy pred_ln_*
        assert data["file_id"] == "wire_run_9"
        assert data["targets"] is None
        assert data["file_hash"] is None
        assert "rusty_bucket_forecasting_20260727_095355" in data["description"]

    def test_provenance_surfaces_grace_fallback_state(self, app_client):
        """S4 (#249, ADR-033 §6): when a bounded grace fallback is active, /provenance/forecast
        flags it under `serving_state` so a consumer sees the newest run was refused."""
        from views_crafdapi.managers.prediction import PredictionProvenance

        client, mgr, mock_pm = app_client
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="mani_good", source="fixture_ensemble",
            created_at="2026-07-23T12:00:00.000Z", filename="f.parquet", file_hash="d",
        )
        mgr._dataset_service._forecast_serving_state = {
            "degraded": True, "reason": "ingest_failed", "fallback_available": True,
            "file_id": "mani_good",
        }
        resp = client.get("/provenance/forecast", headers=HEADERS)
        assert resp.status_code == 200
        state = resp.json()["data"]["serving_state"]
        assert state["degraded"] is True and state["reason"] == "ingest_failed"


class TestHealthKeysTheFreshnessMonitorParses:
    """`.github/workflows/data-freshness.yml` polls `/health` daily and opens an issue when
    the served data goes stale — the question Better Stack's `/ping` monitor cannot answer.

    It parses these keys out of the body. Renaming one here without renaming it there leaves
    the monitor inspecting a field that no longer exists: it would report healthy forever
    while checking nothing. `forecast_freshness` is the dangerous one, because api.py builds
    it best-effort and swallows its own errors, so its absence is silent by design."""

    def test_health_emits_the_keys_the_monitor_reads(self, app_client):
        client, _, _ = app_client
        body = client.get("/health", headers=HEADERS).json()
        assert "status" in body
        assert "appwrite_connected" in body
        assert "forecast_freshness" in body
        assert "is_stale" in body["forecast_freshness"]
