"""Full-stack HTTP integration tests: FastAPI + live Appwrite.

Bridges the gap between mock-based HTTP tests (test_api_endpoints.py) and
manager-layer integration tests (test_integration_appwrite_write.py).
Every test issues a real HTTP request through FastAPI's TestClient with
NO dependency overrides — the full chain runs: routing → dependency injection
→ _validate_api_key → live Appwrite → response serialization.

Requires APPWRITE_* environment variables to be set.
Skipped automatically when credentials are missing.

Run explicitly:
    PYTHONPATH=src pytest tests/test_integration_http.py -v -m integration
"""

import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

# All 9 vars required (not 6 like test_integration_appwrite_write.py) because
# the HTTP path calls _validate_appwrite_env() which demands all 8 + we need
# the API key for the X-API-Key header.
REQUIRED_ENV_VARS = [
    "APPWRITE_ENDPOINT",
    "APPWRITE_DATASTORE_PROJECT_ID",
    "APPWRITE_UNFAO_BUCKET_ID",
    "APPWRITE_UNFAO_BUCKET_NAME",
    "APPWRITE_UNFAO_COLLECTION_ID",
    "APPWRITE_UNFAO_COLLECTION_NAME",
    "APPWRITE_METADATA_DATABASE_ID",
    "APPWRITE_METADATA_DATABASE_NAME",
    "APPWRITE_DATASTORE_API_KEY",
]

_missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]

skip_no_creds = pytest.mark.skipif(
    len(_missing) > 0,
    reason=f"Missing Appwrite env vars: {', '.join(_missing)}",
)

pytestmark = [pytest.mark.integration, pytest.mark.layer3_http, skip_no_creds]


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="module")
def appwrite_config():
    from views_crafdapi.managers.appwrite import AppwriteConfig

    kwargs = dict(
        endpoint=os.getenv("APPWRITE_ENDPOINT"),
        project_id=os.getenv("APPWRITE_DATASTORE_PROJECT_ID"),
        credentials=os.getenv("APPWRITE_DATASTORE_API_KEY"),
        auth_method="api_key",
        cache_ttl_hours=24,
        bucket_id=os.getenv("APPWRITE_UNFAO_BUCKET_ID"),
        collection_id=os.getenv("APPWRITE_UNFAO_COLLECTION_ID"),
        database_id=os.getenv("APPWRITE_METADATA_DATABASE_ID"),
    )
    for env_key, kwarg_key in [
        ("APPWRITE_UNFAO_BUCKET_NAME", "bucket_name"),
        ("APPWRITE_UNFAO_COLLECTION_NAME", "collection_name"),
        ("APPWRITE_METADATA_DATABASE_NAME", "database_name"),
    ]:
        val = os.getenv(env_key)
        if val:
            kwargs[kwarg_key] = val

    return AppwriteConfig(**kwargs)


@pytest.fixture(scope="module")
def file_manager(appwrite_config):
    from views_crafdapi.managers.appwrite import AppWriteFileManager

    return AppWriteFileManager(appwrite_config)


@pytest.fixture(scope="module")
def prediction_manager(appwrite_config):
    from views_crafdapi.managers.prediction import PredictionStoreManager

    return PredictionStoreManager(appwrite_file_manager_config=appwrite_config)


@pytest.fixture(scope="module")
def bucket_id(appwrite_config):
    return appwrite_config.bucket_id


@pytest.fixture(scope="module")
def integration_client(tmp_path_factory):
    """TestClient backed by real Appwrite — full stack, no mocks."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from views_crafdapi.managers.api import CrafdApiManager

    api_key = os.environ["APPWRITE_DATASTORE_API_KEY"]
    cache_dir = tmp_path_factory.mktemp("http_integ_cache")

    mgr = CrafdApiManager.from_config(
        {"deployment": {"host": "0.0.0.0", "port": 80}},
        cache_dir=cache_dir,
    )
    mgr._prediction_bucket_id = os.environ["APPWRITE_UNFAO_BUCKET_ID"]
    mgr.app = FastAPI()
    mgr._register_routes()

    client = TestClient(mgr.app)
    return client, api_key


@pytest.fixture(scope="module")
def test_forecast_file(prediction_manager, file_manager, bucket_id):
    """Upload a test forecast parquet; clean up after all tests."""
    from tests.conftest import make_fao_df

    df = make_fao_df(n_cells=4, n_months=2, n_samples=10)
    tmp = Path(tempfile.mktemp(suffix=".parquet"))
    df.to_parquet(tmp)
    file_id = None
    try:
        result = prediction_manager.upload_predictions(
            file=tmp,
            filename=f"http_integ_{uuid4().hex[:8]}.parquet",
            loa="pgm",
            name="http_integration_probe",
            type="test",
            targets=["pred_test", "pred_other"],
            category="forecast",
        )
        assert result.success, f"Test file upload failed: {result.error}"
        assert result.data is not None, "Upload succeeded but returned no data"
        file_id = result.data.get("file_id")
        yield file_id
    finally:
        tmp.unlink(missing_ok=True)
        if file_id:
            try:
                file_manager.delete_file(bucket_id, file_id)
            except Exception:
                pass


# ============================================================
# Tier 1: Infrastructure through HTTP (no data dependency)
# ============================================================


class TestRootEndpointLive:

    def test_root_returns_catalog(self, integration_client):
        client, _ = integration_client
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert "endpoints" in body
        assert "message" in body


class TestHealthEndpointLive:

    def test_health_with_real_credentials(self, integration_client):
        """Full chain: HTTP → Header extraction → _validate_api_key →
        _create_appwrite_config → _validate_appwrite_env →
        AppWriteFileManager → list_buckets() live → JSON response."""
        client, api_key = integration_client
        resp = client.get("/health", headers={"X-API-Key": api_key})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"


class TestFileListingLive:

    def test_list_files_returns_structure(self, integration_client, bucket_id):
        client, api_key = integration_client
        resp = client.get(f"/files/{bucket_id}", headers={"X-API-Key": api_key})
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["files"], list)
        assert isinstance(body["total"], int)
        assert body["total"] >= 0


class TestAuthErrorLive:

    def test_invalid_key_returns_401(self, integration_client):
        client, _ = integration_client
        resp = client.get("/health", headers={"X-API-Key": "bogus-key-00000"})
        assert resp.status_code == 401


# ============================================================
# Tier 2: Data pipeline through HTTP (needs test data)
# ============================================================


class TestDataSubsetLive:

    def test_forecast_subset_through_http(self, integration_client, test_forecast_file):
        """Full pipeline: HTTP → auth → _get_latest_dataset → download from
        Appwrite → parquet parse → ForecastDataset → get_subset_dataframe →
        dataframe_to_dict → convert_numpy_types → JSON response."""
        client, api_key = integration_client
        resp = client.get(
            "/pg/data/forecast/subset",
            params={"force_refresh": "true"},
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["category"] == "forecast"
        assert body["data"]["level"] == "pg"
        assert len(body["data"]["dataframe"]) > 0


class TestHdiMapLive:

    def test_forecast_hdi_map_through_http(self, integration_client, test_forecast_file):
        """Statistical pipeline through HTTP: auth → download → parse →
        ForecastDataset → PosteriorDistributionAnalyzer → HDI/MAP → serialize."""
        client, api_key = integration_client
        resp = client.get(
            "/pg/analysis/forecast/hdi-map",
            params={"force_refresh": "true"},
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "hdi_map" in body["data"]
        columns = body["data"]["columns"]
        assert any("_map" in col for col in columns)


class TestAggregationLive:

    def test_country_aggregation_through_http(self, integration_client, test_forecast_file):
        """Aggregation pipeline through HTTP: auth → download → parse →
        ForecastDataset → _aggregate_distributions → _elementwise_sum →
        serialize."""
        client, api_key = integration_client
        resp = client.get(
            "/country/data/forecast/subset",
            params={"aggregate": "true"},
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["level"] == "country"
