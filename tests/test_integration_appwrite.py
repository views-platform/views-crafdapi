"""Integration tests that hit real Appwrite.

Requires APPWRITE_* environment variables to be set (from .env on Hetzner).
Skipped automatically when credentials are missing.

Run explicitly:
    pytest tests/test_integration_appwrite.py -v -m integration
"""

import io
import os

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


@pytest.fixture(scope="module")
def appwrite_config():
    from views_faoapi.managers.appwrite import AppwriteConfig

    return AppwriteConfig(
        endpoint=os.getenv("APPWRITE_ENDPOINT"),
        project_id=os.getenv("APPWRITE_DATASTORE_PROJECT_ID"),
        credentials=os.getenv("APPWRITE_DATASTORE_API_KEY"),
        auth_method="api_key",
        cache_ttl_hours=24,
        bucket_id=os.getenv("APPWRITE_UNFAO_BUCKET_ID"),
        bucket_name=os.getenv("APPWRITE_UNFAO_BUCKET_NAME"),
        collection_id=os.getenv("APPWRITE_UNFAO_COLLECTION_ID"),
        collection_name=os.getenv("APPWRITE_UNFAO_COLLECTION_NAME"),
        database_id=os.getenv("APPWRITE_METADATA_DATABASE_ID"),
        database_name=os.getenv("APPWRITE_METADATA_DATABASE_NAME"),
    )


@pytest.fixture(scope="module")
def file_manager(appwrite_config):
    from views_faoapi.managers.appwrite import AppWriteFileManager

    return AppWriteFileManager(appwrite_config)


@pytest.fixture(scope="module")
def prediction_manager(appwrite_config):
    from views_faoapi.managers.prediction import PredictionStoreManager

    return PredictionStoreManager(appwrite_file_manager_config=appwrite_config)


class TestAppwriteConnection:

    def test_auth_succeeds(self, file_manager):
        result = file_manager.list_buckets(limit=1)
        assert result.success, f"Auth failed: {result.error}"

    def test_bucket_accessible(self, file_manager, appwrite_config):
        result = file_manager.list_files(
            bucket_id=appwrite_config.bucket_id, limit=1
        )
        assert result.success, f"Cannot access bucket: {result.error}"


class TestDataDownload:

    def test_forecast_file_exists(self, prediction_manager):
        file_id = prediction_manager.get_latest_file_id(
            filters={"category": "forecast"}
        )
        assert file_id is not None, "No forecast file found in bucket"

    def test_historical_file_exists(self, prediction_manager):
        file_id = prediction_manager.get_latest_file_id(
            filters={"category": "historical"}
        )
        assert file_id is not None, "No historical file found in bucket"

    def test_download_forecast_file(self, prediction_manager):
        file_id = prediction_manager.get_latest_file_id(
            filters={"category": "forecast"}
        )
        assert file_id is not None, "No forecast file to download"

        result = prediction_manager.download_prediction(
            file_id, use_cache=False
        )
        assert result.success, f"Download failed: {result.error}"
        file_bytes = result.data.get("file_bytes")
        assert file_bytes is not None, "Downloaded file is empty"
        assert len(file_bytes) > 0, "Downloaded file has zero bytes"


class TestFormatDetectionAndParsing:
    """Tests the 5-format auto-detection cascade on real data."""

    @pytest.fixture(scope="class")
    def forecast_bytes(self, prediction_manager):
        file_id = prediction_manager.get_latest_file_id(
            filters={"category": "forecast"}
        )
        assert file_id is not None
        result = prediction_manager.download_prediction(
            file_id, use_cache=False
        )
        assert result.success
        return result.data["file_bytes"]

    def test_parses_to_dataframe(self, forecast_bytes):
        df = self._try_parse(forecast_bytes)
        assert df is not None, "Could not parse file in any supported format"
        assert len(df) > 0, "Parsed DataFrame is empty"

    def test_has_multiindex(self, forecast_bytes):
        df = self._try_parse(forecast_bytes)
        assert isinstance(
            df.index, pd.MultiIndex
        ), f"Expected MultiIndex, got {type(df.index).__name__}"
        assert (
            df.index.names[0] == "month_id"
        ), f"Expected index[0]='month_id', got '{df.index.names[0]}'"
        assert (
            df.index.names[1] == "priogrid_id"
        ), f"Expected index[1]='priogrid_id', got '{df.index.names[1]}'"

    def test_has_prediction_columns(self, forecast_bytes):
        df = self._try_parse(forecast_bytes)
        pred_cols = [c for c in df.columns if c.startswith("pred_")]
        assert (
            len(pred_cols) > 0
        ), f"No pred_* columns found. Columns: {list(df.columns)[:20]}"

    def test_has_metadata_columns(self, forecast_bytes):
        from views_faoapi.data.handlers import FAO_PGMDataset

        df = self._try_parse(forecast_bytes)
        missing = [
            c for c in FAO_PGMDataset._METADATA_COLS if c not in df.columns
        ]
        assert (
            len(missing) == 0
        ), f"Missing metadata columns: {missing}"

    @staticmethod
    def _try_parse(file_bytes: bytes) -> pd.DataFrame:
        """Reproduce the 5-format cascade from api.py."""
        buf = io.BytesIO(file_bytes)
        for reader in [
            lambda b: pd.read_parquet(b),
            lambda b: pd.read_csv(b, encoding="utf-8"),
            lambda b: pd.read_csv(b, encoding="latin-1"),
            lambda b: pd.read_csv(b, encoding="iso-8859-1"),
            lambda b: pd.read_csv(b, encoding="cp1252"),
            lambda b: pd.read_json(b),
            lambda b: pd.read_pickle(b),
            lambda b: pd.read_feather(b),
        ]:
            try:
                buf.seek(0)
                return reader(buf)
            except Exception:
                continue
        return None


class TestDatasetConstruction:
    """Tests that downloaded data can be wrapped in FAO_PGMDataset."""

    @pytest.fixture(scope="class")
    def forecast_df(self, prediction_manager):
        file_id = prediction_manager.get_latest_file_id(
            filters={"category": "forecast"}
        )
        assert file_id is not None
        result = prediction_manager.download_prediction(
            file_id, use_cache=False
        )
        return TestFormatDetectionAndParsing._try_parse(result.data["file_bytes"])

    def test_fao_pgm_dataset_constructs(self, forecast_df):
        from views_faoapi.data.handlers import FAO_PGMDataset

        dataset = FAO_PGMDataset(forecast_df)
        assert dataset.dataframe is not None
        assert len(dataset.dataframe) > 0

    def test_dataset_has_geo_metadata(self, forecast_df):
        from views_faoapi.data.handlers import FAO_PGMDataset

        dataset = FAO_PGMDataset(forecast_df)
        assert dataset.geo_metadata is not None
        assert len(dataset.geo_metadata) == len(dataset.dataframe)

    def test_dataset_has_targets(self, forecast_df):
        from views_faoapi.data.handlers import FAO_PGMDataset

        dataset = FAO_PGMDataset(forecast_df)
        assert len(dataset.targets) > 0, "Dataset detected no targets"

    def test_dataset_has_aggregation_levels(self, forecast_df):
        from views_faoapi.data.handlers import FAO_PGMDataset

        dataset = FAO_PGMDataset(forecast_df)
        assert "country" in dataset.levels
        assert "gaul0" in dataset.levels
        assert "gaul1" in dataset.levels
        assert "gaul2" in dataset.levels

    def test_dataset_validates_indices(self, forecast_df):
        from views_faoapi.data.handlers import FAO_PGMDataset

        dataset = FAO_PGMDataset(forecast_df)
        dataset.validate_indices()
