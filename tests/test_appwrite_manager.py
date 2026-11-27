import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from appwrite.exception import AppwriteException

from views_crafdapi.managers.appwrite import (
    ApiKeyAuth,
    AppwriteConfig,
    AppWriteFileManager,
    AuthFactory,
    AuthMethod,
    CacheManager,
    CacheMetadata,
    CacheValidationResult,
    MetadataManager,
    OperationResult,
)

pytestmark = pytest.mark.layer4_infra


def _sdk_result(**kwargs):
    """Build a SimpleNamespace that mimics Appwrite SDK Pydantic models."""
    return SimpleNamespace(**kwargs)


def _mock_session_response(status_code=201, user_id="user123", session_id="sess123"):
    """Build a mock requests.Response for session creation."""
    resp = Mock()
    resp.status_code = status_code
    if status_code == 201:
        resp.json.return_value = {
            "$id": session_id,
            "userId": user_id,
            "$createdAt": "2026-01-01T00:00:00Z",
        }
        resp.cookies = {"a_session_test_project": "mock_cookie_value"}
    else:
        resp.json.return_value = {
            "message": "Invalid credentials. Please check the email and password.",
            "type": "user_invalid_credentials",
        }
        resp.cookies = {}
    return resp


# Fixtures
@pytest.fixture
def mock_path_manager():
    """Mock APIPathManager"""
    manager = Mock()
    manager.cache = Path("/tmp/test_cache")
    return manager


@pytest.fixture
def api_key_config(mock_path_manager):
    """Basic API key configuration"""
    return AppwriteConfig(
        endpoint="https://cloud.appwrite.io/v1",
        project_id="test_project",
        credentials="test_api_key",
        auth_method=AuthMethod.API_KEY,
        bucket_id="test_bucket",
        collection_id="test_collection",
        database_id="test_database",
        cache_dir="/tmp/test_cache",
        path_manager=mock_path_manager,
    )




@pytest.fixture
def mock_client():
    """Mock Appwrite Client"""
    with patch("views_crafdapi.managers.appwrite.manager.Client") as mock:
        client = mock.return_value
        client.set_endpoint.return_value = client
        client.set_project.return_value = client
        client.set_key.return_value = client
        client.set_cookie.return_value = client
        client._endpoint = "https://cloud.appwrite.io/v1"
        client._global_headers = {"x-appwrite-project": "test_project"}
        yield client


@pytest.fixture
def mock_storage():
    """Mock Storage service"""
    with patch("views_crafdapi.managers.appwrite.manager.Storage") as mock:
        yield mock.return_value


@pytest.fixture
def mock_tables():
    """Mock TablesDB service (Appwrite 1.8's Databases API; the metadata search reads through it)"""
    with patch("views_crafdapi.managers.appwrite.manager.TablesDB") as mock:
        yield mock.return_value


@pytest.fixture
def temp_cache_dir(tmp_path):
    """Create a temporary cache directory"""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return cache_dir


@pytest.fixture
def file_manager(api_key_config):
    """AppWriteFileManager with API key auth and all SDK services mocked."""
    with patch("views_crafdapi.managers.appwrite.manager.Client"), \
         patch("views_crafdapi.managers.appwrite.manager.Storage"):
        yield AppWriteFileManager(api_key_config)




# Test AppwriteConfig
class TestAppwriteConfig:
    def test_config_initialization_with_defaults(self, mock_path_manager):
        config = AppwriteConfig(
            endpoint="https://cloud.appwrite.io/v1",
            project_id="test_project",
            credentials="test_key",
            bucket_id="test_bucket",
            path_manager=mock_path_manager,
        )
        
        assert config.auth_method == AuthMethod.API_KEY
        assert config.bucket_name == "Test Bucket"
        assert config.database_name == "Test Bucket Metadata"
        assert config.collection_id is None and config.database_id is None
        assert config.cache_ttl_hours == 24

    def test_config_with_custom_values(self, mock_path_manager):
        config = AppwriteConfig(
            endpoint="https://cloud.appwrite.io/v1",
            project_id="test_project",
            credentials="test_key",
            bucket_id="custom_bucket",
            bucket_name="My Custom Bucket",
            database_name="My Database",
            collection_name="My Collection",
            cache_ttl_hours=48,
            path_manager=mock_path_manager,
        )
        
        assert config.bucket_name == "My Custom Bucket"
        assert config.database_name == "My Database"
        assert config.collection_name == "My Collection"
        assert config.cache_ttl_hours == 48

    def test_config_auth_method_string_conversion(self, mock_path_manager):
        config = AppwriteConfig(
            endpoint="https://cloud.appwrite.io/v1",
            project_id="test_project",
            credentials="test_key",
            auth_method="api_key",
            bucket_id="test_bucket",
            path_manager=mock_path_manager,
        )

        assert isinstance(config.auth_method, AuthMethod)
        assert config.auth_method == AuthMethod.API_KEY

    def test_config_timeout_default(self, mock_path_manager):
        config = AppwriteConfig(
            endpoint="https://cloud.appwrite.io/v1",
            project_id="test_project",
            credentials="test_key",
            bucket_id="test_bucket",
            path_manager=mock_path_manager,
        )
        assert config.timeout_seconds == 30

    def test_config_timeout_custom(self, mock_path_manager):
        config = AppwriteConfig(
            endpoint="https://cloud.appwrite.io/v1",
            project_id="test_project",
            credentials="test_key",
            bucket_id="test_bucket",
            path_manager=mock_path_manager,
            timeout_seconds=60,
        )
        assert config.timeout_seconds == 60


class TestTimeoutInjection:

    @pytest.fixture
    def timeout_manager(self, api_key_config):
        """AppWriteFileManager with real Client (for timeout tests) but mocked auth/services."""
        with patch("views_crafdapi.managers.appwrite.manager.Storage"), \
             patch("views_crafdapi.managers.appwrite.manager.AuthFactory") as MockAuthFactory:
            MockAuthFactory.create_auth.return_value.setup.return_value = OperationResult(
                success=True, data={}, code="OK"
            )
            yield AppWriteFileManager(api_key_config)

    def test_inject_timeout_wraps_client_call(self, timeout_manager):
        """After init, client.call should be the timeout wrapper, not the original."""
        assert timeout_manager.client.call.__name__ == "_timed_call"

    def test_timeout_value_matches_config(self, timeout_manager):
        """The wrapper injects the configured timeout tuple (connect=10, read=30)."""
        captured = {}

        import requests
        original = requests.request

        def spy(*args, **kwargs):
            captured.update(kwargs)
            raise ConnectionError("test abort")

        requests.request = spy
        try:
            timeout_manager.client.call("get", "/test")
        except Exception:
            pass
        finally:
            requests.request = original

        assert captured.get("timeout") == (10, 30)

    def test_module_level_requests_not_permanently_patched(self, timeout_manager):
        """After a call through the wrapper, module-level requests.request is restored."""
        import appwrite.client as _appwrite_client
        import requests

        original = requests.request
        try:
            timeout_manager.client.call("get", "/test")
        except Exception:
            pass

        assert _appwrite_client.requests.request is original


# Test Authentication
class TestAuthFactory:
    def test_create_api_key_auth(self):
        auth = AuthFactory.create_auth(AuthMethod.API_KEY)
        assert isinstance(auth, ApiKeyAuth)


    def test_unsupported_auth_method(self):
        with pytest.raises(ValueError):
            AuthFactory.create_auth("invalid_method")


class TestApiKeyAuth:
    def test_setup_success(self, mock_client):
        auth = ApiKeyAuth()
        result = auth.setup(mock_client, "test_api_key")
        
        assert result.success
        mock_client.set_key.assert_called_once_with("test_api_key")

    def test_setup_invalid_credentials(self, mock_client):
        auth = ApiKeyAuth()
        result = auth.setup(mock_client, {"invalid": "type"})
        
        assert not result.success
        assert result.code == "INVALID_CREDENTIALS"




# Test CacheManager
class TestCacheManager:
    def test_cache_manager_initialization(self, temp_cache_dir):
        cache_ttl = timedelta(hours=24)
        manager = CacheManager(temp_cache_dir, cache_ttl)
        
        assert manager.cache_dir == temp_cache_dir
        assert manager.cache_ttl == cache_ttl
        assert manager.cache_metadata == {}

    def test_add_to_cache(self, temp_cache_dir):
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        
        test_file = temp_cache_dir / "test.txt"
        test_file.write_text("test content")
        
        manager.add_to_cache(
            "bucket1",
            "file123",
            test_file,
            {"name": "test.txt", "$updatedAt": "2025-10-22T12:00:00.000Z"}
        )
        
        cache_key = "bucket1/file123"
        assert cache_key in manager.cache_metadata
        assert manager.cache_metadata[cache_key].filename == "test.txt"

    def test_validate_cache_valid(self, temp_cache_dir):
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        
        test_file = temp_cache_dir / "bucket1" / "file123"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test content")
        
        manager.add_to_cache("bucket1", "file123", test_file)
        
        result = manager.validate_cache("bucket1", "file123")
        assert result == CacheValidationResult.VALID

    def test_validate_cache_not_found(self, temp_cache_dir):
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        
        result = manager.validate_cache("bucket1", "nonexistent")
        assert result == CacheValidationResult.NOT_FOUND

    def test_validate_cache_invalid_ttl(self, temp_cache_dir):
        manager = CacheManager(temp_cache_dir, timedelta(hours=1))
        
        test_file = temp_cache_dir / "bucket1" / "file123"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test content")
        
        # Fix: Use CacheMetadata object instead of dict
        cache_key = "bucket1/file123"
        manager.cache_metadata[cache_key] = CacheMetadata(
            bucket_id="bucket1",
            file_id="file123",
            path=str(test_file),
            cached_at=(datetime.now() - timedelta(hours=2)).isoformat(),
            size_bytes=test_file.stat().st_size,
            filename="test.txt",
        )
        
        result = manager.validate_cache("bucket1", "file123")
        assert result == CacheValidationResult.INVALID_TTL


# Test MetadataManager
class TestMetadataManager:
    @pytest.fixture
    def metadata_manager(self, api_key_config, mock_tables):
        return MetadataManager(api_key_config, mock_tables)

    def test_search_files_by_metadata(self, metadata_manager, mock_tables):
        mock_tables.list_rows.return_value = _sdk_result(
            rows=[_sdk_result(fileId="file123", filename="test.txt")],
            total=1,
        )
        
        result = metadata_manager.search_files_by_metadata(
            filters={"filename": "test.txt"}
        )

        assert result.success
        assert result.data["total"] == 1
        assert len(result.data["documents"]) == 1

    def test_search_files_by_metadata_paginates_beyond_default_page(self, metadata_manager, mock_tables):
        """#287: a run with more artifacts than one Appwrite page (108 shards) must enumerate
        ALL matching documents, not just the default first page. Without pagination the resolver
        saw 25/108 shards → "83 missing" → the entire global wire run was refused (ingest_failed).
        list_rows is called with limit+offset until a short page ends the loop."""
        page1 = _sdk_result(
            rows=[_sdk_result(fileId=f"f{i}", filename=f"shard_{i}") for i in range(100)],
            total=108,
        )
        page2 = _sdk_result(
            rows=[_sdk_result(fileId=f"f{i}", filename=f"shard_{i}") for i in range(100, 108)],
            total=108,
        )
        mock_tables.list_rows.side_effect = [page1, page2]

        result = metadata_manager.search_files_by_metadata(
            filters={"type": "sampled_forecast_shard"}
        )

        assert result.success
        assert len(result.data["documents"]) == 108  # not truncated to a single 100-doc page
        assert mock_tables.list_rows.call_count == 2  # looped past the first full page


# Test AppWriteFileManager
class TestAppWriteFileManager:

    def test_download_file_from_cache(self, file_manager, temp_cache_dir):
        # Setup cache
        file_manager.cache_manager.validate_cache = Mock(
            return_value=CacheValidationResult.VALID
        )
        file_manager.cache_manager.get_cached_file_path = Mock(
            return_value=OperationResult(
                success=True,
                data={"cache_path": str(temp_cache_dir / "test.txt")}
            )
        )
        
        cached_file = temp_cache_dir / "test.txt"
        cached_file.write_text("cached content")
        
        result = file_manager.download_file("bucket1", "file123", use_cache=True)
        
        assert result.success
        assert result.data["from_cache"]

    def test_download_file_from_remote(self, file_manager):
        file_manager.storage.get_file_download.return_value = b"remote content"
        file_manager.get_file = Mock(
            return_value=OperationResult(
                success=True,
                data={"name": "test.txt", "$updatedAt": "2025-10-22T12:00:00Z"}
            )
        )
        
        result = file_manager.download_file("bucket1", "file123", use_cache=False)

        assert result.success
        assert result.data["file_bytes"] == b"remote content"
        assert not result.data["from_cache"]

    def test_download_file_json_returned_as_dict_is_normalized_to_bytes(self, file_manager):
        """#287 follow-up: the SDK auto-deserializes a JSON file (the run manifest) to a dict, not
        bytes. download_file must normalize it to bytes rather than crash on the cache write with
        'a bytes-like object is required, not dict' — this was the FIRST download in the wire-ingest
        path, so a cold-cache manifest fetch refused the entire global run."""
        import json as _json
        payload = {"contract_version": "1.5", "run_id": "r0", "shards": []}
        file_manager.storage.get_file_download.return_value = payload  # SDK returns a dict for JSON
        file_manager.get_file = Mock(
            return_value=OperationResult(
                success=True,
                data={"name": "manifest.json", "$updatedAt": "2026-07-27T17:40:59Z"},
            )
        )

        result = file_manager.download_file("bucket1", "manifest.json", use_cache=False)

        assert result.success  # no "bytes-like object required" crash
        fb = result.data["file_bytes"]
        assert isinstance(fb, (bytes, bytearray))
        assert _json.loads(fb) == payload  # round-trips back to the manifest

    def test_get_file(self, file_manager):
        file_manager.storage.get_file.return_value = _sdk_result(
            **{"$id": "file123", "name": "test.txt", "sizeOriginal": 100}
        )
        
        result = file_manager.get_file("bucket1", "file123")
        
        assert result.success
        assert result.data["$id"] == "file123"

    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_get_file_failure_carries_the_store_status(self, file_manager, code):
        # The file routes answer 401 from 401/403 and 404 from anything else (ADR-027 §4),
        # so the status must be the store's own, not a constant.
        file_manager.storage.get_file.side_effect = AppwriteException("refused", code, "t")
        result = file_manager.get_file("b", "f")
        assert (result.success, result.status) == (False, code)

    def test_get_file_unexpected_error_has_no_store_status(self, file_manager):
        file_manager.storage.get_file.side_effect = RuntimeError("boom")
        result = file_manager.get_file("b", "f")
        assert (result.success, result.status) == (False, None)

    def test_download_file_nonexistent_returns_failure(self, file_manager):
        file_manager.storage.get_file_download.side_effect = AppwriteException(
            "File not found", 404, "storage_file_not_found"
        )
        file_manager.get_file = Mock(
            return_value=OperationResult(success=False, error="Not found")
        )

        result = file_manager.download_file(
            "bucket1", "nonexistent", use_cache=False, validate_cache=False
        )

        assert not result.success
        assert result.code == "storage_file_not_found"


# Test Error Handling
class TestErrorHandling:
    def test_appwrite_exception_handling(self, api_key_config):
        with patch("views_crafdapi.managers.appwrite.manager.Client"), \
             patch("views_crafdapi.managers.appwrite.manager.Storage") as mock_storage:
            
            manager = AppWriteFileManager(api_key_config)
            mock_storage.return_value.get_file.side_effect = AppwriteException(
                "File not found", 404, "storage_file_not_found"
            )
            
            result = manager.get_file("bucket1", "nonexistent")
            
            assert not result.success
            assert result.code == "storage_file_not_found"

    def test_config_rejects_the_retired_session_auth_method(self, mock_path_manager):
        # Session auth was retired (þing-01 #274): the enum no longer carries "session", so a
        # config that asks for it is rejected outright — single-mode by construction.
        with pytest.raises(ValueError):
            AppwriteConfig(
                endpoint="https://cloud.appwrite.io/v1",
                project_id="test_project",
                credentials={"bad": "data"},
                auth_method="session",
                bucket_id="test_bucket",
                path_manager=mock_path_manager,
            )

    def test_download_file_io_error(self, file_manager, tmp_path):
        """A failure while writing the cache file itself answers IO_ERROR."""
        file_manager.storage.get_file_download.return_value = b"content"
        file_manager.get_file = Mock(
            return_value=OperationResult(
                success=True,
                data={"name": "test.txt", "$updatedAt": "2026-01-01T00:00:00Z",
                      "sizeOriginal": len(b"content")}
            )
        )
        file_manager.cache_manager._get_cache_path = Mock(return_value=tmp_path / "file123")
        with patch("views_crafdapi.managers.appwrite.manager.os.fsync",
                   side_effect=OSError("disk full")):
            result = file_manager.download_file("bucket1", "file123", use_cache=False)

        assert not result.success
        assert result.code == "IO_ERROR"
        assert "File operation failed" in result.error


# Test OperationResult
class TestOperationResult:
    def test_operation_result_success(self):
        result = OperationResult(success=True, data={"key": "value"})
        
        assert result.success
        assert result.data == {"key": "value"}
        assert result.error is None

    def test_operation_result_failure(self):
        result = OperationResult(
            success=False,
            error="Something went wrong",
            code="ERROR_CODE"
        )
        
        assert not result.success
        assert result.error == "Something went wrong"
        assert result.code == "ERROR_CODE"

    def test_operation_result_to_dict(self):
        result = OperationResult(
            success=True,
            data={"test": "data"},
            code="SUCCESS"
        )
        
        result_dict = result.to_dict()

        assert result_dict["success"] is True
        assert result_dict["data"] == {"test": "data"}
        assert result_dict["code"] == "SUCCESS"


class TestAppwriteEnvVarValidation:
    """Tests for env var validation (C-03 / ADR-013)."""

    _ALL_ENV = {
        "APPWRITE_ENDPOINT": "https://cloud.appwrite.io/v1",
        "APPWRITE_CRAFD_PROJECT_ID": "proj_123",
        "APPWRITE_CRAFD_BUCKET_ID": "bucket_123",
        "APPWRITE_CRAFD_BUCKET_NAME": "my_bucket",
        "APPWRITE_CRAFD_COLLECTION_ID": "coll_123",
        "APPWRITE_CRAFD_COLLECTION_NAME": "my_coll",
        "APPWRITE_METADATA_DATABASE_ID": "db_123",
        "APPWRITE_METADATA_DATABASE_NAME": "my_db",
    }

    def _getenv(self, overrides=None):
        merged = {**self._ALL_ENV, **(overrides or {})}
        return lambda key, default=None: merged.get(key, default)

    def test_missing_env_var_raises_valueerror(self):
        from views_crafdapi.managers.api import _validate_appwrite_env
        env = {**self._ALL_ENV, "APPWRITE_ENDPOINT": ""}
        with patch("os.getenv", side_effect=lambda key, default=None: env.get(key, default)):
            with pytest.raises(ValueError, match="APPWRITE_ENDPOINT"):
                _validate_appwrite_env()

    def test_missing_multiple_env_vars_lists_all(self):
        from views_crafdapi.managers.api import _validate_appwrite_env
        env = {**self._ALL_ENV, "APPWRITE_ENDPOINT": "", "APPWRITE_METADATA_DATABASE_ID": ""}
        with patch("os.getenv", side_effect=lambda key, default=None: env.get(key, default)):
            with pytest.raises(ValueError, match="APPWRITE_ENDPOINT") as exc_info:
                _validate_appwrite_env()
            assert "APPWRITE_METADATA_DATABASE_ID" in str(exc_info.value)

    def test_missing_name_vars_raises(self):
        from views_crafdapi.managers.api import _validate_appwrite_env
        env = {**self._ALL_ENV, "APPWRITE_CRAFD_BUCKET_NAME": ""}
        with patch("os.getenv", side_effect=lambda key, default=None: env.get(key, default)):
            with pytest.raises(ValueError, match="APPWRITE_CRAFD_BUCKET_NAME"):
                _validate_appwrite_env()

    def test_all_env_vars_present_returns_dict(self):
        from views_crafdapi.managers.api import _validate_appwrite_env
        with patch("os.getenv", side_effect=self._getenv()):
            env = _validate_appwrite_env()
        assert env["APPWRITE_ENDPOINT"] == "https://cloud.appwrite.io/v1"
        assert len(env) == 8

    @patch("views_crafdapi.managers.api.AppWriteFileManager")
    def test_create_appwrite_config_uses_all_eight_vars(self, mock_manager_cls):
        from views_crafdapi.managers.api import CrafdApiManager
        with patch.object(CrafdApiManager, "__init__", lambda self: None):
            mgr = CrafdApiManager()
        # The shared project's id is in the server env too (registry_to_env emits every row);
        # it must never be the project crafdapi talks to (þing 04 D3).
        shared = {"APPWRITE_DATASTORE_PROJECT_ID": "shared_project"}
        with patch("os.getenv", side_effect=self._getenv(shared)):
            config = mgr._create_appwrite_config("test_key")
        assert config.endpoint == "https://cloud.appwrite.io/v1"
        assert config.project_id == "proj_123"
        assert config.bucket_id == "bucket_123"
        assert config.bucket_name == "my_bucket"
        assert config.collection_name == "my_coll"
        assert config.database_name == "my_db"

    def test_init_validates_env_at_startup(self):
        """CrafdApiManager.__init__ calls _validate_appwrite_env after load_dotenv (ADR-013 Decision 3)."""
        import ast
        import inspect
        import textwrap
        from views_crafdapi.managers.api import CrafdApiManager

        init_source = textwrap.dedent(inspect.getsource(CrafdApiManager.__init__))
        tree = ast.parse(init_source)
        call_names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    call_names.append(node.func.id)
        assert "_validate_appwrite_env" in call_names, (
            "CrafdApiManager.__init__ does not call _validate_appwrite_env(). "
            "Env var validation is deferred to first request, violating ADR-013 Decision 3."
        )


# ============================================================
# ADR-018 Normalization Contract (C-27 Tier A)
# ============================================================


class TestNormalizationContract:
    """Every OperationResult.data on success must be a plain dict (ADR-018)."""

    def test_get_file_data_is_dict(self, file_manager):
        file_manager.storage.get_file.return_value = _sdk_result(
            **{"$id": "file123", "name": "test.txt", "sizeOriginal": 100}
        )
        result = file_manager.get_file("bucket1", "file123")
        assert result.success
        assert isinstance(result.data, dict)
        assert "$id" in result.data

    def test_download_file_data_is_dict(self, file_manager):
        file_manager.storage.get_file_download.return_value = b"content"
        file_manager.get_file = Mock(
            return_value=OperationResult(
                success=True,
                data={"name": "test.txt", "$updatedAt": "2026-01-01T00:00:00Z"}
            )
        )
        result = file_manager.download_file("bucket1", "file123", use_cache=False)
        assert result.success
        assert isinstance(result.data, dict)
        assert "file_bytes" in result.data


    def test_list_buckets_data_is_dict(self, file_manager):
        file_manager.storage.list_buckets.return_value = _sdk_result(
            buckets=[_sdk_result(**{"$id": "b1", "name": "Bucket 1"})], total=1
        )
        result = file_manager.list_buckets()
        assert result.success
        assert isinstance(result.data, dict)
        assert isinstance(result.data["buckets"], list)


# ============================================================
# Session Auth Branching (C-27 Tier A)
# ============================================================




class TestProvisioningGate:
    """þing-01 #276 / PLATFORM-001 D5: create_* provisioning is opt-in (default OFF) and raises,
    so a missing/wrong coordinate on a serving path never silently creates phantom storage."""


class TestCredentialRedaction:
    """þing-01 #277 / PLATFORM-001 redaction clause: no credential in any carrier reaches a log line."""

    def test_config_repr_omits_the_credential(self, api_key_config):
        # api_key_config.credentials == "test_api_key"; it must not appear in the dataclass repr.
        text = repr(api_key_config)
        assert "test_api_key" not in text, "the API key leaked into AppwriteConfig repr"

    def test_api_key_hash_is_a_digest_not_the_raw_key(self):
        raw = "super-secret-caller-key"
        # this is exactly how api.py keys the cache — a sha256 prefix, never the raw key
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        assert raw not in digest and len(digest) == 16


class TestAFailedCallKeepsItsHttpStatus:
    """C-233 (#158). The SDK raises `AppwriteException` for every failure — an HTTP error, a
    network error or a timeout. Keeping its HTTP status on the result is what lets a caller tell
    "not authorised" (401/403) from "unreachable" (0/None/5xx) instead of reading all of them as
    "nothing found"."""

    def test_a_failed_search_carries_the_status_and_type(self, mock_tables, api_key_config):
        mock_tables.list_rows.side_effect = AppwriteException(
            "Service unavailable", 503, "general_server_error"
        )
        result = MetadataManager(api_key_config, mock_tables).search_files_by_metadata(
            filters={"type": "sampled_forecast_manifest"}
        )
        assert result.success is False
        assert result.status == 503 and result.code == "general_server_error"
        assert result.to_dict()["status"] == 503

    def test_a_failed_bucket_listing_carries_the_status(self, file_manager):
        file_manager.storage.list_buckets.side_effect = AppwriteException(
            "The current user is not authorized", 401, "general_unauthorized_scope"
        )
        result = file_manager.list_buckets(limit=1)
        assert result.success is False and result.status == 401

    def test_a_success_has_no_status(self):
        assert OperationResult(success=True).status is None


    def test_a_failed_download_carries_the_status(self, file_manager):
        file_manager.storage.get_file_download.side_effect = AppwriteException(
            "The current user is not authorized", 401, "user_unauthorized"
        )
        file_manager.get_file = Mock(return_value=OperationResult(success=False, error="x"))
        result = file_manager.download_file("bucket1", "f1", use_cache=False, validate_cache=False)
        assert result.success is False and result.status == 401


# ── C-288 (#169): a cached download is written atomically and checked against its declared size ──
class TestCachedDownloadIsCompleteOrAbsent:
    BODY = b"PAR1" + b"x" * 96

    def _manager(self, file_manager, tmp_path, *, size_original, body=None):
        from views_crafdapi.managers.appwrite.results import OperationResult

        file_manager.cache_manager = CacheManager(tmp_path, timedelta(hours=24))
        file_manager.get_file = Mock(return_value=OperationResult(success=True, data={
            "name": "shard.parquet", "$updatedAt": "2020-01-01T00:00:00.000Z",
            "sizeOriginal": size_original,
        }))
        file_manager.storage.get_file_download = Mock(return_value=body or self.BODY)
        return file_manager

    def test_a_short_body_is_refused_and_nothing_is_cached(self, file_manager, tmp_path):
        mgr = self._manager(file_manager, tmp_path, size_original=len(self.BODY) + 10)
        out = mgr.download_file("b", "f")
        assert not out.success and "declares" in out.error
        assert out.code == "DOWNLOAD_INCOMPLETE"   # the code /download maps to 502
        assert mgr.cache_manager.validate_cache("b", "f") == CacheValidationResult.NOT_FOUND
        assert not (tmp_path / "b").exists() or list((tmp_path / "b").iterdir()) == []

    def test_a_body_longer_than_declared_is_refused_too(self, file_manager, tmp_path):
        mgr = self._manager(file_manager, tmp_path, size_original=len(self.BODY) - 1)
        out = mgr.download_file("b", "f")
        assert not out.success and out.code == "DOWNLOAD_INCOMPLETE"
        assert mgr.cache_manager.validate_cache("b", "f") == CacheValidationResult.NOT_FOUND

    def test_each_write_has_its_own_temp_file(self, file_manager, tmp_path, monkeypatch):
        """Two requests downloading the same file at once must not write into one temp file."""
        import os

        mgr = self._manager(file_manager, tmp_path, size_original=len(self.BODY))
        real_replace, sources = os.replace, []
        monkeypatch.setattr(os, "replace", lambda src, dst: (sources.append(Path(src).name),
                                                             real_replace(src, dst)))
        for _ in range(2):
            mgr.cache_manager.cache_metadata.clear()
            assert mgr.download_file("b", "f").success
        file_writes = [s for s in sources if s.startswith("f.tmp-")]  # not the index file
        assert len(file_writes) == 2 and len(set(file_writes)) == 2, sources

    def test_a_crash_mid_write_leaves_no_file_at_the_cache_path(self, file_manager, tmp_path, monkeypatch):
        import os

        mgr = self._manager(file_manager, tmp_path, size_original=len(self.BODY))
        monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("killed")))
        assert not mgr.download_file("b", "f").success
        assert list((tmp_path / "b").iterdir()) == []  # neither the target nor a stray temp

    def test_a_crash_mid_rewrite_keeps_the_previous_copy_whole(self, file_manager, tmp_path, monkeypatch):
        """Atomic means the old file survives a failed replacement intact; an in-place write
        would already have truncated it."""
        import os

        mgr = self._manager(file_manager, tmp_path, size_original=len(self.BODY))
        assert mgr.download_file("b", "f").success
        path = Path(mgr.cache_manager.get_cached_file_path("b", "f").data["cache_path"])
        mgr.storage.get_file_download = Mock(return_value=b"N" * len(self.BODY))
        mgr.cache_manager.cache_metadata.clear()  # force a re-download
        monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("killed")))
        assert not mgr.download_file("b", "f").success
        assert path.read_bytes() == self.BODY
        assert sorted(p.name for p in path.parent.iterdir()) == [path.name]

    def test_get_cached_file_path_refuses_a_wrong_size_copy(self, file_manager, tmp_path):
        """What `/cached` relies on: the path lookup itself names an incomplete copy."""
        mgr = self._manager(file_manager, tmp_path, size_original=len(self.BODY))
        assert mgr.download_file("b", "f").success
        path = Path(mgr.cache_manager.get_cached_file_path("b", "f").data["cache_path"])
        path.write_bytes(self.BODY[:10])
        out = mgr.cache_manager.get_cached_file_path("b", "f")
        assert not out.success and out.code == "CACHE_FILE_WRONG_SIZE"
        assert "is 10 bytes, a whole copy is 100" in out.error
        path.write_bytes(self.BODY + b"!")   # longer is not the file either
        assert mgr.cache_manager.get_cached_file_path("b", "f").code == "CACHE_FILE_WRONG_SIZE"
        assert mgr.cache_manager.validate_cache("b", "f") == CacheValidationResult.INVALID_SIZE

    def test_a_truncated_cached_file_is_invalid_and_refetched(self, file_manager, tmp_path):
        mgr = self._manager(file_manager, tmp_path, size_original=len(self.BODY))
        assert mgr.download_file("b", "f").success
        path = Path(mgr.cache_manager.get_cached_file_path("b", "f").data["cache_path"])
        path.write_bytes(self.BODY[:10])  # torn by an earlier, non-atomic writer
        assert mgr.cache_manager.validate_cache("b", "f") == CacheValidationResult.INVALID_SIZE
        out = mgr.download_file("b", "f")
        assert out.success and not out.data["from_cache"] and out.data["file_bytes"] == self.BODY
        assert path.read_bytes() == self.BODY

    def test_a_deserialized_manifest_is_checked_against_what_was_cached(self, file_manager, tmp_path):
        """The SDK hands a JSON file back as a dict; its re-serialised bytes need not match
        `sizeOriginal`, so the download is not refused on it — but the cached copy is still
        checked, against the size recorded when it was written."""
        mgr = self._manager(file_manager, tmp_path, size_original=9999, body={"run_id": "r"})
        out = mgr.download_file("b", "f")
        assert out.success and out.data["file_bytes"] == b'{"run_id": "r"}'
        assert mgr.cache_manager.validate_cache("b", "f") == CacheValidationResult.VALID
        path = Path(mgr.cache_manager.get_cached_file_path("b", "f").data["cache_path"])
        path.write_bytes(b'{"run_')
        assert mgr.cache_manager.validate_cache("b", "f") == CacheValidationResult.INVALID_SIZE

    def test_an_entry_is_checked_against_its_recorded_size(self, temp_cache_dir):
        """A whole cached copy is valid; one torn since (a write killed mid-way) is caught."""
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        f = temp_cache_dir / "bucket1" / "file1"
        f.parent.mkdir(parents=True)
        f.write_text("old contents")
        manager.add_to_cache("bucket1", "file1", f)
        assert manager.validate_cache("bucket1", "file1") == CacheValidationResult.VALID
        f.write_text("old")
        assert manager.validate_cache("bucket1", "file1") == CacheValidationResult.INVALID_SIZE

    def test_an_entry_cached_under_its_store_name_is_a_miss(self, temp_cache_dir):
        """#216: entries written before the cache was named by file id point to the store name.
        They are never read; the file is downloaded again and cached under its id."""
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        f = temp_cache_dir / "bucket1" / "old.txt"
        f.parent.mkdir(parents=True)
        f.write_text("old contents")
        manager.add_to_cache("bucket1", "file1", f)
        assert manager.validate_cache("bucket1", "file1") == CacheValidationResult.NOT_FOUND

    def test_a_file_removed_mid_check_is_a_miss_not_an_error(self, temp_cache_dir, monkeypatch):
        """A concurrent clear can unlink the file between lookups; that is NOT_FOUND / missing."""
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        f = temp_cache_dir / "bucket1" / "x.txt"
        f.parent.mkdir(parents=True)
        f.write_text("x")
        manager.add_to_cache("bucket1", "file1", f)
        f.unlink()
        assert manager.validate_cache("bucket1", "file1") == CacheValidationResult.NOT_FOUND
        assert manager.get_cached_file_path("bucket1", "file1").code == "CACHE_FILE_MISSING"

    def test_a_download_whose_size_cannot_be_checked_is_served_but_not_cached(
        self, file_manager, tmp_path
    ):
        """If the metadata lookup fails there is no declared size: the body is returned, but not
        cached, because a cached entry is trusted on every later read."""
        from views_crafdapi.managers.appwrite.results import OperationResult

        mgr = self._manager(file_manager, tmp_path, size_original=len(self.BODY))
        mgr.get_file = Mock(return_value=OperationResult(success=False, error="blip"))
        out = mgr.download_file("b", "f")
        assert out.success and out.data["file_bytes"] == self.BODY
        assert mgr.cache_manager.validate_cache("b", "f") == CacheValidationResult.NOT_FOUND
        save = tmp_path / "out.bin"
        assert mgr.download_file("b", "f", save_path=str(save)).success
        assert save.read_bytes() == self.BODY
        assert mgr.cache_manager.validate_cache("b", "f") == CacheValidationResult.NOT_FOUND
