"""Endpoint smoke tests via FastAPI TestClient (C-06)."""

import io
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from tests.conftest import make_fao_df, start_loader
from views_crafdapi.managers.api import CrafdApiManager
from views_crafdapi.managers.appwrite import OperationResult
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.managers.serialization import estimate_records_bytes

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
    # The history reaches three months back and overlaps the forecast in its first month
    # (600), as the real one does: forecast 600-601 (2029-12, 2030-01), history 598-600.
    history = ForecastDataset(_relabel_months(
        make_fao_df(n_months=3, seed=3, targets=("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")),
        [598, 599, 600],
    ))
    # The store's file ids, as loading stamps them; the bulk files are named by them (#203).
    dataset.source_file_id, history.source_file_id = "file_002", "file_001"
    api_key_hash = mgr._get_api_key_hash("test-api-key")
    mgr._dataframe_cache[api_key_hash] = {
        "historical": {
            "data": history.dataframe,
            "file_id": "file_001",
            "timestamp": time.time(),
            "dataset": history,
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

    # ADR-044: the loader reads the store with the server's key; here the stand-in store and the
    # warm entries above, under the same key. Loaded before the first request, as at start.
    start_loader(mgr, "test-api-key", mock_pm)

    client = TestClient(mgr.app)
    return client, mgr, mock_pm


HEADERS = {"X-API-Key": "test-api-key"}


def _relabel_months(df, months):
    """The same rows, with the month ids replaced in order."""
    import pandas as pd

    cells = df.index.get_level_values("priogrid_id")
    old = df.index.get_level_values("month_id")
    mapping = dict(zip(sorted(set(old)), months))
    df = df.copy()
    df.index = pd.MultiIndex.from_arrays([[mapping[m] for m in old], cells], names=["month_id", "priogrid_id"])
    return df


# ============================================================
# Static endpoints
# ============================================================


# FastAPI adds these pages itself; they are not part of the service.
_FASTAPI_OWN_PAGES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _named_routes(text):
    """Every path a message names (anything starting with `/`), without trailing punctuation."""
    import re

    return [m.rstrip(".,;:") for m in re.findall(r"(?<![\w])/[\w{}./-]+", text)]


class TestTheRouteList:
    """The service offers exactly these routes (#207, docs/SERVICE_SPECIFICATION.md). A new route
    changes this list on purpose; it never appears by accident. One slice route serves the
    specification's three levels and every outcome (#204, #206)."""

    _EXPECTED = {
        ("GET", path)
        for path in {"/ping", "/version", "/health", "/bulk/{level}/{outcome}", "/slice/{level}/{outcome}"}
    }

    def test_exactly_these_routes_are_offered(self, app_client):
        """Every route of any kind, with its methods. Only FastAPI's own documentation pages are
        left out, and they are named, so nothing else can hide among them."""
        _, mgr, _ = app_client
        offered = {
            (method, route.path)
            for route in mgr.app.routes
            if route.path not in _FASTAPI_OWN_PAGES
            for method in (getattr(route, "methods", None) or {"ANY"}) - {"HEAD"}
        }
        assert offered == self._EXPECTED

    def test_each_route_takes_exactly_its_options(self, app_client):
        """The query options each route reads, from the service's own OpenAPI description. The
        slice has the specification's options (the outcome is in the path); nothing can force
        a reload (#206)."""
        _, mgr, _ = app_client
        paths = mgr.app.openapi()["paths"]
        options = {
            path: sorted(p["name"] for p in spec["get"].get("parameters", []) if p["in"] == "query")
            for path, spec in paths.items()
        }
        assert options == {
            "/ping": [], "/version": [], "/health": [], "/bulk/{level}/{outcome}": [],
            "/slice/{level}/{outcome}": ["columns", "months", "places"],
        }

    @pytest.mark.parametrize("path", [
        "/", "/provenance/forecast", "/provenance/historical",
        "/files/test-bucket", "/files/test-bucket/file_001/info",
        "/files/test-bucket/file_001/download",
        "/gaul0/data/forecast/subset", "/gaul2/analysis/historical/hdi-map",
        # The sample and per-request summary routes the slice replaced (#204, #206).
        "/pg/data/forecast/subset", "/pg/data/historical/subset",
        "/country/analysis/forecast/hdi-map", "/gaul1/analysis/historical/hdi-map",
        # The admin-1, all-outcomes bulk file the nine bulk files replaced (#203).
        "/data/forecast/bulk",
    ])
    def test_a_removed_route_answers_404(self, app_client, path):
        client, _, _ = app_client
        assert client.get(path, headers=HEADERS).status_code == 404


class TestHealthEndpoint:

    def test_health_names_the_served_run(self, app_client):
        """#262: a check from outside confirms which forecast run partners get."""
        client, mgr, _ = app_client
        _serve(mgr, provenance={"run_id": "chrome_box_forecasting_20261112_210150",
                                "created_at": datetime.now(timezone.utc).isoformat()})
        assert client.get("/health", headers=HEADERS).json()["served_run"] == \
            "chrome_box_forecasting_20261112_210150"

    def test_health_omits_the_served_run_when_nothing_is_loaded(self, app_client):
        client, mgr, _ = app_client
        mgr._loader._served = None
        assert "served_run" not in client.get("/health", headers=HEADERS).json()

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
        _serve(mgr, provenance={"mode": "wire", "source": "rusty_bucket", "created_at": fresh})
        _serve(mgr, serving_state={"degraded": False})
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
        _serve(mgr, serving_state={
            "degraded": True, "reason": "ingest_failed", "fallback_available": True,
            "serving": "last_good_manifested", "file_id": "mani_good", "age_days": 5.0,
            "sla_days": 45.0,
        })
        resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["forecast_serving_state"]["degraded"] is True
        assert body["forecast_serving_state"]["fallback_available"] is True


class TestHealthAfterAnUnverifiedServe:

    def test_a_past_unverified_serve_does_not_page_once_the_store_answers_again(self, app_client):
        """One lookup failure leaves `cached_unverified` on the worker until its next forecast
        request. If /health's own store probe succeeds, the store is back: that flag describes a
        past response, not the service, and must not keep /health degraded (#158 review)."""
        client, mgr, _ = app_client
        self._unverified(mgr)
        body = client.get("/health", headers=HEADERS).json()
        assert body["status"] == "healthy"
        assert body["forecast_serving_state"]["serving"] == "cached_unverified"   # still visible

    def test_an_unverified_serve_still_degrades_health_while_the_store_is_unreachable(self, app_client):
        client, mgr, _ = app_client
        self._unverified(mgr)
        mgr.app.dependency_overrides[mgr._get_appwrite_manager]().list_buckets.return_value = (
            MagicMock(success=False)
        )
        body = client.get("/health", headers=HEADERS).json()
        assert body["status"] == "degraded"

    @staticmethod
    def _unverified(mgr):
        # A fresh served run, so freshness cannot be what decides the status.
        _serve(mgr, provenance={
            "file_id": "wire_run_9", "mode": "wire", "source": "rusty_bucket",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        _serve(mgr, serving_state={
            "degraded": True, "reason": "manifest_lookup_error",
            "fallback_available": True, "serving": "cached_unverified",
        })


class TestHealthDegradesWhenItCannotVouchForTheData:
    """#248: the Better Stack monitor alarms only when /health is not healthy. Two cases the old
    GitHub job caught must therefore degrade /health itself."""

    def test_an_unmeasurable_forecast_age_degrades(self, app_client):
        client, mgr, mock_pm = app_client
        _serve(mgr, provenance={"created_at": "not a date"})
        resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["forecast_freshness"]["is_stale"] is None
        assert resp.json()["status"] == "degraded"

    @pytest.mark.parametrize("status", [500, None])   # None: the store could not be reached
    def test_a_store_error_other_than_a_refused_key_degrades(self, app_client, status):
        client, mgr, _ = app_client
        # A measurable, fresh age, so the store error is the only reason left to degrade.
        fresh = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        _serve(mgr, provenance={"created_at": fresh})
        appwrite = mgr.app.dependency_overrides[mgr._get_appwrite_manager]()
        appwrite.list_buckets.return_value = SimpleNamespace(success=False, status=status)
        resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 200
        assert resp.json()["appwrite_connected"] is False
        assert resp.json()["forecast_freshness"]["is_stale"] is False
        assert resp.json()["status"] == "degraded"

    def test_no_date_anywhere_degrades(self, app_client):
        """Not on the served run, no manifest, no provenance record: the age is unknown."""
        client, mgr, mock_pm = app_client
        _serve(mgr, provenance=None)
        mock_pm.get_latest_manifest.return_value = None
        mock_pm.get_latest_provenance.return_value = None
        body = client.get("/health", headers=HEADERS).json()
        assert body["forecast_freshness"]["created_at"] is None
        assert body["forecast_freshness"]["is_stale"] is None
        assert body["status"] == "degraded"

    def test_a_measured_fresh_age_and_a_working_store_stay_healthy(self, app_client):
        client, mgr, _ = app_client
        fresh = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        _serve(mgr, provenance={"created_at": fresh})
        body = client.get("/health", headers=HEADERS).json()
        assert body["appwrite_connected"] is True and body["forecast_freshness"]["is_stale"] is False
        assert body["status"] == "healthy"


class TestHealthCarriesNoStoreText:
    """ADR-033 §6a rule 6 / C-264: no response carries the store's own error text — /health
    included, which had put `str(e)` in its freshness error and its 503 detail (#160 review)."""

    def test_the_freshness_error_is_generic(self, app_client):
        from views_crafdapi.managers.prediction import MetadataLookupError

        client, mgr, mock_pm = app_client
        _serve(mgr, provenance=None)
        mock_pm.get_latest_manifest.return_value = None
        mock_pm.get_latest_provenance.side_effect = MetadataLookupError(
            "storage_secret_detail", status=503)
        resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 200
        assert "storage_secret_detail" not in resp.text
        assert resp.json()["forecast_freshness"] == {"error": "freshness check failed"}
        assert resp.json()["status"] == "degraded"   # #248: an unmeasured age is not healthy

    def test_the_unhealthy_detail_is_generic(self, app_client, caplog):
        client, mgr, _ = app_client
        appwrite = mgr.app.dependency_overrides[mgr._get_appwrite_manager]()
        appwrite.list_buckets.side_effect = RuntimeError("storage_secret_detail")
        with caplog.at_level("ERROR"):
            resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Service unhealthy"   # fixed: not even the error's type
        # The cause goes to the log, with where it happened.
        logged = [r for r in caplog.records if r.getMessage() == "Health check failed"]
        assert logged and logged[0].exc_info is not None


class TestRemovedCacheRoutes:
    """C-269 (2026-11-11): the file cache is shared by every key. `/cached` served it with no
    store check, so a revoked key, or a key from another audience, could read what someone
    else's request had cached; `/cache/stats` and `DELETE /cache` read and emptied every
    caller's cache. The routes are gone, for every key."""

    _REMOVED = {
        ("GET", "/files/{bucket_id}/{file_id}/cached"),
        ("GET", "/cache/stats"),
        ("DELETE", "/cache"),
    }

    def test_routes_are_not_registered(self, app_client):
        _, mgr, _ = app_client
        registered = {
            (method, route.path)
            for route in mgr.app.routes
            for method in getattr(route, "methods", ())
        }
        assert not self._REMOVED & registered

    def test_no_route_exposes_the_shared_cache_under_any_name(self, app_client):
        # The property, not the three old paths: a renamed or re-methoded cache route
        # (`/cache/statistics`, `POST /cache`, `/files/{b}/{f}/cache`) is the same defect.
        _, mgr, _ = app_client
        assert [r.path for r in mgr.app.routes if "cache" in r.path.lower()] == []

    def test_no_handler_reads_or_empties_the_shared_file_cache(self):
        import inspect

        import views_crafdapi.managers.api as api

        source = inspect.getsource(api)
        for call in ("cache_manager.get_stats", "cache_manager.clear_cache",
                     "get_cached_file_path", ".get_cache_stats(", ".clear_cache("):
            assert call not in source, call

    @pytest.mark.parametrize("method,path", [
        pytest.param("GET", "/files/test-bucket/file_001/cached", id="cached"),
        pytest.param("GET", "/cache/stats", id="cache-stats"),
        pytest.param("DELETE", "/cache", id="clear-cache"),
    ])
    def test_route_answers_nothing(self, app_client, method, path):
        client, _, _ = app_client
        assert client.request(method, path, headers=HEADERS).status_code in (404, 405)

    def test_health_does_not_report_the_shared_cache(self, app_client):
        client, mgr, _ = app_client
        sentinel = "shared-cache-sentinel-7c1"
        mock_appwrite = MagicMock()
        mock_appwrite.list_buckets.return_value = MagicMock(success=True)
        mock_appwrite.cache_manager.get_stats.return_value = {"cache_dir": sentinel}
        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: mock_appwrite
        resp = client.get("/health", headers=HEADERS)
        assert resp.status_code == 200
        assert sentinel not in resp.text
        assert not mock_appwrite.cache_manager.get_stats.called

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
        resp = client.get("/slice/grid/sb")
        assert resp.status_code == 422


# ============================================================
# The slice route (#204, #206): four options, summary columns, never samples
# ============================================================

_SLICE_LEVELS = ["grid", "admin1", "country"]
_IDENTITY = {   # the place, then the month written two ways (#205, #206)
    "grid": ["priogrid_id", "latitude", "longitude", "admin1_code", "admin1_name",
             "country_code", "country_name", "country_iso3", "month", "month_id"],
    "admin1": ["admin1_code", "admin1_name", "country_code", "country_name", "country_iso3",
               "month", "month_id"],
    "country": ["country_code", "country_name", "country_iso3", "month", "month_id"],
}
_SB_COLUMNS = [
    "sb_p_gt0", "sb_p_gt25", "sb_p_gt100", "sb_p_gt500", "sb_p_gt1000",
    "sb_mean", "sb_mean_if_any", "sb_median_if_any", "sb_severe_scenario",
    "sb_actual", "n_draws",
]


def _fixture_dataset(mgr, category="forecast"):
    """The loaded dataset the routes answer from (each request works on a copy of it)."""
    served = mgr._loader.served()
    return served.forecast if category == "forecast" else served.history


def _serve(mgr, **replace):
    """Hand the routes other data, as the loader does after a new delivery."""
    import dataclasses

    mgr._loader._served = dataclasses.replace(mgr._loader.served(), **replace)


class TestSliceRoute:
    """The partner guide's slice: `GET /slice/{level}/{outcome}?places=&months=&columns=`."""

    @pytest.mark.parametrize("level", _SLICE_LEVELS)
    def test_every_level_answers_with_its_identity_month_and_summary_columns(self, app_client, level):
        client, _, _ = app_client
        resp = client.get(f"/slice/{level}/sb", headers=HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["level"] == level and body["outcome"] == "sb"
        assert body["columns"] == _IDENTITY[level] + _SB_COLUMNS
        n_places = {"grid": 4, "admin1": 2, "country": 2}[level]
        assert len(body["rows"]) == n_places * 4   # two recorded months, two forecast months
        assert {(r["month"], r["month_id"]) for r in body["rows"]} == {
            ("2029-10", 598), ("2029-11", 599), ("2029-12", 600), ("2030-01", 601)}

    @pytest.mark.parametrize("outcome", ["sb", "ns", "os"])
    def test_every_outcome_has_its_own_columns_and_no_other(self, app_client, outcome):
        client, _, _ = app_client
        columns = client.get(f"/slice/admin1/{outcome}", headers=HEADERS).json()["columns"]
        values = [c for c in columns if c not in _IDENTITY["admin1"]]
        assert values == [c.replace("sb_", f"{outcome}_", 1) for c in _SB_COLUMNS]

    def test_the_values_are_the_summaries_of_the_summed_samples(self, app_client):
        """Admin-1 values come from the region's samples summed first, then summarised; the
        recorded deaths are the sum of the region's cells. Region 2 is cells 102 and 103."""
        client, mgr, _ = app_client
        expected = _fixture_dataset(mgr).summarize(
            features=["pred_lr_ged_sb"], level="gaul1", aggregate=True, with_metadata=False
        )
        rows = client.get(
            "/slice/admin1/sb", params={"places": "2", "months": "2029-11,2030-01"}, headers=HEADERS
        ).json()["rows"]
        past, ahead = rows
        assert ahead["admin1_code"] == 2 and ahead["admin1_name"] == "Region2"
        assert ahead["sb_mean"] == pytest.approx(expected.loc[(601, 2), "pred_lr_ged_sb_mean"])
        assert ahead["sb_p_gt0"] == pytest.approx(expected.loc[(601, 2), "pred_lr_ged_sb_p_gt0"])
        assert ahead["n_draws"] == expected.loc[(601, 2), "pred_lr_ged_sb_n_draws"] == 10
        history = _fixture_dataset(mgr, "historical")
        positions = [history.dataframe.index.get_loc((599, cell)) for cell in (102, 103)]
        assert past["sb_actual"] == pytest.approx(history._sample_array("pred_lr_ged_sb")[positions, 0].sum())
        assert past["sb_mean"] is None and past["n_draws"] is None and ahead["sb_actual"] is None

    def test_a_country_is_named_by_its_gaul_code(self, app_client):
        """Partners name countries by GAUL code, and the dataset sums them by it (#225). Country 20
        is cells 102 and 103; its ISO3, BBB, is a plain column."""
        client, mgr, _ = app_client
        expected = _fixture_dataset(mgr).summarize(
            features=["pred_lr_ged_sb"], level="country", aggregate=True, with_metadata=False
        )
        rows = client.get(
            "/slice/country/sb", params={"places": "20", "months": "2029-12"}, headers=HEADERS
        ).json()["rows"]
        assert [(r["country_code"], r["country_iso3"]) for r in rows] == [(20, "BBB")]
        assert rows[0]["sb_mean"] == pytest.approx(expected.loc[(600, 20), "pred_lr_ged_sb_mean"])
        assert rows[0]["sb_median_if_any"] == pytest.approx(
            expected.loc[(600, 20), "pred_lr_ged_sb_median_if_any"]
        )
        assert rows[0]["sb_severe_scenario"] is None   # 10 draws: below the 20 a severe case needs

    def test_places_and_months_narrow_the_rows(self, app_client):
        client, _, _ = app_client
        rows = client.get(
            "/slice/grid/sb", params={"places": "100,103", "months": "2029-12"}, headers=HEADERS
        ).json()["rows"]
        assert sorted((r["priogrid_id"], r["month"]) for r in rows) == [(100, "2029-12"), (103, "2029-12")]

    def test_a_column_list_returns_only_those_columns_and_always_the_names(self, app_client):
        client, _, _ = app_client
        body = client.get(
            "/slice/country/sb", params={"columns": "sb_p_gt100,sb_mean"}, headers=HEADERS
        ).json()
        # The summary columns keep their usual order, whatever order they were asked in.
        assert body["columns"] == _IDENTITY["country"] + ["sb_p_gt100", "sb_mean"]
        assert set(body["rows"][0]) == set(body["columns"])

    def test_no_sample_is_ever_served(self, app_client):
        """Partners never get the samples (specification). No value in any row is a list."""
        client, _, _ = app_client
        for level in _SLICE_LEVELS:
            for row in client.get(f"/slice/{level}/sb", headers=HEADERS).json()["rows"]:
                assert not any(isinstance(v, (list, dict)) for v in row.values()), row

    @pytest.mark.parametrize("path", ["/slice/pg/sb", "/slice/gaul1/sb", "/slice/admin2/sb", "/slice/grid/xx"])
    def test_an_unknown_level_or_outcome_is_404(self, app_client, path):
        client, _, _ = app_client
        resp = client.get(path, headers=HEADERS)
        assert resp.status_code == 404
        assert "Unknown" in resp.json()["detail"]

    @pytest.mark.parametrize("params", [
        {"months": "2030-1"}, {"months": "203001"}, {"months": "2030-13"}, {"months": ""},
        # Month 13 of 2029 would count on as 2030-01, a month the fixture serves; it must be
        # refused as written, not carried over.
        {"months": "2029-13"}, {"months": "2030-00"},
        {"places": "AAA"}, {"places": "1.5"}, {"places": "-1"},
        {"places": "999"}, {"months": "2031-01"}, {"columns": "sb_map"}, {"columns": "ns_map"},
    ])
    def test_a_badly_written_or_unknown_option_is_422(self, app_client, params):
        """A typo is refused with a reason, never answered with an empty table."""
        client, _, _ = app_client
        resp = client.get("/slice/grid/sb", params=params, headers=HEADERS)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]

    @pytest.mark.parametrize("option", [
        "time_ids", "entity_ids", "features", "sample_idx", "with_metadata", "aggregate",
        "force_refresh", "alpha", "enforce_non_negative",
    ])
    def test_the_old_options_change_nothing(self, app_client, option):
        """Only the four options exist (specification). An old one is not read: the answer is
        the same as without it."""
        client, _, _ = app_client
        plain = client.get("/slice/grid/sb", headers=HEADERS).json()
        with_old = client.get("/slice/grid/sb", params={option: "0"}, headers=HEADERS).json()
        assert with_old == plain

    def test_a_repeated_place_or_month_is_answered_once(self, app_client):
        client, _, _ = app_client
        rows = client.get(
            "/slice/grid/sb", params={"places": "100,100", "months": "2029-12,2029-12"}, headers=HEADERS
        ).json()["rows"]
        assert [(r["priogrid_id"], r["month"]) for r in rows] == [(100, "2029-12")]

    def test_an_unknown_outcome_is_refused_before_the_forecast_loads(self, app_client):
        """On a cold server the load can take minutes; a typo should not wait for it."""
        client, mgr, _ = app_client

        def must_not_load(*args, **kwargs):
            raise AssertionError("the forecast was loaded for an unknown outcome")
        mgr._get_latest_dataset = must_not_load
        resp = client.get("/slice/grid/sbb", headers=HEADERS)
        assert resp.status_code == 404 and "sbb" in resp.json()["detail"]

    def test_slices_run_one_at_a_time_on_the_event_loop(self, app_client):
        """A plain `def` would run slices side by side in worker threads, on dataset copies
        that share pandas index objects; concurrent first use of a fresh index gave wrong
        answers (8 in 30, measured in review). So the route stays `async def` (C-285)."""
        import inspect

        _, mgr, _ = app_client
        endpoint = next(r.endpoint for r in mgr.app.routes if getattr(r, "path", "") == "/slice/{level}/{outcome}")
        assert inspect.iscoroutinefunction(endpoint)

    def test_the_newest_recorded_month_is_not_served_as_recorded_deaths(self, app_client):
        """The history's newest month is also the first forecast month (600), and only a sliver
        of it has been reported when the history is cut, with the rest stored as 0. Served, it
        would show 0 deaths next to the forecast (register C-293)."""
        client, _, _ = app_client
        rows = client.get("/slice/admin1/sb", params={"months": "2029-12"}, headers=HEADERS).json()["rows"]
        assert rows and all(r["sb_mean"] is not None and r["sb_actual"] is None for r in rows)

    def test_a_forecast_only_request_does_not_need_the_history(self, app_client):
        """Forecast months only: answered without the history, so a broken history does not
        take these requests away."""
        client, mgr, _ = app_client
        _serve(mgr, history=None)
        resp = client.get("/slice/admin1/sb", params={"months": "2029-12,2030-01"}, headers=HEADERS)
        assert resp.status_code == 200 and len(resp.json()["rows"]) == 4

    def test_a_history_without_the_outcome_is_refused_not_served_blank(self, app_client):
        client, mgr, _ = app_client
        history = _fixture_dataset(mgr, "historical")
        history.targets = [t for t in history.targets if not t.endswith("_os")]
        resp = client.get("/slice/country/os", headers=HEADERS)
        assert resp.status_code == 503 and "recorded deaths" in resp.json()["detail"]

    @pytest.mark.parametrize("query", [
        "places=100&places=103", "months=2029-12&months=2030-01", "columns=sb_mean&columns=sb_actual",
    ])
    def test_a_repeated_option_is_refused_not_cut_to_its_last_value(self, app_client, query):
        """`?places=100&places=103` is how some clients send a list; keeping only 103 would
        answer with part of the table and no sign of it."""
        client, _, _ = app_client
        resp = client.get(f"/slice/grid/sb?{query}", headers=HEADERS)
        assert resp.status_code == 422 and "once, comma-separated" in resp.json()["detail"]

    @pytest.mark.parametrize("params", [{"places": "\u0661\u0660\u0660"}, {"months": "\u0662\u0660\u0662\u0669-12"}])
    def test_digits_of_other_scripts_are_refused(self, app_client, params):
        """`int()` reads Arabic-Indic digits, so one id could be written two ways and come back
        twice. Only 0-9 is accepted."""
        client, _, _ = app_client
        assert client.get("/slice/grid/sb", params=params, headers=HEADERS).status_code == 422

    def test_an_outcome_the_forecast_does_not_hold_is_404(self, app_client):
        """`os` is a known outcome, but a forecast without it must say so, not answer with
        empty summaries."""
        client, mgr, _ = app_client
        forecast = _fixture_dataset(mgr)
        forecast.targets = [t for t in forecast.targets if not t.endswith("_os")]
        resp = client.get("/slice/grid/os", params={"months": "2029-12"}, headers=HEADERS)
        assert resp.status_code == 404 and "Outcomes: sb, ns" in resp.json()["detail"]

    def test_one_place_written_two_ways_is_answered_once(self, app_client):
        client, _, _ = app_client
        rows = client.get(
            "/slice/grid/sb", params={"places": "100,0100", "months": "2029-12"}, headers=HEADERS
        ).json()["rows"]
        assert len(rows) == 1

    def test_a_negative_gaul_code_can_be_asked_for(self, app_client):
        """GAUL gives the Azores admin-1 codes -3727 to -3730; partners must be able to ask."""
        client, mgr, _ = app_client
        forecast = _fixture_dataset(mgr)
        forecast.geo_metadata.loc[forecast.geo_metadata["admin1_gaul1_code"] == 1, "admin1_gaul1_code"] = -3727
        rows = client.get("/slice/admin1/sb", params={"places": "-3727", "months": "2029-12"}, headers=HEADERS).json()["rows"]
        assert [r["admin1_code"] for r in rows] == [-3727]

    def test_without_history_the_slice_is_refused_not_served_blank(self, app_client):
        """Every row carries the recorded deaths. Served empty they would read as "nothing
        recorded", so a missing history is a 503 that says so, not a quiet blank column."""
        client, mgr, _ = app_client
        _serve(mgr, history=None)
        resp = client.get("/slice/admin1/sb", headers=HEADERS)
        assert resp.status_code == 503
        assert "recorded deaths are unavailable" in resp.json()["detail"]
        assert "no historical artifact" not in resp.text   # nothing about the store


class TestBulkRoute:
    """The partner guide's bulk download: `GET /bulk/{level}/{outcome}`, one Parquet file (#203)."""

    @pytest.mark.parametrize("level", _SLICE_LEVELS)
    @pytest.mark.parametrize("outcome", ["sb", "ns", "os"])
    def test_the_file_is_the_slice_with_no_options(self, app_client, level, outcome):
        """Same columns, rows and values as `GET /slice/{level}/{outcome}`: one table, two ways
        to get it, so they cannot disagree."""
        client, _, _ = app_client
        resp = client.get(f"/bulk/{level}/{outcome}", headers=HEADERS)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/vnd.apache.parquet"
        assert f'filename="{outcome}_{level}.parquet"' in resp.headers["content-disposition"]
        bulk = pd.read_parquet(io.BytesIO(resp.content))
        body = client.get(f"/slice/{level}/{outcome}", headers=HEADERS).json()
        sliced = pd.DataFrame(body["rows"], columns=body["columns"])
        assert list(bulk.columns) == body["columns"]
        assert len(bulk) == len(sliced) > 0
        pd.testing.assert_frame_equal(
            bulk.astype(object).where(bulk.notna(), None).reset_index(drop=True),
            sliced.astype(object).where(sliced.notna(), None),
            check_dtype=False,
        )

    def test_recorded_and_forecast_rows_are_told_apart_by_which_columns_are_filled(self, app_client):
        """A recorded month has the recorded deaths and no summaries; a forecast month has the
        summaries and no recorded deaths. Never both, never neither."""
        client, mgr, _ = app_client
        bulk = pd.read_parquet(io.BytesIO(client.get("/bulk/grid/sb", headers=HEADERS).content))
        first_forecast = min(_fixture_dataset(mgr)._time_values)
        recorded = bulk["month_id"] < first_forecast
        assert recorded.any() and (~recorded).any()
        assert bulk.loc[recorded, "sb_actual"].notna().all()
        assert bulk.loc[recorded, "sb_mean"].isna().all() and bulk.loc[recorded, "n_draws"].isna().all()
        assert bulk.loc[~recorded, "sb_mean"].notna().all() and bulk.loc[~recorded, "sb_actual"].isna().all()

    @pytest.mark.parametrize("path", ["/bulk/gaul2/sb", "/bulk/admin1/xx"])
    def test_an_unknown_level_or_outcome_is_404(self, app_client, path):
        client, _, _ = app_client
        assert client.get(path, headers=HEADERS).status_code == 404

    def test_an_outcome_the_forecast_does_not_hold_is_404(self, app_client):
        """`ns` is a known outcome code, but this forecast holds only `sb`."""
        client, mgr, _ = app_client
        only_sb = ForecastDataset(make_fao_df(targets=("pred_lr_ged_sb",)))
        only_sb.source_file_id = "file_002"
        _serve(mgr, forecast=only_sb)
        resp = client.get("/bulk/admin1/ns", headers=HEADERS)
        assert resp.status_code == 404 and "Outcomes: sb" in resp.json()["detail"]

    def test_a_partner_key_the_store_refuses_is_401(self, app_client):
        client, mgr, _ = app_client
        mgr._valid_keys.clear()
        mgr._validate_api_key.side_effect = HTTPException(status_code=401, detail="refused")
        assert client.get("/bulk/admin1/sb", headers=HEADERS).status_code == 401

    def test_without_history_the_file_is_refused_not_served_blank(self, app_client):
        client, mgr, _ = app_client
        _serve(mgr, history=None)
        resp = client.get("/bulk/admin1/sb", headers=HEADERS)
        assert resp.status_code == 503
        assert "recorded deaths are unavailable" in resp.json()["detail"]

    def test_the_file_is_built_once_and_rebuilt_for_a_new_forecast(self, app_client, monkeypatch):
        """Built once, then served as is; a new delivery is a new file. A fresh load of the same
        delivery gets the file already built. (The loader builds the files before a delivery is
        served; here they are removed first, so the route builds them.)"""
        from views_crafdapi.managers import api

        client, mgr, _ = app_client
        mgr._bulk_files.clear()
        builds = []
        real = api.write_bulk_file
        monkeypatch.setattr(api, "write_bulk_file", lambda *a, **k: builds.append(a[3:]) or real(*a, **k))
        first = client.get("/bulk/admin1/sb", headers=HEADERS).content
        assert client.get("/bulk/admin1/sb", headers=HEADERS).content == first
        reloaded = ForecastDataset(make_fao_df(targets=("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")))
        reloaded.source_file_id = "file_002"   # the same delivery, loaded again
        _serve(mgr, forecast=reloaded)
        assert client.get("/bulk/admin1/sb", headers=HEADERS).content == first
        assert builds == [("admin1", "sb")]

        new = ForecastDataset(make_fao_df(seed=7, targets=("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")))
        new.source_file_id = "file_003"
        _serve(mgr, forecast=new)
        second = client.get("/bulk/admin1/sb", headers=HEADERS).content
        assert builds == [("admin1", "sb")] * 2
        assert second != first

    def test_without_a_store_id_the_file_is_built_for_the_request_and_removed(self, app_client):
        """A dataset whose delivery is not known is never matched to a kept file."""
        client, mgr, _ = app_client
        mgr._bulk_files.clear()
        del _fixture_dataset(mgr).source_file_id
        resp = client.get("/bulk/admin1/sb", headers=HEADERS)
        assert resp.status_code == 200 and len(pd.read_parquet(io.BytesIO(resp.content))) > 0
        assert list(mgr._bulk_files._directory.glob("*.parquet*")) == []


class TestResponseSizeBound:
    """C-284: a slice the box cannot afford to render is refused before it is computed.

    The budget is driven through `CRAFDAPI_MAX_RESPONSE_BYTES` rather than by building a huge
    fixture, because the thing under test is the decision, not the arithmetic.
    """

    def test_a_selection_over_budget_is_refused(self, app_client, monkeypatch):
        client, _, _ = app_client
        monkeypatch.setenv("CRAFDAPI_MAX_RESPONSE_BYTES", "1")
        assert client.get("/slice/grid/sb", headers=HEADERS).status_code == 413

    def test_the_refusal_says_how_to_get_the_data(self, app_client, monkeypatch):
        """It names the way through: fewer places or months, or the bulk download."""
        client, _, _ = app_client
        monkeypatch.setenv("CRAFDAPI_MAX_RESPONSE_BYTES", "1")
        detail = client.get("/slice/grid/sb", headers=HEADERS).json()["detail"]
        assert "fewer places or months" in detail
        # The route it names is the bulk file of the same level and outcome, and it answers.
        assert _named_routes(detail) == ["/bulk/grid/sb"]
        assert client.get("/bulk/grid/sb", headers=HEADERS).status_code == 200

    def test_a_narrow_selection_is_still_admitted(self, app_client, monkeypatch):
        """One budget between the two sizes: the whole grid is refused, one cell in one month
        is served. A bound on the whole table would refuse every narrow request."""
        client, _, _ = app_client
        n_columns = len(_IDENTITY["grid"]) + len(_SB_COLUMNS)
        full = estimate_records_bytes(4 * 4, n_columns, 0)   # 4 cells x 4 months
        narrow = estimate_records_bytes(1, n_columns, 0)
        monkeypatch.setenv("CRAFDAPI_MAX_RESPONSE_BYTES", str((full + narrow) // 2))
        assert client.get("/slice/grid/sb", headers=HEADERS).status_code == 413
        resp = client.get(
            "/slice/grid/sb", params={"places": "100", "months": "2029-12"}, headers=HEADERS
        )
        assert resp.status_code == 200 and len(resp.json()["rows"]) == 1

    def test_a_column_list_counts_toward_the_size(self, app_client, monkeypatch):
        """Asking for one column instead of thirteen is a smaller answer, and is judged so."""
        client, _, _ = app_client
        full = estimate_records_bytes(16, len(_IDENTITY["grid"]) + len(_SB_COLUMNS), 0)
        one = estimate_records_bytes(16, len(_IDENTITY["grid"]) + 1, 0)
        monkeypatch.setenv("CRAFDAPI_MAX_RESPONSE_BYTES", str((full + one) // 2))
        assert client.get("/slice/grid/sb", headers=HEADERS).status_code == 413
        assert client.get("/slice/grid/sb", params={"columns": "sb_mean"}, headers=HEADERS).status_code == 200

    def test_the_work_counts_not_only_the_answer(self, app_client, monkeypatch):
        """A country is one row of answer and many cells of work, with every forecast draw.
        Country 10 (cells 100, 101) over all four months reads 2 cells x (2 forecast months x
        10 draws + 2 recorded months) = 44 values. The answer-size estimate is switched off
        here, so only the work can refuse."""
        import views_crafdapi.managers.api as api

        client, _, _ = app_client
        monkeypatch.setattr(api, "estimate_records_bytes", lambda *args: 0)
        params = {"places": "10", "columns": "sb_mean"}
        monkeypatch.setenv("CRAFDAPI_MAX_RESPONSE_BYTES", str(44 * 8 - 1))
        resp = client.get("/slice/country/sb", params=params, headers=HEADERS)
        assert resp.status_code == 413 and "reads 44 values" in resp.json()["detail"]
        monkeypatch.setenv("CRAFDAPI_MAX_RESPONSE_BYTES", str(44 * 8))
        assert client.get("/slice/country/sb", params=params, headers=HEADERS).status_code == 200

    def test_a_month_named_twice_is_counted_once(self, app_client, monkeypatch):
        """Country 10 (2 cells) for one forecast month reads 2 x 10 draws = 20 values. Named
        twice, the month must still count once, or the request is refused for work it will
        not do."""
        import views_crafdapi.managers.api as api

        client, _, _ = app_client
        monkeypatch.setattr(api, "estimate_records_bytes", lambda *args: 0)
        monkeypatch.setenv("CRAFDAPI_MAX_RESPONSE_BYTES", str(20 * 8))
        params = {"places": "10", "months": "2029-12,2029-12"}
        assert client.get("/slice/country/sb", params=params, headers=HEADERS).status_code == 200

    def test_the_default_budget_admits_the_ordinary_request(self, app_client):
        client, _, _ = app_client
        assert client.get("/slice/grid/sb", headers=HEADERS).status_code == 200


# ============================================================
# OperationResult.data consumer site tests (C-06 Sprint 5)
# ============================================================


class TestOperationResultDataConsumers:

    def test_missing_file_bytes_on_download_is_no_forecast_available(self, app_client):
        """`download_result.data` without a 'file_bytes' key: the delivery cannot be read, and
        with nothing cached there is no forecast to serve, so the slice answers 503 (the
        partner guide's "no forecast is available right now"), not a bare 500."""
        client, mgr, mock_pm = app_client
        api_key_hash = mgr._get_api_key_hash("test-api-key")
        mgr._dataframe_cache.pop(api_key_hash, None)
        mgr._loader._served = None   # a cold start: nothing loaded yet
        mock_pm.download_prediction.return_value = OperationResult(
            success=True, data={}
        )
        mock_pm.get_latest_file_id.return_value = OperationResult(
            success=True, data={"file_id": "file_001"}
        )
        # The behaviour under test is the INGEST error path (`download_prediction` returning
        # data with no `file_bytes`), which the loader reaches; nothing gets served.
        assert not mgr._loader.refresh()
        resp = client.get("/slice/grid/sb", headers=HEADERS)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "No forecast is available right now. Try again later."

class TestHTTPExceptionPassthrough:

    @pytest.mark.parametrize("method,path", [
        pytest.param("GET", "/health", id="health"),
    ])
    def test_auth_failure_returns_401(self, app_client, method, path):
        client, mgr, _ = app_client

        def reject_key():
            raise HTTPException(status_code=401, detail="Invalid key")

        mgr.app.dependency_overrides[mgr._get_appwrite_manager] = reject_key
        resp = client.request(method, path, headers=HEADERS)
        assert resp.status_code == 401


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
        assert "age_days" in body["forecast_freshness"]     # #173: the age is the verdict's input
        assert "created_at" in body["forecast_freshness"]   # #173: the delivery-due date reads it


# ============================================================
# C-356 (#156): the degraded fallback through HTTP, across a restart
# ============================================================


def _cold_app(cache_dir, pm):
    """A freshly started CrafdApiManager over an existing disk cache — what systemd's
    Restart=always produces on every deploy. Nothing warm; only `pm` (the store) is mocked."""
    mgr = CrafdApiManager.from_config({}, cache_dir=cache_dir)
    mgr.app = FastAPI()
    mgr._register_routes()
    appwrite = MagicMock()
    appwrite.list_buckets.return_value = MagicMock(success=True)
    mgr._validate_api_key = MagicMock(return_value=appwrite)
    mgr.app.dependency_overrides[mgr._get_prediction_manager] = lambda: pm
    mgr.app.dependency_overrides[mgr._get_appwrite_manager] = lambda: appwrite
    # These tests are about the forecast slot. The store mock holds no history, and the slice
    # refuses to answer without one (503), so the history is a stand-in loaded from memory.
    history = ForecastDataset(make_fao_df(targets=("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")))
    service = mgr._dataset_service
    load = service.get_latest_dataset
    service.get_latest_dataset = lambda m, key, category, *a, **kw: (
        history if category == "historical" else load(m, key, category, *a, **kw)
    )
    # At start the loader loads with the server key (ADR-044), before any request.
    from views_crafdapi.managers.forecast_loader import ForecastLoader

    mgr._loader = ForecastLoader(service, mgr._disk_cache, mgr._bulk_files, "server-key", lambda _k: pm)
    mgr._loader.refresh()
    client = TestClient(mgr.app)
    client.mgr = mgr
    return client


class TestDegradedFallbackAcrossARestart:
    """A good run is served, the service restarts, and the next delivery is refused (``bad``) or
    absent (``none``). ADR-033 §6 says the last-good run is served, degraded and loudly. Before
    #156 every data route answered 500 instead, because the route re-read a warm cache slot the
    fallback never fills."""

    ROUTES = ["/slice/grid/sb", "/bulk/admin1/sb"]

    @pytest.fixture
    def restarted(self, tmp_path):
        from tests.forecast._wire_fixtures import make_wire_run
        from tests.test_dataset_service_fallback import _good_then_bad_manager, _iso

        pm, state = _good_then_bad_manager(make_wire_run(), created_at=_iso(5))
        pm.get_latest_provenance.return_value = None   # the history is the stand-in in `_cold_app`
        cache = tmp_path / "cache"
        first = _cold_app(cache, pm)
        assert first.get(self.ROUTES[0], headers=HEADERS).status_code == 200  # good run on disk
        return cache, pm, state

    @pytest.mark.parametrize("phase", ["bad", "none"])
    @pytest.mark.parametrize("route", ROUTES)
    def test_route_serves_last_good_after_restart(self, restarted, phase, route):
        cache, pm, state = restarted
        state["phase"] = phase
        client = _cold_app(cache, pm)

        resp = client.get(route, headers=HEADERS)
        assert resp.status_code == 200, resp.text

        health = client.get("/health", headers=HEADERS).json()
        assert health["status"] == "degraded"
        assert health["forecast_serving_state"]["serving"] == "last_good_manifested"


# ============================================================
# C-268 (#159): a revoked key fails closed; a transient failure is not "invalid key"
# ============================================================


class TestARevokedKeyIsRefusedOnItsNextRequest:
    """A partner key decides who gets in (ADR-044); the data is read with the server's key. A
    valid key is remembered for one minute, so a key deleted in Appwrite is refused within a
    minute. /health still asks the store with the caller's key, so a refusal there is a 401."""

    ROUTES = [
        "/slice/grid/sb",
        "/slice/country/sb?places=10",
        "/bulk/admin1/sb",
    ]

    @pytest.mark.parametrize("route", ROUTES)
    def test_a_deleted_key_is_refused_within_a_minute(self, app_client, route):
        client, mgr, _ = app_client
        assert client.get(route, headers=HEADERS).status_code == 200        # checked, remembered
        mgr._validate_api_key.side_effect = HTTPException(status_code=401, detail="refused")
        assert client.get(route, headers=HEADERS).status_code == 200        # within the minute
        mgr._valid_keys.clear()                                             # the minute is over
        assert client.get(route, headers=HEADERS).status_code == 401

    def test_a_valid_key_is_remembered_for_one_minute_only(self, app_client):
        _, mgr, _ = app_client
        assert mgr._valid_keys.ttl == 60

    def test_a_store_that_cannot_be_asked_about_the_key_is_503_not_401(self, app_client):
        client, mgr, _ = app_client
        mgr._valid_keys.clear()
        mgr._validate_api_key.side_effect = HTTPException(status_code=503, detail="Metadata store unavailable")
        assert client.get("/slice/grid/sb", headers=HEADERS).status_code == 503

    def test_an_unknown_key_is_checked_before_anything_is_answered(self, app_client):
        client, mgr, _ = app_client
        mgr._valid_keys.clear()
        mgr._validate_api_key.side_effect = HTTPException(status_code=401, detail="refused")
        assert client.get("/bulk/admin1/sb", headers={"X-API-Key": "never-seen"}).status_code == 401

    def test_health_is_401_when_only_the_metadata_lookup_is_refused(self, app_client):
        """A key that lost its database scope but kept buckets.read passes the bucket probe; its
        refused manifest lookup must not be swallowed as a best-effort freshness check."""
        from views_crafdapi.managers.prediction import MetadataLookupError

        client, mgr, mock_pm = app_client
        _serve(mgr, provenance=None)   # /health must ask the store
        mock_pm.get_latest_manifest.side_effect = MetadataLookupError("no", status=401)
        mock_pm.get_latest_provenance.side_effect = MetadataLookupError("no", status=401)
        assert client.get("/health", headers=HEADERS).status_code == 401

    @pytest.mark.parametrize("refused", ["manifest", "stored"])
    def test_each_health_freshness_lookup_refuses_on_its_own(self, app_client, refused):
        from views_crafdapi.managers.prediction import MetadataLookupError, PredictionProvenance

        client, mgr, mock_pm = app_client
        _serve(mgr, provenance=None)      # /health must ask the store
        revoked = MetadataLookupError("no", status=401)
        if refused == "manifest":
            mock_pm.get_latest_manifest.side_effect = revoked
            mock_pm.get_latest_provenance.return_value = PredictionProvenance(
                file_id="l", source="s", created_at="2026-10-01T00:00:00Z", filename="x")
        else:
            mock_pm.get_latest_manifest.return_value = None          # so /health asks for the stored one
            mock_pm.get_latest_provenance.side_effect = revoked
        assert client.get("/health", headers=HEADERS).status_code == 401

    def test_health_is_401_when_the_bucket_probe_is_forbidden(self, app_client):
        client, mgr, _ = app_client
        appwrite = mgr.app.dependency_overrides[mgr._get_appwrite_manager]()
        appwrite.list_buckets.return_value = OperationResult(success=False, status=403)
        assert client.get("/health", headers=HEADERS).status_code == 401

    def test_health_is_401_for_a_revoked_key(self, app_client):
        client, mgr, _ = app_client
        appwrite = mgr.app.dependency_overrides[mgr._get_appwrite_manager]()
        appwrite.list_buckets.return_value = OperationResult(
            success=False, error="not authorized", code="general_unauthorized_scope", status=401,
        )
        assert client.get("/health", headers=HEADERS).status_code == 401


class TestKeyValidationTellsAuthFromOutage:
    """`_validate_api_key` answered every failed `list_buckets` with 401 "Invalid API key" — an
    Appwrite restart, a 5xx or a network timeout told the caller their key was wrong."""

    @pytest.mark.parametrize("status,expected", [
        (401, 401), (403, 401),                    # the store refused this key
        # Anything else says nothing about the key: no answer, rate limit, timeout, a wrong
        # endpoint or project (404), a server error. Telling the caller "invalid key" for these
        # sent them to rotate a key that was fine (#159 review).
        (0, 503), (None, 503), (400, 503), (404, 503), (408, 503), (429, 503), (500, 503), (503, 503),
    ])
    def test_status_maps_to_the_right_refusal(self, tmp_path, monkeypatch, status, expected):
        import views_crafdapi.managers.api as api_module

        mgr = CrafdApiManager.from_config({}, cache_dir=tmp_path / "cache")
        monkeypatch.setattr(mgr, "_create_appwrite_config", lambda key: MagicMock())
        appwrite = MagicMock()
        appwrite.list_buckets.return_value = OperationResult(
            success=False, error="List buckets failed", code="x", status=status,
        )
        monkeypatch.setattr(api_module, "AppWriteFileManager", lambda config: appwrite)

        with pytest.raises(HTTPException) as e:
            mgr._validate_api_key("some-key")
        assert e.value.status_code == expected
        assert "List buckets failed" not in str(e.value.detail)        # C-264

    def test_an_unexpected_exception_is_503_and_missing_config_is_500(self, tmp_path, monkeypatch):
        import views_crafdapi.managers.api as api_module

        mgr = CrafdApiManager.from_config({}, cache_dir=tmp_path / "cache")
        monkeypatch.setattr(mgr, "_create_appwrite_config", lambda key: MagicMock())

        def boom(config):
            raise ConnectionError("connection refused")
        monkeypatch.setattr(api_module, "AppWriteFileManager", boom)
        with pytest.raises(HTTPException) as e:
            mgr._validate_api_key("some-key")
        assert e.value.status_code == 503
        assert e.value.detail == "Metadata store unavailable"     # not "connection refused"

        def missing_env(key):
            raise ValueError("Missing required environment variables: APPWRITE_ENDPOINT")
        monkeypatch.setattr(mgr, "_create_appwrite_config", missing_env)
        with pytest.raises(HTTPException) as e:
            mgr._validate_api_key("some-key")
        assert e.value.status_code == 500
        assert "APPWRITE_ENDPOINT" not in str(e.value.detail)   # deployment detail stays in the log


# ============================================================
# C-282 (#170): /health names the build that wrote the served forecast
# ============================================================


class TestHealthNamesTheBuildThatWroteTheServedForecast:
    """After a deploy the disk slot can still hold what the previous build wrote, and nothing
    said so: "is the fix in effect?" had no answer short of reading files on the box."""

    ROUTE = "/slice/grid/sb"

    @pytest.fixture
    def store(self):
        from tests.forecast._wire_fixtures import make_wire_run
        from tests.test_dataset_service_fallback import _good_then_bad_manager, _iso

        pm, state = _good_then_bad_manager(make_wire_run(), created_at=_iso(5))
        pm.get_latest_provenance.return_value = None
        return pm, state

    def test_a_fresh_ingest_is_reported_as_written_by_the_running_build(self, tmp_path, store):
        from views_crafdapi.version import installed_version

        pm, _ = store
        client = _cold_app(tmp_path / "cache", pm)
        assert client.get(self.ROUTE, headers=HEADERS).status_code == 200

        entry = client.get("/health", headers=HEADERS).json()["served_entry"]
        assert entry["written_by"] == installed_version()
        assert entry["source_kind"] == "wire"
        assert datetime.fromisoformat(entry["written_at"]) > datetime.now(timezone.utc) - timedelta(minutes=5)

    def test_after_a_restart_the_slot_reports_the_build_that_wrote_it(
        self, tmp_path, store, monkeypatch
    ):
        """The slot was written by an older build; the restarted service serves it from disk and
        says so, rather than reporting its own version."""
        import views_crafdapi.managers.disk_cache as disk_cache

        pm, _ = store
        cache = tmp_path / "cache"
        monkeypatch.setattr(disk_cache, "_RUNNING_VERSION", "0.6.9")
        assert _cold_app(cache, pm).get(self.ROUTE, headers=HEADERS).status_code == 200
        monkeypatch.undo()

        client = _cold_app(cache, pm)
        pm.download_prediction.reset_mock()
        for serve in ("disk hit", "warm hit"):
            assert client.get(self.ROUTE, headers=HEADERS).status_code == 200
            health = client.get("/health", headers=HEADERS).json()
            assert health["served_entry"]["written_by"] == "0.6.9", serve
            assert health["status"] == "healthy"   # reported, not judged
        pm.download_prediction.assert_not_called()   # neither request re-ingested

    def test_the_last_good_fallback_reports_the_slot_it_serves(self, tmp_path, store, monkeypatch):
        import views_crafdapi.managers.disk_cache as disk_cache

        pm, state = store
        cache = tmp_path / "cache"
        monkeypatch.setattr(disk_cache, "_RUNNING_VERSION", "0.6.9")
        assert _cold_app(cache, pm).get(self.ROUTE, headers=HEADERS).status_code == 200
        monkeypatch.undo()

        state["phase"] = "bad"
        client = _cold_app(cache, pm)
        assert client.get(self.ROUTE, headers=HEADERS).status_code == 200
        assert client.get("/health", headers=HEADERS).json()["served_entry"]["written_by"] == "0.6.9"

    def test_a_refused_delivery_leaves_the_loaded_forecast_served_and_named(self, tmp_path, store):
        """ADR-044: a new delivery that cannot be served does not take the loaded forecast away,
        even with no last-good slot on disk. /health names what is served and says degraded."""
        pm, state = store
        client = _cold_app(tmp_path / "cache", pm)
        assert client.get(self.ROUTE, headers=HEADERS).status_code == 200
        named = client.get("/health", headers=HEADERS).json()["served_entry"]

        for slot in (tmp_path / "cache").rglob("*_forecast_meta.json"):
            slot.unlink()              # the last-good slot is gone
        state["phase"] = "bad"
        assert not client.mgr._loader.refresh()          # the next check finds nothing servable
        assert client.get(self.ROUTE, headers=HEADERS).status_code == 200
        health = client.get("/health", headers=HEADERS).json()
        assert health["served_entry"] == named and health["status"] == "degraded"

        state["phase"] = "good"        # the producer recovers; the warm entry serves again
        assert client.get(self.ROUTE, headers=HEADERS).status_code == 200
        assert "served_entry" in client.get("/health", headers=HEADERS).json()


# ============================================================
# #208 (ADR-044): the month the forecast was made in, what /health says about the loading, and
# the loader started at start-up and stopped at shutdown
# ============================================================


class TestTheMonthTheForecastWasMadeIn:
    """The fixture's forecast runs 2029-12 to 2030-01; it was made in its first month."""

    def test_a_slice_names_it(self, app_client):
        client, _, _ = app_client
        body = client.get("/slice/admin1/sb", params={"months": "2030-01"}, headers=HEADERS).json()
        assert body["forecast_made_in"] == "2029-12"

    def test_a_bulk_file_names_it_in_a_header(self, app_client):
        client, _, _ = app_client
        resp = client.get("/bulk/admin1/sb", headers=HEADERS)
        assert resp.headers["X-Forecast-Made-In"] == "2029-12"


class TestHealthSaysWhatIsLoaded:

    def test_a_loaded_forecast_is_reported(self, app_client):
        client, _, _ = app_client
        body = client.get("/health", headers=HEADERS).json()
        assert body["forecast_loaded"] is True and "forecast_loading" not in body

    def test_nothing_loaded_yet_is_degraded(self, app_client):
        client, mgr, _ = app_client
        mgr._loader._served = None
        body = client.get("/health", headers=HEADERS).json()
        assert body["forecast_loaded"] is False and body["status"] == "degraded"

    def test_a_forecast_without_its_recorded_deaths_is_degraded(self, app_client):
        """The forecast is served, but bulk files and past months answer 503: not healthy."""
        client, mgr, _ = app_client
        _serve(mgr, history=None)
        body = client.get("/health", headers=HEADERS).json()
        assert body["history_loaded"] is False and body["status"] == "degraded"
        assert body["forecast_loaded"] is True

    def test_the_recorded_deaths_loaded_are_reported(self, app_client):
        client, _, _ = app_client
        assert client.get("/health", headers=HEADERS).json()["history_loaded"] is True

    def test_a_delivery_that_cannot_be_loaded_is_degraded(self, app_client):
        client, mgr, _ = app_client
        _serve(mgr, provenance={"created_at": datetime.now(timezone.utc).isoformat()})
        mgr._loader._failed_at = time.time()
        body = client.get("/health", headers=HEADERS).json()
        assert body["status"] == "degraded" and "could not be loaded" in body["forecast_loading"]
        assert body["forecast_loaded"] is True


class TestTheLoaderRunsWithTheService:

    def test_started_at_start_up_and_stopped_at_shutdown(self, tmp_path):
        import asyncio

        mgr = CrafdApiManager.from_config({}, cache_dir=tmp_path / "cache")
        events = []

        class Loader:
            def __init__(self):
                self.stopped = asyncio.Event()

            async def run(self, minutes):
                events.append("run")
                await self.stopped.wait()
                events.append("ended")

            def stop(self):
                events.append("stop")
                self.stopped.set()

        mgr._loader = Loader()
        with TestClient(FastAPI(lifespan=mgr._lifespan)):
            assert events == ["run"]
        assert events == ["run", "stop", "ended"]

    def test_a_load_that_ended_with_an_error_still_lets_the_caches_clear(self, tmp_path):
        """It no longer writes the caches, so they are cleared, and shutdown does not fail."""
        mgr = CrafdApiManager.from_config({}, cache_dir=tmp_path / "cache")

        class BrokenLoader:
            async def run(self, minutes):
                raise RuntimeError("the loader broke")

            def stop(self):
                pass

        mgr._loader = BrokenLoader()
        with TestClient(FastAPI(lifespan=mgr._lifespan)):
            mgr._file_cache["x"] = {"data": b"1"}
        assert "x" not in mgr._file_cache

    def test_caches_are_kept_when_the_load_does_not_stop_in_time(self, tmp_path):
        """The worker thread may still be writing them; clearing under it is unsafe."""
        import asyncio

        mgr = CrafdApiManager.from_config({}, cache_dir=tmp_path / "cache")
        mgr._STOP_WAIT_SECONDS = 0.1

        class StuckLoader:
            async def run(self, minutes):
                await asyncio.sleep(3600)

            def stop(self):
                pass

        mgr._loader = StuckLoader()
        with TestClient(FastAPI(lifespan=mgr._lifespan)):
            mgr._file_cache["x"] = {"data": b"1"}
        assert "x" in mgr._file_cache


def test_the_loader_is_stopped_before_the_caches_are_cleared(tmp_path):
    """ADR-044: clearing the caches under a running load would pull data from under it."""
    import asyncio

    mgr = CrafdApiManager.from_config({}, cache_dir=tmp_path / "cache")
    caches = {"file": mgr._file_cache, "dataframe": mgr._dataframe_cache, "manager": mgr._manager_cache}
    seen = {}

    class Loader:
        def __init__(self):
            self.stopped = asyncio.Event()

        async def run(self, minutes):
            await self.stopped.wait()
            await asyncio.sleep(0.2)   # a load finishes its step after being asked to stop
            seen["at_run_end"] = {name: dict(c) for name, c in caches.items()}

        def stop(self):
            self.stopped.set()

    mgr._loader = Loader()
    with TestClient(FastAPI(lifespan=mgr._lifespan)):
        for name, cache in caches.items():
            cache["x"] = name
    # Still full when the load ended (the shutdown waited for it), all three empty afterwards.
    assert seen["at_run_end"] == {name: {"x": name} for name in caches}
    assert all(len(c) == 0 for c in caches.values())
