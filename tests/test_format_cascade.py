"""Tests for the format auto-detection cascade in FAOApiManager._get_latest_dataframe (C-11).

The cascade tries: parquet → CSV (utf-8 only) → JSON → feather. **Pickle was REMOVED**
(register C-59): `pd.read_pickle` runs `pickle.load`, which executes arbitrary code on
deserialization (RCE on untrusted Appwrite bytes). `TestPickleRefused` asserts a crafted
pickle is no longer executed.
After parsing, it constructs FAO_PGMDataset, which requires a 2-level MultiIndex and metadata
columns. Formats that don't preserve MultiIndex (CSV, JSON, feather) will parse successfully
but fail at dataset construction.

C-29 resolution: latin-1/iso-8859-1/cp1252 CSV encodings were removed because they accept any
byte sequence, making the error-accumulation path unreachable. With utf-8 only, the cascade
can now genuinely fail all formats and surface diagnostic error messages.
"""

import io

import numpy as np
import pyarrow.feather as feather
import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock, patch

from tests.conftest import make_fao_df
from views_faoapi.managers.api import FAOApiManager
from views_faoapi.managers.prediction import PredictionProvenance

pytestmark = pytest.mark.layer2_data


def _manager_with_bytes(tmp_path, file_bytes):
    """Create a FAOApiManager wired to return `file_bytes` from a mock PredictionStoreManager."""
    mgr = FAOApiManager.from_config(
        {"deployment": {"host": "0.0.0.0", "port": 80}},
        cache_dir=tmp_path / "cache",
    )
    mgr._prediction_bucket_id = "test-bucket"

    mock_pm = MagicMock()
    mock_pm.get_latest_file_id.return_value = "file_001"
    # _get_latest_dataframe resolves the artifact via get_latest_provenance (C-86).
    mock_pm.get_latest_provenance.return_value = PredictionProvenance(
        file_id="file_001", source="test", created_at="2025-10-22T12:00:00.000Z"
    )

    download_result = MagicMock()
    download_result.success = True
    download_result.data = {"file_bytes": file_bytes}
    mock_pm.download_prediction.return_value = download_result

    return mgr, mock_pm


def _make_test_df():
    return make_fao_df(n_cells=4, n_months=2, n_samples=10, seed=99)


# ============================================================
# Formats that preserve MultiIndex (end-to-end)
# ============================================================


class TestParquetFormat:

    def test_parquet_parsed(self, tmp_path):
        df = _make_test_df()
        buf = io.BytesIO()
        df.to_parquet(buf)

        mgr, mock_pm = _manager_with_bytes(tmp_path, buf.getvalue())
        result = mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert result is not None
        assert result.shape[0] == 8


_PICKLE_EXECUTED = []


def _evil_side_effect():
    """Records that a malicious pickle's payload ran (it must not). Returns a non-DataFrame."""
    _PICKLE_EXECUTED.append(True)
    return {}


class _EvilPayload:
    """A pickle whose __reduce__ runs `_evil_side_effect` on load (the `pickle.load` RCE path)."""

    def __reduce__(self):
        return (_evil_side_effect, ())


class TestPickleRefused:
    """C-59: pickle is removed from the cascade — a crafted pickle must NOT be deserialized."""

    def test_malicious_pickle_is_refused_not_executed(self, tmp_path):
        import pickle

        _PICKLE_EXECUTED.clear()
        # Non-PAR1 bytes whose deserialization would run code under pd.read_pickle.
        payload = pickle.dumps(_EvilPayload())

        mgr, mock_pm = _manager_with_bytes(tmp_path, payload)
        with pytest.raises(HTTPException):
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert _PICKLE_EXECUTED == [], "pickle payload was deserialized — the RCE path is reachable"


# ============================================================
# Formats that lose MultiIndex — patch FAO_PGMDataset to isolate cascade
# ============================================================


def _mock_dataset(df, **kwargs):
    """Return a mock dataset that wraps the parsed DataFrame."""
    mock = MagicMock()
    mock.dataframe = df
    return mock


class TestCSVFormat:

    @patch("views_faoapi.managers.dataset_service.ForecastDataset", side_effect=_mock_dataset)
    def test_csv_utf8_parsed(self, mock_cls, tmp_path):
        df = _make_test_df()
        csv_bytes = df.to_csv().encode("utf-8")

        mgr, mock_pm = _manager_with_bytes(tmp_path, csv_bytes)
        result = mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert result is not None
        mock_cls.assert_called_once()


class TestJSONFormat:

    @patch("views_faoapi.managers.dataset_service.ForecastDataset", side_effect=_mock_dataset)
    def test_json_parsed(self, mock_cls, tmp_path):
        df = _make_test_df().reset_index()
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, np.ndarray)).any():
                df[col] = df[col].apply(lambda x: x.tolist() if isinstance(x, np.ndarray) else x)
        json_bytes = df.to_json().encode("utf-8")

        mgr, mock_pm = _manager_with_bytes(tmp_path, json_bytes)
        result = mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert result is not None


class TestFeatherFormat:

    @patch("views_faoapi.managers.dataset_service.ForecastDataset", side_effect=_mock_dataset)
    def test_feather_parsed(self, mock_cls, tmp_path):
        df = _make_test_df().reset_index()
        for col in df.columns:
            if df[col].apply(lambda x: isinstance(x, np.ndarray)).any():
                df[col] = df[col].apply(lambda x: x.tolist() if isinstance(x, np.ndarray) else x)
        buf = io.BytesIO()
        feather.write_feather(df, buf)

        mgr, mock_pm = _manager_with_bytes(tmp_path, buf.getvalue())
        result = mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert result is not None


# ============================================================
# Cascade behavior
# ============================================================


class TestCascadeBehavior:

    def test_garbage_bytes_fail_all_formats(self, tmp_path):
        """Garbage bytes fail every format and surface diagnostic error messages."""
        garbage = b"\x00\x01\x02\xff\xfe garbage that matches no format"
        mgr, mock_pm = _manager_with_bytes(tmp_path, garbage)

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert exc_info.value.status_code == 500
        detail = exc_info.value.detail
        assert "Failed to parse file" in detail
        for fmt in ["Parquet:", "CSV (utf-8):", "JSON:", "Feather:"]:
            assert fmt in detail, f"Missing format diagnostic: {fmt}"

    @patch("views_faoapi.managers.dataset_service.ForecastDataset", side_effect=_mock_dataset)
    def test_non_utf8_csv_falls_through_to_later_formats(self, mock_cls, tmp_path):
        """Non-UTF-8 bytes that aren't valid parquet should fail CSV utf-8
        and fall through to JSON/pickle/feather instead of silently
        'succeeding' as latin-1 CSV garbage."""
        non_utf8 = b"\x80\x81\x82\xff" * 100
        mgr, mock_pm = _manager_with_bytes(tmp_path, non_utf8)

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert exc_info.value.status_code == 500
        detail = exc_info.value.detail
        assert "CSV (utf-8):" in detail

    def test_no_files_returns_404(self, tmp_path):
        mgr = FAOApiManager.from_config(
            {"deployment": {"host": "0.0.0.0", "port": 80}},
            cache_dir=tmp_path / "cache",
        )
        mgr._prediction_bucket_id = "test-bucket"

        mock_pm = MagicMock()
        mock_pm.get_latest_file_id.return_value = None
        mock_pm.get_latest_provenance.return_value = None  # no artifact resolves (C-86)

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert exc_info.value.status_code == 404

    def test_download_failure_returns_500(self, tmp_path):
        mgr = FAOApiManager.from_config(
            {"deployment": {"host": "0.0.0.0", "port": 80}},
            cache_dir=tmp_path / "cache",
        )
        mgr._prediction_bucket_id = "test-bucket"

        mock_pm = MagicMock()
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="file_001", source="test", created_at="2025-10-22T12:00:00.000Z"
        )
        download_result = MagicMock()
        download_result.success = False
        download_result.error = "Network error"
        mock_pm.download_prediction.return_value = download_result

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert exc_info.value.status_code == 500
        assert "Failed to download" in exc_info.value.detail

    def test_empty_download_returns_500(self, tmp_path):
        mgr = FAOApiManager.from_config(
            {"deployment": {"host": "0.0.0.0", "port": 80}},
            cache_dir=tmp_path / "cache",
        )
        mgr._prediction_bucket_id = "test-bucket"

        mock_pm = MagicMock()
        mock_pm.get_latest_provenance.return_value = PredictionProvenance(
            file_id="file_001", source="test", created_at="2025-10-22T12:00:00.000Z"
        )
        download_result = MagicMock()
        download_result.success = True
        download_result.data = {"file_bytes": None}
        mock_pm.download_prediction.return_value = download_result

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert exc_info.value.status_code == 500
        assert "empty" in exc_info.value.detail.lower()

    def test_cache_returns_independent_copies(self, tmp_path):
        """Mutating a returned DataFrame must not contaminate the cache."""
        df = _make_test_df()
        buf = io.BytesIO()
        df.to_parquet(buf)

        mgr, mock_pm = _manager_with_bytes(tmp_path, buf.getvalue())
        first = mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        # Mutate the returned container in place (post-S4d a prediction `.dataframe` has no
        # sample columns, so add a probe column rather than drop one).
        first["_probe"] = 0

        second = mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert "_probe" not in second.columns, "Cache was contaminated by in-place mutation"


# ============================================================
# Parquet magic-bytes guard (C-52)
# ============================================================


class TestParquetMagicBytesGuard:

    def test_corrupted_parquet_magic_valid_body_invalid(self, tmp_path):
        """Bytes starting with PAR1 but with corrupted body must raise,
        not silently fall through to CSV/JSON and produce garbage."""
        corrupted = b"PAR1" + b"\x00\xff\xde\xad" * 50
        mgr, mock_pm = _manager_with_bytes(tmp_path, corrupted)

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert exc_info.value.status_code == 500
        assert "parquet header" in exc_info.value.detail.lower()

    def test_parquet_magic_bytes_guard_does_not_cascade(self, tmp_path):
        """When parquet header is present but parse fails, the error must NOT
        contain diagnostics from CSV/JSON/pickle/feather attempts."""
        corrupted = b"PAR1" + bytes(range(256)) * 2
        mgr, mock_pm = _manager_with_bytes(tmp_path, corrupted)

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        detail = exc_info.value.detail
        for fmt in ["CSV", "JSON", "Pickle", "Feather"]:
            assert fmt not in detail, f"Should not have tried {fmt} after parquet magic-bytes match"

    def test_valid_parquet_still_works(self, tmp_path):
        """Guard must not interfere with valid parquet files."""
        df = _make_test_df()
        buf = io.BytesIO()
        df.to_parquet(buf)
        raw = buf.getvalue()
        assert raw[:4] == b"PAR1"

        mgr, mock_pm = _manager_with_bytes(tmp_path, raw)
        result = mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert result is not None
        assert result.shape[0] == 8

    def test_non_parquet_bytes_still_cascade(self, tmp_path):
        """Bytes without PAR1 header should still try all formats."""
        garbage = b"\x00\x01\x02\xff\xfe not parquet at all"
        mgr, mock_pm = _manager_with_bytes(tmp_path, garbage)

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        detail = exc_info.value.detail
        for fmt in ["Parquet:", "CSV (utf-8):", "JSON:", "Feather:"]:
            assert fmt in detail, f"Missing format diagnostic: {fmt}"

    def test_empty_bytes_raises(self, tmp_path):
        """Empty file_bytes after download must raise, not silently produce empty DataFrame."""
        mgr, mock_pm = _manager_with_bytes(tmp_path, b"")
        mock_pm.get_latest_file_id.return_value = "file_empty"
        mock_pm.download_prediction.return_value.data = {"file_bytes": b""}

        with pytest.raises(HTTPException) as exc_info:
            mgr._get_latest_dataframe(mock_pm, "test-key", "historical")
        assert exc_info.value.status_code == 500
