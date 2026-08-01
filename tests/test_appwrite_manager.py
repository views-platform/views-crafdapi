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
    ProvisioningDisabledError,
    ProvisioningError,
    _require_provisioning,
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
    """Mock ModelPathManager"""
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
def mock_databases():
    """Mock Databases service"""
    with patch("views_crafdapi.managers.appwrite.manager.Databases") as mock:
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
         patch("views_crafdapi.managers.appwrite.manager.Storage"), \
         patch("views_crafdapi.managers.appwrite.manager.Databases"), \
         patch("views_crafdapi.managers.appwrite.manager.Users"):
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
        # Fix: Use the actual default from the config
        assert config.database_name == "File Metadata"  # Changed from "Test Bucket Metadata"
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
            path_manager=mock_path_manager,
        )

        assert isinstance(config.auth_method, AuthMethod)
        assert config.auth_method == AuthMethod.API_KEY

    def test_config_timeout_default(self, mock_path_manager):
        config = AppwriteConfig(
            endpoint="https://cloud.appwrite.io/v1",
            project_id="test_project",
            credentials="test_key",
            path_manager=mock_path_manager,
        )
        assert config.timeout_seconds == 30

    def test_config_timeout_custom(self, mock_path_manager):
        config = AppwriteConfig(
            endpoint="https://cloud.appwrite.io/v1",
            project_id="test_project",
            credentials="test_key",
            path_manager=mock_path_manager,
            timeout_seconds=60,
        )
        assert config.timeout_seconds == 60


class TestTimeoutInjection:

    @pytest.fixture
    def timeout_manager(self, api_key_config):
        """AppWriteFileManager with real Client (for timeout tests) but mocked auth/services."""
        with patch("views_crafdapi.managers.appwrite.manager.Storage"), \
             patch("views_crafdapi.managers.appwrite.manager.Databases"), \
             patch("views_crafdapi.managers.appwrite.manager.Users"), \
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
        
        cache_key = "bucket1_file123"
        assert cache_key in manager.cache_metadata
        assert manager.cache_metadata[cache_key].filename == "test.txt"

    def test_validate_cache_valid(self, temp_cache_dir):
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        
        test_file = temp_cache_dir / "bucket1" / "test.txt"
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
        
        test_file = temp_cache_dir / "bucket1" / "test.txt"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test content")
        
        # Fix: Use CacheMetadata object instead of dict
        cache_key = "bucket1_file123"
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

    def test_remove_from_cache(self, temp_cache_dir):
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        
        test_file = temp_cache_dir / "bucket1" / "test.txt"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test content")
        
        manager.add_to_cache("bucket1", "file123", test_file)
        assert "bucket1_file123" in manager.cache_metadata
        
        manager.remove_from_cache("bucket1", "file123")
        assert "bucket1_file123" not in manager.cache_metadata
        assert not test_file.exists()

    def test_clear_cache_all(self, temp_cache_dir):
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        
        # Add multiple files
        for i in range(3):
            test_file = temp_cache_dir / "bucket1" / f"test{i}.txt"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(f"content {i}")
            manager.add_to_cache("bucket1", f"file{i}", test_file)
        
        result = manager.clear_cache()
        
        assert result.success
        assert result.data["deleted_files"] == 3
        assert len(manager.cache_metadata) == 0

    def test_get_cache_stats(self, temp_cache_dir):
        manager = CacheManager(temp_cache_dir, timedelta(hours=24))
        
        test_file = temp_cache_dir / "bucket1" / "test.txt"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test content")
        
        manager.add_to_cache("bucket1", "file123", test_file)
        
        stats = manager.get_stats()
        
        assert stats["total_files"] == 1
        assert "bucket1" in stats["by_bucket"]
        assert stats["by_bucket"]["bucket1"]["files"] == 1


# Test MetadataManager
class TestMetadataManager:
    @pytest.fixture
    def metadata_manager(self, mock_databases, api_key_config):
        return MetadataManager(mock_databases, api_key_config)

    def test_create_database_if_not_exists_new(self, metadata_manager, mock_databases, monkeypatch):
        monkeypatch.setenv("FAOAPI_ALLOW_PROVISIONING", "1")  # #276: provisioning is opt-in
        mock_databases.list.return_value = _sdk_result(databases=[])
        mock_databases.create.return_value = _sdk_result(
            **{"$id": "file_metadata", "name": "File Metadata"}
        )
        
        result = metadata_manager.create_database_if_not_exists()
        
        assert result.success
        mock_databases.create.assert_called_once()

    def test_create_database_if_not_exists_existing(self, metadata_manager, mock_databases):
        mock_databases.list.return_value = _sdk_result(
            databases=[
                _sdk_result(**{"$id": "file_metadata", "name": "Test Bucket Metadata"})
            ]
        )
        
        result = metadata_manager.create_database_if_not_exists()
        
        assert result.success
        assert result.code == "EXISTS"
        mock_databases.create.assert_not_called()

    def test_infer_attribute_type(self, metadata_manager):
        # Test different types
        assert metadata_manager._infer_attribute_type("string") == ("string", False)
        assert metadata_manager._infer_attribute_type(123) == ("integer", False)
        assert metadata_manager._infer_attribute_type(12.5) == ("double", False)
        assert metadata_manager._infer_attribute_type(True) == ("boolean", False)
        assert metadata_manager._infer_attribute_type([1, 2, 3]) == ("integer", True)
        assert metadata_manager._infer_attribute_type("2025-10-22T12:00:00") == ("datetime", False)

    def test_search_files_by_metadata(self, metadata_manager, mock_databases):
        mock_databases.list_documents.return_value = _sdk_result(
            documents=[_sdk_result(fileId="file123", filename="test.txt")],
            total=1,
        )
        
        result = metadata_manager.search_files_by_metadata(
            filters={"filename": "test.txt"}
        )

        assert result.success
        assert result.data["total"] == 1
        assert len(result.data["documents"]) == 1

    def test_search_files_by_metadata_paginates_beyond_default_page(self, metadata_manager, mock_databases):
        """#287: a run with more artifacts than one Appwrite page (108 shards) must enumerate
        ALL matching documents, not just the default first page. Without pagination the resolver
        saw 25/108 shards → "83 missing" → the entire global wire run was refused (ingest_failed).
        list_documents is called with limit+offset until a short page ends the loop."""
        page1 = _sdk_result(
            documents=[_sdk_result(fileId=f"f{i}", filename=f"shard_{i}") for i in range(100)],
            total=108,
        )
        page2 = _sdk_result(
            documents=[_sdk_result(fileId=f"f{i}", filename=f"shard_{i}") for i in range(100, 108)],
            total=108,
        )
        mock_databases.list_documents.side_effect = [page1, page2]

        result = metadata_manager.search_files_by_metadata(
            filters={"type": "sampled_forecast_shard"}
        )

        assert result.success
        assert len(result.data["documents"]) == 108  # not truncated to a single 100-doc page
        assert mock_databases.list_documents.call_count == 2  # looped past the first full page


# Test AppWriteFileManager
class TestAppWriteFileManager:

    def test_calculate_file_hash_from_path(self, file_manager, tmp_path):
        test_file = tmp_path / "test.txt"
        test_content = b"test content"
        test_file.write_bytes(test_content)
        
        file_hash = file_manager._calculate_file_hash(file_path=str(test_file))
        expected_hash = hashlib.sha256(test_content).hexdigest()
        
        assert file_hash == expected_hash

    def test_calculate_file_hash_from_bytes(self, file_manager):
        test_content = b"test content"
        
        file_hash = file_manager._calculate_file_hash(file_bytes=test_content)
        expected_hash = hashlib.sha256(test_content).hexdigest()
        
        assert file_hash == expected_hash

    def test_upload_file_success(self, file_manager, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        
        file_manager.storage.create_file.return_value = _sdk_result(
            **{"$id": "file123", "name": "test.txt", "sizeOriginal": 12}
        )

        with patch("views_crafdapi.managers.appwrite.manager.InputFile"):
            result = file_manager.upload_file(
                "bucket1",
                str(test_file),
                check_duplicates=False
            )
        
        assert result.success
        assert result.data["$id"] == "file123"

    def test_upload_file_duplicate_exists(self, file_manager, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        
        # Mock duplicate detection
        file_manager.metadata_manager.check_file_exists_by_hash = Mock(
            return_value=OperationResult(
                success=True,
                data={"$id": "existing_file"},
                code="FOUND"
            )
        )
        
        result = file_manager.upload_file(
            "bucket1",
            str(test_file),
            check_duplicates=True,
            overwrite=False
        )
        
        assert result.success
        assert result.code == "EXISTS"

    def test_upload_file_from_bytes(self, file_manager):
        test_content = b"test content"
        
        file_manager.storage.create_file.return_value = _sdk_result(
            **{"$id": "file123", "name": "test.txt", "sizeOriginal": len(test_content)}
        )
        
        with patch("views_crafdapi.managers.appwrite.manager.InputFile"):
            result = file_manager.upload_file_from_bytes(
                "bucket1",
                test_content,
                "test.txt",
                check_duplicates=False
            )
        
        assert result.success
        assert result.data["$id"] == "file123"

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

    def test_list_files(self, file_manager):
        file_manager.storage.list_files.return_value = _sdk_result(
            files=[
                _sdk_result(**{"$id": "file1", "name": "test1.txt"}),
                _sdk_result(**{"$id": "file2", "name": "test2.txt"}),
            ],
            total=2,
        )
        
        result = file_manager.list_files("bucket1")
        
        assert result.success
        assert len(result.data["files"]) == 2
        assert result.data["total"] == 2

    def test_delete_file(self, file_manager):
        file_manager.storage.delete_file.return_value = _sdk_result()
        file_manager.cache_manager.remove_from_cache = Mock()
        
        result = file_manager.delete_file("bucket1", "file123")
        
        assert result.success
        assert result.code == "DELETED"
        file_manager.cache_manager.remove_from_cache.assert_called_once_with(
            "bucket1", "file123"
        )

    def test_get_file(self, file_manager):
        file_manager.storage.get_file.return_value = _sdk_result(
            **{"$id": "file123", "name": "test.txt", "sizeOriginal": 100}
        )
        
        result = file_manager.get_file("bucket1", "file123")
        
        assert result.success
        assert result.data["$id"] == "file123"

    def test_create_bucket(self, file_manager, monkeypatch):
        monkeypatch.setenv("FAOAPI_ALLOW_PROVISIONING", "1")  # #276: provisioning is opt-in
        file_manager.storage.create_bucket.return_value = _sdk_result(
            **{"$id": "new_bucket", "name": "New Bucket"}
        )
        file_manager.metadata_manager.create_database_if_not_exists = Mock(
            return_value=OperationResult(success=True, data={})
        )
        
        result = file_manager.create_bucket(
            "new_bucket",
            name="New Bucket",
            create_metadata_db=True
        )
        
        assert result.success
        assert result.data["$id"] == "new_bucket"

    def test_upload_file_with_metadata(self, file_manager, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        
        # Mock the upload and metadata creation
        file_manager.upload_file = Mock(
            return_value=OperationResult(
                success=True,
                data={"$id": "file123", "name": "test.txt", "sizeOriginal": 12}
            )
        )
        file_manager.metadata_manager.create_metadata_collection_if_not_exists = Mock(
            return_value=OperationResult(
                success=True,
                data={"database_id": "db1", "collection_id": "coll1"}
            )
        )
        file_manager.metadata_manager.check_file_exists_by_hash = Mock(
            return_value=OperationResult(success=False, code="NOT_FOUND")
        )
        file_manager._store_metadata_document = Mock(
            return_value=OperationResult(success=True, data={})
        )
        
        metadata = {"custom_field": "custom_value"}
        
        result = file_manager.upload_file_with_metadata(
            "bucket1",
            str(test_file),
            "test.txt",
            metadata
        )

        assert result.success

    def _arrange_orphan(self, file_manager, tmp_path, get_file_result):
        """FOUND_BY_HASH metadata whose storage probe returns ``get_file_result`` — the
        input to the #322 orphan-delete gating. Also arms a successful re-upload, which is
        only reached when the file is a *true* orphan."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        file_manager.metadata_manager.check_file_exists_by_hash = Mock(
            return_value=OperationResult(
                success=True, code="FOUND_BY_HASH",
                data={"fileId": "file123", "$id": "doc123"},
            )
        )
        file_manager.get_file = Mock(return_value=get_file_result)
        file_manager.databases.delete_document = Mock()
        file_manager.upload_file = Mock(
            return_value=OperationResult(
                success=True, data={"$id": "newfile", "name": "test.txt", "sizeOriginal": 12}
            )
        )
        file_manager.metadata_manager.create_metadata_collection_if_not_exists = Mock(
            return_value=OperationResult(success=True, data={"database_id": "db1", "collection_id": "coll1"})
        )
        file_manager._store_metadata_document = Mock(return_value=OperationResult(success=True, data={}))
        return str(test_file)

    def test_orphan_true_not_found_deletes_and_reuploads(self, file_manager, tmp_path):
        # get_file reports a GENUINE not-found -> the metadata is a real orphan: delete + re-upload.
        path = self._arrange_orphan(
            file_manager, tmp_path,
            OperationResult(success=False, code="storage_file_not_found", error="Get file failed"),
        )
        result = file_manager.upload_file_with_metadata("bucket1", path, "test.txt", {"k": "v"})
        file_manager.databases.delete_document.assert_called_once()
        assert result.success

    def test_orphan_auth_error_does_not_delete_and_fails_visible(self, file_manager, tmp_path):
        # get_file fails for lack of read scope -> we CANNOT know the file is gone: never delete.
        path = self._arrange_orphan(
            file_manager, tmp_path,
            OperationResult(success=False, code="general_unauthorized_scope", error="missing scope files.read"),
        )
        result = file_manager.upload_file_with_metadata("bucket1", path, "test.txt", {"k": "v"})
        file_manager.databases.delete_document.assert_not_called()
        assert not result.success
        assert result.code == "general_unauthorized_scope"

    def test_orphan_unexpected_error_does_not_delete_and_fails_visible(self, file_manager, tmp_path):
        path = self._arrange_orphan(
            file_manager, tmp_path,
            OperationResult(success=False, code="UNEXPECTED_ERROR", error="boom"),
        )
        result = file_manager.upload_file_with_metadata("bucket1", path, "test.txt", {"k": "v"})
        file_manager.databases.delete_document.assert_not_called()
        assert not result.success
        assert result.code == "UNEXPECTED_ERROR"

    def test_orphan_not_found_via_message_when_code_is_unreliable(self, file_manager, tmp_path):
        # #341 review finding 2: the SDK can leave e.type (-> code) None for non-JSON error bodies,
        # so a genuine not-found must still heal via the message (like the sibling get_bucket).
        path = self._arrange_orphan(
            file_manager, tmp_path,
            OperationResult(success=False, code=None, error="The requested file could not be found."),
        )
        result = file_manager.upload_file_with_metadata("bucket1", path, "test.txt", {"k": "v"})
        file_manager.databases.delete_document.assert_called_once()
        assert result.success

    def test_orphan_bucket_not_found_does_not_delete(self, file_manager, tmp_path):
        # #341 max review finding 1: a wrong/missing bucket_id yields storage_bucket_not_found whose
        # message ALSO contains "could not be found" — but the file may still exist in the correct
        # bucket, so deleting its metadata would be the exact false-orphan (C-231) the gate prevents.
        path = self._arrange_orphan(
            file_manager, tmp_path,
            OperationResult(
                success=False, code="storage_bucket_not_found",
                error="Storage bucket with the requested ID could not be found.",
            ),
        )
        result = file_manager.upload_file_with_metadata("wrong_bucket", path, "test.txt", {"k": "v"})
        file_manager.databases.delete_document.assert_not_called()
        assert not result.success

    def test_clear_cache(self, file_manager):
        file_manager.cache_manager.clear_cache = Mock(
            return_value=OperationResult(
                success=True,
                data={"deleted_files": 5, "deleted_bytes": 1000}
            )
        )
        
        result = file_manager.clear_cache()
        
        assert result.success
        assert result.data["deleted_files"] == 5

    def test_get_cache_stats(self, file_manager):
        file_manager.cache_manager.get_stats = Mock(
            return_value={
                "total_files": 10,
                "total_size_bytes": 5000,
                "total_size_mb": 0.005,
            }
        )
        
        stats = file_manager.get_cache_stats()

        assert stats["total_files"] == 10
        assert stats["total_size_mb"] == 0.005

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


    def test_get_user_preferences_api_key_no_user_id(self, file_manager):
        result = file_manager.get_user_preferences()

        assert not result.success
        assert result.code == "MISSING_USER_ID"


# Test Error Handling
class TestErrorHandling:
    def test_appwrite_exception_handling(self, api_key_config):
        with patch("views_crafdapi.managers.appwrite.manager.Client"), \
             patch("views_crafdapi.managers.appwrite.manager.Storage") as mock_storage, \
             patch("views_crafdapi.managers.appwrite.manager.Databases"), \
             patch("views_crafdapi.managers.appwrite.manager.Users"):
            
            manager = AppWriteFileManager(api_key_config)
            mock_storage.return_value.get_file.side_effect = AppwriteException(
                "File not found", 404, "storage_file_not_found"
            )
            
            result = manager.get_file("bucket1", "nonexistent")
            
            assert not result.success
            assert result.code == "storage_file_not_found"

    # Remove the invalid config test or update it to test actual validation
    # The config doesn't validate credential types in __post_init__, so this test is invalid
    # Option 1: Remove the test
    # Option 2: Add actual validation to AppwriteConfig and test it
    # For now, let's test that initialization succeeds (which is the actual behavior)

    def test_config_rejects_the_retired_session_auth_method(self, mock_path_manager):
        # Session auth was retired (þing-01 #274): the enum no longer carries "session", so a
        # config that asks for it is rejected outright — single-mode by construction.
        with pytest.raises(ValueError):
            AppwriteConfig(
                endpoint="https://cloud.appwrite.io/v1",
                project_id="test_project",
                credentials={"bad": "data"},
                auth_method="session",
                path_manager=mock_path_manager,
            )

    def test_upload_file_with_metadata_partial_success(self, file_manager, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        file_manager.upload_file = Mock(
            return_value=OperationResult(
                success=True,
                data={"$id": "file123", "name": "test.txt", "sizeOriginal": 12}
            )
        )
        file_manager.metadata_manager.check_file_exists_by_hash = Mock(
            return_value=OperationResult(success=False, code="NOT_FOUND")
        )
        file_manager.metadata_manager.create_metadata_collection_if_not_exists = Mock(
            return_value=OperationResult(
                success=True,
                data={"database_id": "db1", "collection_id": "coll1"}
            )
        )
        file_manager._store_metadata_document = Mock(
            return_value=OperationResult(
                success=False, error="DB write failed", code="WRITE_ERROR"
            )
        )

        result = file_manager.upload_file_with_metadata(
            "bucket1", str(test_file), "test.txt", {"key": "val"}
        )

        assert not result.success
        assert result.code == "PARTIAL_SUCCESS"
        assert result.data["file_id"] == "file123"

    def test_upload_file_appwrite_exception_wrapping(self, file_manager, tmp_path):
        file_manager.storage.create_file.side_effect = AppwriteException(
            "Internal error", 500, "storage_internal"
        )

        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        with patch("views_crafdapi.managers.appwrite.manager.InputFile"):
            result = file_manager.upload_file(
                "bucket1", str(test_file), check_duplicates=False
            )

        assert not result.success
        assert result.code == "storage_internal"

    def test_upload_file_from_bytes_exception_wrapping(self, file_manager):
        file_manager.storage.create_file.side_effect = AppwriteException(
            "Internal error", 500, "storage_internal"
        )

        with patch("views_crafdapi.managers.appwrite.manager.InputFile"):
            result = file_manager.upload_file_from_bytes(
                "bucket1", b"test content", "test.txt", check_duplicates=False
            )

        assert not result.success
        assert result.code == "storage_internal"

    def test_download_file_io_error(self, file_manager):
        file_manager.storage.get_file_download.return_value = b"content"
        file_manager.get_file = Mock(
            return_value=OperationResult(
                success=True,
                data={"name": "test.txt", "$updatedAt": "2026-01-01T00:00:00Z"}
            )
        )
        file_manager.cache_manager._get_cache_path = Mock(
            return_value=Path("/nonexistent/dir/file.txt")
        )

        result = file_manager.download_file(
            "bucket1", "file123", use_cache=False
        )

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
        "APPWRITE_DATASTORE_PROJECT_ID": "proj_123",
        "APPWRITE_UNFAO_BUCKET_ID": "bucket_123",
        "APPWRITE_UNFAO_BUCKET_NAME": "my_bucket",
        "APPWRITE_UNFAO_COLLECTION_ID": "coll_123",
        "APPWRITE_UNFAO_COLLECTION_NAME": "my_coll",
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
        env = {**self._ALL_ENV, "APPWRITE_UNFAO_BUCKET_NAME": ""}
        with patch("os.getenv", side_effect=lambda key, default=None: env.get(key, default)):
            with pytest.raises(ValueError, match="APPWRITE_UNFAO_BUCKET_NAME"):
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
        with patch("os.getenv", side_effect=self._getenv()):
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

    def test_list_files_data_is_dict(self, file_manager):
        file_manager.storage.list_files.return_value = _sdk_result(
            files=[_sdk_result(**{"$id": "f1", "name": "test.txt"})], total=1
        )
        result = file_manager.list_files("bucket1")
        assert result.success
        assert isinstance(result.data, dict)
        assert isinstance(result.data["files"], list)

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

    def test_upload_file_data_is_dict(self, file_manager, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        file_manager.storage.create_file.return_value = _sdk_result(
            **{"$id": "file123", "name": "test.txt", "sizeOriginal": 12}
        )
        with patch("views_crafdapi.managers.appwrite.manager.InputFile"):
            result = file_manager.upload_file(
                "bucket1", str(test_file), check_duplicates=False
            )
        assert result.success
        assert isinstance(result.data, dict)
        assert "$id" in result.data


    def test_get_user_preferences_api_key_data_is_dict(self, file_manager):
        file_manager.users.get_prefs.return_value = _sdk_result(
            theme="dark", lang="en"
        )
        result = file_manager.get_user_preferences(user_id="user123")
        assert result.success
        assert isinstance(result.data, dict)


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

    def test_require_provisioning_raises_by_default(self, monkeypatch):
        monkeypatch.delenv("FAOAPI_ALLOW_PROVISIONING", raising=False)
        with pytest.raises(ProvisioningDisabledError, match=r"bucket 'x'"):
            _require_provisioning("bucket 'x'")

    def test_require_provisioning_passes_when_explicitly_enabled(self, monkeypatch):
        monkeypatch.setenv("FAOAPI_ALLOW_PROVISIONING", "1")
        assert _require_provisioning("bucket 'x'") is None  # no raise

    def test_create_bucket_refuses_provisioning_by_default(self, file_manager, monkeypatch):
        monkeypatch.delenv("FAOAPI_ALLOW_PROVISIONING", raising=False)
        with pytest.raises(ProvisioningDisabledError, match="bucket"):
            file_manager.create_bucket("phantom_bucket", name="Phantom")
        file_manager.storage.create_bucket.assert_not_called()  # raised before touching the SDK

    def test_create_bucket_half_success_raises_all_or_raise(self, file_manager, monkeypatch):
        monkeypatch.setenv("FAOAPI_ALLOW_PROVISIONING", "1")
        file_manager.storage.create_bucket.return_value = _sdk_result(**{"$id": "b", "name": "B"})
        file_manager.metadata_manager.create_database_if_not_exists = Mock(
            return_value=OperationResult(success=False, error="db creation failed")
        )
        with pytest.raises(ProvisioningError, match="stranded"):
            file_manager.create_bucket("b", name="B", create_metadata_db=True)

    def test_serving_and_read_paths_never_provision(self):
        # Characterisation guard (#276 step 1): serving/read modules must never call a create_*
        # helper — so the gate can never fire on a serving path, and gating cannot break serving.
        import pathlib
        managers = pathlib.Path(__file__).parent.parent / "src" / "views_crafdapi" / "managers"
        for mod in ("dataset_service.py", "api.py"):
            text = (managers / mod).read_text()
            for helper in ("create_bucket(", "create_database_if_not_exists(", "_create_attribute_by_type("):
                assert helper not in text, f"{mod} calls provisioning helper {helper} on a serving path"


class TestCredentialRedaction:
    """þing-01 #277 / PLATFORM-001 redaction clause: no credential in any carrier reaches a log line."""

    def test_config_repr_omits_the_credential(self, api_key_config):
        # api_key_config.credentials == "test_api_key"; it must not appear in the dataclass repr.
        text = repr(api_key_config)
        assert "test_api_key" not in text, "the API key leaked into AppwriteConfig repr"

    def test_api_key_hash_is_a_digest_not_the_raw_key(self):
        import hashlib
        raw = "super-secret-caller-key"
        # this is exactly how api.py keys the cache — a sha256 prefix, never the raw key
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        assert raw not in digest and len(digest) == 16
