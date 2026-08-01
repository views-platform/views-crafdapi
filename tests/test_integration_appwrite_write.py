"""Tier B integration tests: write path, cache, failure modes.

(Session-auth integration tests were removed with the session path — þing-01 #274.)

Requires APPWRITE_* environment variables to be set (from .env on Hetzner).
Skipped automatically when credentials are missing.

These tests MUTATE live Appwrite state (upload, delete). Every write test
cleans up via try/finally to avoid polluting the production bucket.

Run explicitly:
    PYTHONPATH=src pytest tests/test_integration_appwrite_write.py -v -m integration
"""

import io
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

REQUIRED_ENV_VARS = [
    "APPWRITE_ENDPOINT",
    "APPWRITE_DATASTORE_PROJECT_ID",
    "APPWRITE_UNFAO_BUCKET_ID",
    "APPWRITE_METADATA_DATABASE_ID",
    "APPWRITE_UNFAO_COLLECTION_ID",
    "APPWRITE_DATASTORE_API_KEY",
]

_missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]

skip_no_creds = pytest.mark.skipif(
    len(_missing) > 0,
    reason=f"Missing Appwrite env vars: {', '.join(_missing)}",
)

pytestmark = [pytest.mark.integration, pytest.mark.layer1_storage, skip_no_creds]





# ============================================================
# Helpers
# ============================================================


def _unique_filename(prefix="tierb"):
    return f"{prefix}_{uuid4().hex[:8]}.bin"


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture(scope="module")
def appwrite_config():
    from views_faoapi.managers.appwrite import AppwriteConfig

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
    from views_faoapi.managers.appwrite import AppWriteFileManager

    return AppWriteFileManager(appwrite_config)


@pytest.fixture(scope="module")
def prediction_manager(appwrite_config):
    from views_faoapi.managers.prediction import PredictionStoreManager

    return PredictionStoreManager(appwrite_file_manager_config=appwrite_config)


@pytest.fixture(scope="module")
def bucket_id(appwrite_config):
    return appwrite_config.bucket_id


@pytest.fixture(scope="module")
def cached_file_manager(appwrite_config):
    from views_faoapi.managers.appwrite import AppWriteFileManager, AppwriteConfig

    cache_dir = tempfile.mkdtemp(prefix="tierb_cache_")
    kwargs = dict(
        endpoint=appwrite_config.endpoint,
        project_id=appwrite_config.project_id,
        credentials=appwrite_config.credentials,
        auth_method="api_key",
        cache_ttl_hours=24,
        cache_dir=cache_dir,
        bucket_id=appwrite_config.bucket_id,
        collection_id=appwrite_config.collection_id,
        database_id=appwrite_config.database_id,
    )
    for attr, kwarg_key in [
        ("bucket_name", "bucket_name"),
        ("collection_name", "collection_name"),
        ("database_name", "database_name"),
    ]:
        val = getattr(appwrite_config, attr, None)
        if val:
            kwargs[kwarg_key] = val

    config = AppwriteConfig(**kwargs)
    manager = AppWriteFileManager(config)
    yield manager
    shutil.rmtree(cache_dir, ignore_errors=True)






# ============================================================
# F-3: Write-Read Round-Trip (byte identity)
# ============================================================


class TestWriteReadRoundTrip:

    def test_upload_download_byte_identity(self, file_manager, bucket_id):
        known_content = b"Tier-B round-trip probe: " + uuid4().bytes
        file_id = None

        tmp = Path(tempfile.mktemp(suffix=".bin"))
        tmp.write_bytes(known_content)
        try:
            upload = file_manager.upload_file(bucket_id, str(tmp))
            assert upload.success, f"Upload failed: {upload.error}"
            file_id = upload.data["$id"]

            download = file_manager.download_file(bucket_id, file_id, use_cache=False)
            assert download.success, f"Download failed: {download.error}"
            assert download.data["file_bytes"] == known_content, (
                f"Round-trip mismatch: sent {len(known_content)}B, "
                f"got {len(download.data.get('file_bytes', b''))}B"
            )
        finally:
            tmp.unlink(missing_ok=True)
            if file_id:
                file_manager.delete_file(bucket_id, file_id)

    def test_upload_download_parquet_round_trip(self, file_manager, bucket_id):
        from tests.conftest import make_fao_df

        df_original = make_fao_df(n_cells=2, n_months=1, n_samples=5)
        tmp = Path(tempfile.mktemp(suffix=".parquet"))
        df_original.to_parquet(tmp)
        file_id = None

        try:
            upload = file_manager.upload_file(bucket_id, str(tmp))
            assert upload.success, f"Upload failed: {upload.error}"
            file_id = upload.data["$id"]

            download = file_manager.download_file(bucket_id, file_id, use_cache=False)
            assert download.success, f"Download failed: {download.error}"

            df_downloaded = pd.read_parquet(io.BytesIO(download.data["file_bytes"]))
            assert df_downloaded.shape == df_original.shape
            assert list(df_downloaded.columns) == list(df_original.columns)
            assert list(df_downloaded.index.names) == list(df_original.index.names)
        finally:
            tmp.unlink(missing_ok=True)
            if file_id:
                file_manager.delete_file(bucket_id, file_id)


# ============================================================
# F-1: Upload With Metadata + Hash Dedup
# ============================================================


class TestUploadWithMetadata:

    def test_upload_creates_file_and_metadata_document(self, file_manager, bucket_id):
        content = b"metadata probe " + uuid4().bytes
        filename = _unique_filename("meta")
        tmp = Path(tempfile.mktemp(suffix=".bin"))
        tmp.write_bytes(content)
        file_id = None

        try:
            result = file_manager.upload_file_with_metadata(
                bucket_id=bucket_id,
                file_path=str(tmp),
                filename=filename,
                metadata={"probe": "F-1", "purpose": "tierb_metadata"},
            )
            assert result.success, f"Upload with metadata failed: {result.error}"
            assert result.code == "UPLOAD_SUCCESS"
            assert "file_id" in result.data
            assert "document_id" in result.data
            file_id = result.data["file_id"]

            file_check = file_manager.get_file(bucket_id, file_id)
            assert file_check.success, "Uploaded file not found in storage"
        finally:
            tmp.unlink(missing_ok=True)
            if file_id:
                file_manager.delete_file(bucket_id, file_id)

    def test_hash_dedup_returns_metadata_updated(self, file_manager, bucket_id):
        content = b"dedup probe " + uuid4().bytes
        filename = _unique_filename("dedup")
        tmp = Path(tempfile.mktemp(suffix=".bin"))
        tmp.write_bytes(content)
        file_id = None

        try:
            first = file_manager.upload_file_with_metadata(
                bucket_id=bucket_id,
                file_path=str(tmp),
                filename=filename,
                metadata={"probe": "F-1-dedup", "round": "first"},
            )
            assert first.success, f"First upload failed: {first.error}"
            file_id = first.data["file_id"]

            second = file_manager.upload_file_with_metadata(
                bucket_id=bucket_id,
                file_path=str(tmp),
                filename=filename,
                metadata={"probe": "F-1-dedup", "round": "second"},
            )
            assert second.success, f"Second upload failed: {second.error}"
            assert second.code in ("METADATA_UPDATED", "EXISTS", "EXISTS_METADATA_UPDATED"), (
                f"Expected dedup skip, got code={second.code}"
            )
        finally:
            tmp.unlink(missing_ok=True)
            if file_id:
                file_manager.delete_file(bucket_id, file_id)

    def test_orphan_cleanup_re_uploads_after_stale_metadata(self, file_manager, bucket_id):
        content = b"orphan probe " + uuid4().bytes
        filename = _unique_filename("orphan")
        tmp = Path(tempfile.mktemp(suffix=".bin"))
        tmp.write_bytes(content)
        file_id = None
        second_file_id = None

        try:
            first = file_manager.upload_file_with_metadata(
                bucket_id=bucket_id,
                file_path=str(tmp),
                filename=filename,
                metadata={"probe": "F-1-orphan"},
            )
            assert first.success, f"Initial upload failed: {first.error}"
            file_id = first.data["file_id"]

            # Delete ONLY the storage file — leave metadata as orphan
            delete_result = file_manager.delete_file(bucket_id, file_id)
            assert delete_result.success, f"Storage delete failed: {delete_result.error}"

            # Re-upload same content — should detect orphan, clean up, re-upload
            second = file_manager.upload_file_with_metadata(
                bucket_id=bucket_id,
                file_path=str(tmp),
                filename=filename,
                metadata={"probe": "F-1-orphan-reupload"},
            )
            assert second.success, f"Re-upload after orphan failed: {second.error}"
            assert second.code == "UPLOAD_SUCCESS", (
                f"Expected fresh UPLOAD_SUCCESS after orphan cleanup, got code={second.code}"
            )
            second_file_id = second.data["file_id"]
        finally:
            tmp.unlink(missing_ok=True)
            cleanup_id = second_file_id or file_id
            if cleanup_id:
                file_manager.delete_file(bucket_id, cleanup_id)


# ============================================================
# F-2: Live Failure Modes
# ============================================================


class TestLiveFailureModes:

    def test_download_nonexistent_returns_failure(self, file_manager, bucket_id):
        result = file_manager.download_file(
            bucket_id=bucket_id,
            file_id="nonexistent_000000",
            use_cache=False,
        )
        assert not result.success, "Expected failure for nonexistent file"
        assert isinstance(result.error, str), "Failure should include error message"
        assert result.code is not None, "Failure should include error code"

    def test_get_file_nonexistent_returns_failure(self, file_manager, bucket_id):
        result = file_manager.get_file(bucket_id, "nonexistent_000000")
        assert not result.success
        assert isinstance(result.error, str)

    def test_delete_nonexistent_returns_failure(self, file_manager, bucket_id):
        result = file_manager.delete_file(bucket_id, "nonexistent_000000")
        assert not result.success


# ============================================================
# F-5: ADR-018 Normalization on Live Data
# ============================================================


class TestADR018NormalizationLive:

    def test_list_files_data_is_dict(self, file_manager, bucket_id):
        result = file_manager.list_files(bucket_id, limit=1)
        assert result.success
        assert isinstance(result.data, dict), (
            f"ADR-018 violation: list_files data is {type(result.data).__name__}"
        )
        assert isinstance(result.data["files"], list)

    def test_download_data_is_dict(self, prediction_manager):
        file_id = prediction_manager.get_latest_file_id(
            filters={"category": "forecast"}
        )
        if file_id is None:
            pytest.skip("No forecast file available")

        result = prediction_manager.download_prediction(file_id, use_cache=False)
        assert result.success
        assert isinstance(result.data, dict), (
            f"ADR-018 violation: download data is {type(result.data).__name__}"
        )
        assert "file_bytes" in result.data

    def test_upload_result_data_is_dict(self, file_manager, bucket_id):
        content = b"adr018 probe " + uuid4().bytes
        tmp = Path(tempfile.mktemp(suffix=".bin"))
        tmp.write_bytes(content)
        file_id = None

        try:
            result = file_manager.upload_file(bucket_id, str(tmp))
            assert result.success
            assert isinstance(result.data, dict), (
                f"ADR-018 violation: upload data is {type(result.data).__name__}"
            )
            file_id = result.data.get("$id")
        finally:
            tmp.unlink(missing_ok=True)
            if file_id:
                file_manager.delete_file(bucket_id, file_id)


# ============================================================
# F-6: PredictionStoreManager Mutations
# ============================================================


class TestPredictionManagerMutations:

    def test_upload_predictions_with_file_path(
        self, prediction_manager, file_manager, bucket_id
    ):
        from tests.conftest import make_fao_df

        df = make_fao_df(n_cells=2, n_months=1, n_samples=5)
        tmp = Path(tempfile.mktemp(suffix=".parquet"))
        df.to_parquet(tmp)
        file_id = None

        try:
            result = prediction_manager.upload_predictions(
                file=tmp,
                filename=_unique_filename("pred"),
                loa="pgm",
                name="tierb_probe",
                type="test",
                targets=["pred_test"],
                category="forecast",
            )
            assert result.success, f"upload_predictions failed: {result.error}"
            file_id = result.data.get("file_id")
            assert file_id is not None, "No file_id in upload result"
        finally:
            tmp.unlink(missing_ok=True)
            if file_id:
                file_manager.delete_file(bucket_id, file_id)

    def test_list_all_predictions_unfiltered(self, prediction_manager):
        results = prediction_manager.list_all_predictions_unfiltered()
        assert isinstance(results, list), (
            f"Expected list, got {type(results).__name__}"
        )

    def test_delete_prediction_removes_file(
        self, prediction_manager, file_manager, bucket_id
    ):
        from tests.conftest import make_fao_df

        df = make_fao_df(n_cells=2, n_months=1, n_samples=5)
        tmp = Path(tempfile.mktemp(suffix=".parquet"))
        df.to_parquet(tmp)
        file_id = None

        try:
            upload = prediction_manager.upload_predictions(
                file=tmp,
                filename=_unique_filename("del_probe"),
                loa="pgm",
                name="tierb_delete_probe",
                type="test",
                targets=["pred_test"],
                category="forecast",
            )
            assert upload.success, f"Upload failed: {upload.error}"
            file_id = upload.data.get("file_id")

            check_before = file_manager.get_file(bucket_id, file_id)
            assert check_before.success, "File should exist before deletion"

            delete_result = prediction_manager.delete_prediction(file_id)
            assert delete_result.success, f"Delete failed: {delete_result.error}"

            check_after = file_manager.get_file(bucket_id, file_id)
            assert not check_after.success, "File should be gone after deletion"
            file_id = None  # Already deleted
        finally:
            tmp.unlink(missing_ok=True)
            if file_id:
                file_manager.delete_file(bucket_id, file_id)


# ============================================================
# F-7: Cache Integration
# ============================================================


class TestCacheIntegration:

    def test_download_cache_round_trip(self, cached_file_manager, bucket_id, prediction_manager):
        file_id = prediction_manager.get_latest_file_id(
            filters={"category": "forecast"}
        )
        if file_id is None:
            pytest.skip("No forecast file available")

        first = cached_file_manager.download_file(bucket_id, file_id, use_cache=True)
        assert first.success, f"First download failed: {first.error}"
        assert first.code == "RETURNED_FROM_REMOTE", (
            f"First download should be from remote, got code={first.code}"
        )

        second = cached_file_manager.download_file(bucket_id, file_id, use_cache=True)
        assert second.success, f"Second download failed: {second.error}"
        assert second.code == "RETURNED_FROM_CACHE", (
            f"Second download should be from cache, got code={second.code}"
        )

    def test_cached_bytes_match_remote_bytes(self, cached_file_manager, bucket_id, prediction_manager):
        file_id = prediction_manager.get_latest_file_id(
            filters={"category": "forecast"}
        )
        if file_id is None:
            pytest.skip("No forecast file available")

        # Ensure file is cached from prior test or cache it now
        cached = cached_file_manager.download_file(bucket_id, file_id, use_cache=True)
        assert cached.success

        remote = cached_file_manager.download_file(bucket_id, file_id, use_cache=False)
        assert remote.success

        assert cached.data["file_bytes"] == remote.data["file_bytes"], (
            "Cached bytes differ from remote bytes"
        )


# ============================================================
# F-8: Session Auth (conditional)
# ============================================================


