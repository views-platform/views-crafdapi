from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from views_crafdapi.client import CrafdApiClient


@pytest.fixture
def client():
    return CrafdApiClient("http://localhost:8080", "test-api-key")


def _mock_response(status_code: int = 200, json_data=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


class TestExtractDetail:
    def test_json_detail(self):
        resp = _mock_response(json_data={"detail": "Not found"})
        assert CrafdApiClient._extract_detail(resp) == "Not found"

    def test_json_no_detail(self):
        resp = _mock_response(json_data={"error": "oops"}, text="raw body")
        assert CrafdApiClient._extract_detail(resp) == "raw body"

    def test_malformed_json(self):
        resp = MagicMock()
        resp.json.side_effect = ValueError("bad json")
        resp.text = "not json at all"
        assert CrafdApiClient._extract_detail(resp) == "not json at all"

    def test_truncates_long_text(self):
        resp = MagicMock()
        resp.json.side_effect = ValueError()
        resp.text = "x" * 500
        result = CrafdApiClient._extract_detail(resp)
        assert len(result) == 300


class TestHealth:
    @patch("views_crafdapi.client.requests.get")
    def test_success(self, mock_get, client):
        mock_get.return_value = _mock_response(
            json_data={"status": "ok", "appwrite_connected": True}
        )
        result = client.health()
        assert result["status"] == "ok"
        mock_get.assert_called_once_with(
            "http://localhost:8080/health",
            headers={"X-API-Key": "test-api-key"},
            timeout=30.0,
        )

    @patch("views_crafdapi.client.requests.get")
    def test_error_raises(self, mock_get, client):
        mock_get.return_value = _mock_response(
            status_code=503, json_data={"detail": "Service unavailable"}
        )
        with pytest.raises(RuntimeError, match="503"):
            client.health()


class TestFetchSubset:
    @patch("views_crafdapi.client.requests.get")
    def test_historical_default(self, mock_get, client):
        mock_get.return_value = _mock_response(
            json_data={"data": {"dataframe": {"col_a": [1, 2], "col_b": [3, 4]}}}
        )
        df = client.fetch_subset("country", [540], "NGA")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        call_url = mock_get.call_args[0][0]
        assert "historical" in call_url

    @patch("views_crafdapi.client.requests.get")
    def test_forecast_type(self, mock_get, client):
        mock_get.return_value = _mock_response(
            json_data={"data": {"dataframe": {"col_a": [1]}}}
        )
        client.fetch_subset("country", [540], "NGA", data_type="forecast")
        call_url = mock_get.call_args[0][0]
        assert "forecast" in call_url
        assert "historical" not in call_url

    def test_invalid_data_type(self, client):
        with pytest.raises(ValueError, match="data_type must be"):
            client.fetch_subset("country", [540], "NGA", data_type="invalid")

    @patch("views_crafdapi.client.requests.get")
    def test_entity_ids_list(self, mock_get, client):
        mock_get.return_value = _mock_response(
            json_data={"data": {"dataframe": {"a": [1]}}}
        )
        client.fetch_subset("gaul1", [540], [100, 200])
        params = mock_get.call_args[1]["params"]
        assert params["entity_ids"] == "100,200"

    @patch("views_crafdapi.client.requests.get")
    def test_entity_ids_string(self, mock_get, client):
        mock_get.return_value = _mock_response(
            json_data={"data": {"dataframe": {"a": [1]}}}
        )
        client.fetch_subset("country", [540], "NGA")
        params = mock_get.call_args[1]["params"]
        assert params["entity_ids"] == "NGA"

    @patch("views_crafdapi.client.requests.get")
    def test_entity_ids_none_omits_filter(self, mock_get, client):
        """entity_ids=None (the default) must omit the param entirely so the server returns ALL
        entities for the month — the global footprint the coverage notebooks rely on."""
        mock_get.return_value = _mock_response(
            json_data={"data": {"dataframe": {"a": [1]}}}
        )
        client.fetch_subset("pg", [559], None, data_type="forecast")
        params = mock_get.call_args[1]["params"]
        assert "entity_ids" not in params  # no filter → all cells
        client.fetch_subset("pg", [559])  # default is None
        assert "entity_ids" not in mock_get.call_args[1]["params"]

    @patch("views_crafdapi.client.requests.get")
    def test_features_param(self, mock_get, client):
        mock_get.return_value = _mock_response(
            json_data={"data": {"dataframe": {"a": [1]}}}
        )
        client.fetch_subset("country", [540], "NGA", features=["lr_ged_sb", "lr_ged_ns"])
        params = mock_get.call_args[1]["params"]
        assert params["features"] == "lr_ged_sb,lr_ged_ns"

    @patch("views_crafdapi.client.requests.get")
    def test_error_raises(self, mock_get, client):
        mock_get.return_value = _mock_response(
            status_code=404, json_data={"detail": "Not found"}
        )
        with pytest.raises(RuntimeError, match="404"):
            client.fetch_subset("country", [540], "NGA")

    @patch("views_crafdapi.client.requests.get")
    def test_api_key_header(self, mock_get, client):
        mock_get.return_value = _mock_response(
            json_data={"data": {"dataframe": {"a": [1]}}}
        )
        client.fetch_subset("country", [540], "NGA")
        headers = mock_get.call_args[1]["headers"]
        assert headers["X-API-Key"] == "test-api-key"


class TestHdiMap:
    @patch("views_crafdapi.client.requests.get")
    def test_returns_dataframe_of_records(self, mock_get, client):
        mock_get.return_value = _mock_response(json_data={"data": {"hdi_map": [
            {"country_iso_a3": "NGA", "sb_map": 3.0, "sb_hdi90_lower": 0.0, "sb_hdi90_upper": 9.0},
            {"country_iso_a3": "SOM", "sb_map": 1.0, "sb_hdi90_lower": 0.0, "sb_hdi90_upper": 4.0},
        ]}})
        df = client.hdi_map("country", [559], "NGA")
        assert isinstance(df, pd.DataFrame) and len(df) == 2
        assert {"sb_map", "sb_hdi90_lower", "sb_hdi90_upper"} <= set(df.columns)

    @patch("views_crafdapi.client.requests.get")
    def test_builds_forecast_endpoint_and_params(self, mock_get, client):
        mock_get.return_value = _mock_response(json_data={"data": {"hdi_map": []}})
        client.hdi_map("country", [559], "NGA", aggregate=True, alpha=0.95,
                       features=["pred_lr_ged_sb"])
        url = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        assert url.endswith("country/analysis/forecast/hdi-map")
        assert params["alpha"] == 0.95 and params["aggregate"] is True
        assert params["entity_ids"] == "NGA" and params["features"] == "pred_lr_ged_sb"

    @patch("views_crafdapi.client.requests.get")
    def test_time_ids_and_entity_list_serialized(self, mock_get, client):
        mock_get.return_value = _mock_response(json_data={"data": {"hdi_map": []}})
        client.hdi_map("gaul1", [559, 560], [100, 200], data_type="forecast")
        params = mock_get.call_args[1]["params"]
        assert params["time_ids"] == "559,560" and params["entity_ids"] == "100,200"

    def test_invalid_data_type(self, client):
        with pytest.raises(ValueError, match="data_type must be"):
            client.hdi_map("country", [559], "NGA", data_type="nope")

    @patch("views_crafdapi.client.requests.get")
    def test_error_raises(self, mock_get, client):
        mock_get.return_value = _mock_response(status_code=500, json_data={"detail": "boom"})
        with pytest.raises(RuntimeError, match="500"):
            client.hdi_map("country", [559], "NGA")


class TestProvenance:
    @patch("views_crafdapi.client.requests.get")
    def test_returns_data_record(self, mock_get, client):
        mock_get.return_value = _mock_response(json_data={"success": True, "data": {
            "run_id": "rusty_bucket_forecasting_20260727", "created_at": "2026-07-27T09:53:55Z",
            "mode": "wire", "methodology_version": "3.0.0"}})
        prov = client.provenance("forecast")
        assert prov["run_id"].startswith("rusty_bucket") and prov["mode"] == "wire"
        assert mock_get.call_args[0][0].endswith("/provenance/forecast")

    def test_invalid_category(self, client):
        with pytest.raises(ValueError, match="category must be"):
            client.provenance("nope")

    @patch("views_crafdapi.client.requests.get")
    def test_error_raises(self, mock_get, client):
        mock_get.return_value = _mock_response(status_code=404, json_data={"detail": "none"})
        with pytest.raises(RuntimeError, match="404"):
            client.provenance("historical")


class TestPackageExport:
    def test_faoapiclient_is_top_level_export(self):
        import views_crafdapi
        from views_crafdapi import CrafdApiClient as Exported
        assert Exported is CrafdApiClient
        assert "CrafdApiClient" in views_crafdapi.__all__


class TestInit:
    def test_trailing_slash_stripped(self):
        c = CrafdApiClient("http://example.com/api/", "key")
        assert c._base_url == "http://example.com/api"

    def test_default_timeout(self):
        c = CrafdApiClient("http://example.com", "key")
        assert c._timeout == 30.0

    def test_custom_timeout(self):
        c = CrafdApiClient("http://example.com", "key", timeout=60.0)
        assert c._timeout == 60.0


class TestTimeout:
    @patch("views_crafdapi.client.requests.get")
    def test_health_passes_timeout(self, mock_get):
        mock_get.return_value = _mock_response(json_data={"status": "ok"})
        c = CrafdApiClient("http://localhost", "key", timeout=10.0)
        c.health()
        assert mock_get.call_args[1]["timeout"] == 10.0

    @patch("views_crafdapi.client.requests.get")
    def test_fetch_passes_timeout(self, mock_get):
        mock_get.return_value = _mock_response(
            json_data={"data": {"dataframe": {"a": [1]}}}
        )
        c = CrafdApiClient("http://localhost", "key", timeout=15.0)
        c.fetch_subset("country", [540], "NGA")
        assert mock_get.call_args[1]["timeout"] == 15.0
