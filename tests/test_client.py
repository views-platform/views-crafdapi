from unittest.mock import patch, MagicMock

import pandas as pd
import pytest
import requests

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
    def test_ids_read_as_floats_are_sent_as_whole_numbers(self, mock_get, client):
        """GAUL codes read from a file often arrive as 1301.0; the API takes 1301."""
        mock_get.return_value = _mock_response(json_data=self._BODY)
        client.slice("admin1", "sb", places=[1301.0, -3727.0])
        assert mock_get.call_args[1]["params"] == {"places": "1301,-3727"}

    @pytest.mark.parametrize("option", [
        {"places": "161,160"}, {"months": "2027-01"}, {"columns": "sb_map"},
    ])
    def test_a_string_where_a_list_belongs_is_refused(self, client, option):
        """A string would be split into single characters."""
        with pytest.raises(TypeError, match="lists"):
            client.slice("country", "sb", **option)

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


class _Download:
    """Stands in for a streamed ``requests.get`` answer."""

    def __init__(self, status=200, body=b"", made_in="2026-11", breaks=False, detail="no such file"):
        self.status_code, self.ok = status, status == 200
        self.headers = {"X-Forecast-Made-In": made_in} if made_in else {}
        self._body, self._breaks, self.text = body, breaks, detail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def json(self):
        return {"detail": self.text}

    def iter_content(self, chunk_size):
        yield self._body[:3]
        if self._breaks:
            raise requests.exceptions.ChunkedEncodingError("cut off")
        yield self._body[3:]


def _parquet_bytes():
    table = pd.DataFrame({"month": ["2026-11", "2026-12"], "country_code": [1, 1], "sb_mean": [2.0, 3.0]})
    return table.to_parquet()


class TestDownloadBulk:
    @patch("views_crafdapi.client.requests.get")
    def test_saves_the_file_and_returns_the_month(self, mock_get, client, tmp_path):
        mock_get.return_value = _Download(body=b"abcdef")
        target = tmp_path / "country_sb.parquet"
        assert client.download_bulk_to("country", "sb", target) == "2026-11"
        assert target.read_bytes() == b"abcdef" and list(tmp_path.iterdir()) == [target]
        assert mock_get.call_args.args == ("http://localhost:8080/bulk/country/sb",)
        assert mock_get.call_args.kwargs["headers"] == {"X-API-Key": "test-api-key"}

    @pytest.mark.parametrize("status", [502, 503, 504])
    @patch("views_crafdapi.client.requests.get")
    def test_a_busy_server_is_tried_again(self, mock_get, client, tmp_path, status):
        mock_get.side_effect = [_Download(status), _Download(status), _Download(body=b"abcdef")]
        assert client.download_bulk_to("grid", "ns", tmp_path / "f", wait_seconds=0) == "2026-11"
        assert (tmp_path / "f").read_bytes() == b"abcdef"

    @patch("views_crafdapi.client.time.sleep")
    @patch("views_crafdapi.client.requests.get")
    def test_the_wait_between_tries_and_the_request_made(self, mock_get, mock_sleep, client, tmp_path):
        # Busy once, then no answer at all, then the file: two waits of the default minute.
        mock_get.side_effect = [_Download(503), requests.exceptions.ConnectionError("refused"),
                                _Download(body=b"abcdef")]
        client.download_bulk_to("grid", "sb", tmp_path / "f")
        assert [call.args for call in mock_sleep.call_args_list] == [(60,), (60,)]
        assert mock_get.call_args.kwargs["stream"] is True and mock_get.call_args.kwargs["timeout"] == 600

        mock_get.side_effect = requests.exceptions.Timeout("slow")
        with pytest.raises(requests.exceptions.Timeout):
            client.download_bulk_to("grid", "sb", tmp_path / "g", wait_seconds=0)
        assert mock_get.call_count == 6   # three more tries, then the error

    @patch("views_crafdapi.client.requests.get")
    def test_a_server_busy_three_times_is_an_error(self, mock_get, client, tmp_path):
        mock_get.side_effect = [_Download(503, detail="loading")] * 3
        with pytest.raises(RuntimeError, match="API error 503 on /bulk/grid/sb: loading"):
            client.download_bulk_to("grid", "sb", tmp_path / "f", wait_seconds=0)
        assert list(tmp_path.iterdir()) == []

    @patch("views_crafdapi.client.requests.get")
    def test_a_refusal_is_not_tried_again(self, mock_get, client, tmp_path):
        mock_get.return_value = _Download(401, detail="Invalid API key")
        with pytest.raises(RuntimeError, match="API error 401 on /bulk/grid/sb: Invalid API key"):
            client.download_bulk_to("grid", "sb", tmp_path / "f", wait_seconds=0)
        assert mock_get.call_count == 1 and list(tmp_path.iterdir()) == []

    @patch("views_crafdapi.client.requests.get")
    def test_a_download_cut_off_is_tried_again_and_leaves_no_partial_file(self, mock_get, client, tmp_path):
        mock_get.side_effect = [_Download(body=b"abcdef", breaks=True), _Download(body=b"abcdef")]
        client.download_bulk_to("grid", "sb", tmp_path / "f", wait_seconds=0)
        assert [p.name for p in tmp_path.iterdir()] == ["f"] and (tmp_path / "f").read_bytes() == b"abcdef"

        mock_get.side_effect = [_Download(body=b"abcdef", breaks=True)] * 3
        with pytest.raises(requests.exceptions.ChunkedEncodingError):
            client.download_bulk_to("grid", "sb", tmp_path / "g", wait_seconds=0)
        assert [p.name for p in tmp_path.iterdir()] == ["f"]   # no g, no g.part

    @patch("views_crafdapi.client.requests.get")
    def test_an_answer_without_the_forecast_month_is_refused(self, mock_get, client, tmp_path):
        mock_get.return_value = _Download(body=b"abcdef", made_in=None)
        with pytest.raises(RuntimeError, match="did not say which month"):
            client.download_bulk_to("grid", "sb", tmp_path / "f")
        assert list(tmp_path.iterdir()) == []

    @patch("views_crafdapi.client.requests.get")
    def test_the_table_with_months_and_columns_chosen(self, mock_get, client, tmp_path, monkeypatch):
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
        mock_get.return_value = _Download(body=_parquet_bytes())
        table, made_in = client.download_bulk("country", "sb")
        assert made_in == "2026-11" and table.shape == (2, 3)
        table, _ = client.download_bulk("country", "sb", ["2026-12"], ["month", "sb_mean"])
        assert table.to_dict("list") == {"month": ["2026-12"], "sb_mean": [3.0]}
        assert list(tmp_path.iterdir()) == []   # the temporary copy is gone
