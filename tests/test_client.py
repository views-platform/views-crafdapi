from unittest.mock import patch, MagicMock

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


class TestSlice:
    _BODY = {
        "level": "country", "outcome": "sb",
        "columns": ["country_code", "country_name", "country_iso3", "month", "sb_map"],
        "rows": [{"country_code": 161, "country_name": "Sudan", "country_iso3": "SDN",
                  "month": "2027-01", "sb_map": 12.0}],
    }

    @patch("views_crafdapi.client.requests.get")
    def test_all_options_left_out(self, mock_get, client):
        mock_get.return_value = _mock_response(json_data=self._BODY)
        df = client.slice("country", "sb")
        assert mock_get.call_args[0][0] == "http://localhost:8080/slice/country/sb"
        assert mock_get.call_args[1]["params"] == {}
        assert list(df.columns) == self._BODY["columns"]
        assert df.loc[0, "sb_map"] == 12.0

    @patch("views_crafdapi.client.requests.get")
    def test_options_are_comma_separated(self, mock_get, client):
        mock_get.return_value = _mock_response(json_data=self._BODY)
        client.slice("country", "sb", places=[161, 160], months=["2027-01", "2027-02"],
                     columns=["sb_map"])
        assert mock_get.call_args[1]["params"] == {
            "places": "161,160", "months": "2027-01,2027-02", "columns": "sb_map",
        }

    @patch("views_crafdapi.client.requests.get")
    def test_an_empty_slice_keeps_its_columns(self, mock_get, client):
        mock_get.return_value = _mock_response(json_data={**self._BODY, "rows": []})
        df = client.slice("country", "sb")
        assert df.empty and list(df.columns) == self._BODY["columns"]

    @patch("views_crafdapi.client.requests.get")
    def test_error_raises_with_the_detail(self, mock_get, client):
        mock_get.return_value = _mock_response(status_code=422, json_data={"detail": "Months must be written"})
        with pytest.raises(RuntimeError, match="422.*Months must be written"):
            client.slice("country", "sb", months=["2027-1"])


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
    def test_slice_passes_timeout(self, mock_get):
        mock_get.return_value = _mock_response(json_data={"columns": ["a"], "rows": [{"a": 1}]})
        c = CrafdApiClient("http://localhost", "key", timeout=15.0)
        c.slice("country", "sb")
        assert mock_get.call_args[1]["timeout"] == 15.0
