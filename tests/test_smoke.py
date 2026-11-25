"""Offline unit tests for the post-deploy smoke test (views_crafdapi.smoke, C-173).

No network: `requests.get` is monkeypatched for the unauth checks; a fake client stands in for the
authed ones. Verifies version-tag match/mismatch, /health degraded→FAIL, coverage empty/regional→FAIL
vs non-African-present→PASS, and the cold-start retry-once.
"""
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from views_crafdapi import smoke

pytestmark = pytest.mark.layer4_infra


def _resp(status=200, json_body=None):
    r = SimpleNamespace(status_code=status)
    r.json = lambda: (json_body if json_body is not None else {})
    r.text = ""
    return r


def _patch_get(monkeypatch, by_path):
    """Route smoke.requests.get by URL suffix to a canned response (or raise)."""
    def fake_get(url, headers=None, timeout=None, stream=False):
        for suffix, resp in by_path.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected GET {url}")
    monkeypatch.setattr(smoke.requests, "get", fake_get)


# ── /ping ──────────────────────────────────────────────────────────────────
def test_ping_ok(monkeypatch):
    _patch_get(monkeypatch, {"/ping": _resp(200, {"status": "ok"})})
    assert smoke.check_ping("http://x").ok


def test_ping_unreachable(monkeypatch):
    _patch_get(monkeypatch, {"/ping": requests.exceptions.ConnectionError("refused")})
    r = smoke.check_ping("http://x")
    assert not r.ok and "unreachable" in r.detail


# ── /version ───────────────────────────────────────────────────────────────
def test_version_tag_match(monkeypatch):
    _patch_get(monkeypatch, {"/version": _resp(200, {"version": "1.3.9", "deployed_tag": "v1.3.9"})})
    assert smoke.check_version("http://x", expect_tag="v1.3.9").ok


def test_version_lags_tag_fails(monkeypatch):
    # /version reports old code (1.3.2) under a newer deployed_tag — the C-167/#296 drift.
    _patch_get(monkeypatch, {"/version": _resp(200, {"version": "1.3.2", "deployed_tag": "v1.3.9"})})
    r = smoke.check_version("http://x", expect_tag="v1.3.9")
    assert not r.ok and "disagree" in r.detail


def test_version_expected_tag_mismatch_fails(monkeypatch):
    # consistent build (v1.3.8), but not the tag we expected to have deployed
    _patch_get(monkeypatch, {"/version": _resp(200, {"version": "1.3.8", "deployed_tag": "v1.3.8"})})
    r = smoke.check_version("http://x", expect_tag="v1.3.9")
    assert not r.ok and "expected deployed_tag=v1.3.9" in r.detail


def test_version_no_expect_reports(monkeypatch):
    _patch_get(monkeypatch, {"/version": _resp(200, {"version": "1.3.9", "deployed_tag": "v1.3.9"})})
    assert smoke.check_version("http://x").ok  # no assertion, just reports


# ── /health ────────────────────────────────────────────────────────────────
class _Client:
    def __init__(self, *, health=None, health_exc=None, fetch=None, fetch_exc_then=None):
        self._health, self._health_exc = health, health_exc
        self._fetch, self._fetch_exc_then = fetch, list(fetch_exc_then or [])

    def health(self):
        if self._health_exc:
            raise self._health_exc
        return self._health

    def slice(self, level, outcome, *, places=None, months=None, columns=None):
        self.asked = (level, outcome, places, months, columns)
        if self._fetch_exc_then:
            raise self._fetch_exc_then.pop(0)
        return self._fetch


def test_health_healthy_passes():
    c = _Client(health={"status": "healthy", "appwrite_connected": True,
                        "forecast_freshness": {"is_stale": False}})
    assert smoke.check_health(c).ok


def test_health_degraded_fails():
    c = _Client(health={"status": "degraded", "appwrite_connected": True,
                        "forecast_freshness": {"is_stale": True}})
    r = smoke.check_health(c)
    assert not r.ok and "degraded" in r.detail


def test_health_unreachable_fails():
    c = _Client(health_exc=RuntimeError("API error 401"))
    assert not smoke.check_health(c).ok


# ── coverage ───────────────────────────────────────────────────────────────
def _df(countries, values=None, column="sb_mean"):
    values = values if values is not None else [1.0] * len(countries)
    return pd.DataFrame({"admin1_code": range(len(countries)), "country_iso3": countries, column: values})


def test_coverage_present_passes():
    c = _Client(fetch=_df(["IDN", "IDN", "IDN"]))
    r = smoke.check_coverage(c, "2026-07", "IDN", "sb_mean")
    assert r.ok and "IDN: 3 rows, 3 with a value" in r.detail
    assert c.asked == ("admin1", "sb", None, ["2026-07"], ["sb_mean"])


def test_coverage_empty_fails():
    c = _Client(fetch=_df([], column="sb_actual"))
    assert not smoke.check_coverage(c, "2024-12", "IDN", "sb_actual").ok


def test_coverage_country_absent_fails():
    # served something, but the requested non-African country isn't in it (a regional-scope regression)
    c = _Client(fetch=_df(["NGA", "ETH"], column="sb_actual"))
    r = smoke.check_coverage(c, "2024-12", "IDN", "sb_actual")
    assert not r.ok and "IDN: 0 rows" in r.detail


def test_coverage_country_present_but_without_values_fails():
    # the rows exist but carry nothing: the country is named, not covered
    c = _Client(fetch=_df(["IDN", "IDN"], values=[None, None]))
    r = smoke.check_coverage(c, "2026-07", "IDN", "sb_mean")
    assert not r.ok and "0 with a value" in r.detail


# ── retry-once on cold-server timeout ────────────────────────────────────────
def test_fetch_retry_once_recovers():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.Timeout("cold")
        return "warm-result"

    retried = {"n": 0}
    out = smoke.fetch_retry_once(fetch, on_retry=lambda: retried.__setitem__("n", retried["n"] + 1))
    assert out == "warm-result" and calls["n"] == 2 and retried["n"] == 1


def test_check_coverage_retries_on_timeout():
    c = _Client(fetch=_df(["IDN"]), fetch_exc_then=[requests.exceptions.Timeout("cold")])
    r = smoke.check_coverage(c, "2026-07", "IDN", "sb_mean")
    assert r.ok  # first call timed out, retry succeeded


# ── run() wiring ─────────────────────────────────────────────────────────────
def test_run_without_key_flags_auth(monkeypatch):
    _patch_get(monkeypatch, {"/ping": _resp(200, {"status": "ok"}),
                             "/version": _resp(200, {"version": "1.3.9", "deployed_tag": "v1.3.9"})})
    results = smoke.run("http://x", "", expect_tag=None, forecast_month="2026-07", hist_month="2024-12")
    names = {r.name: r.ok for r in results}
    assert names["ping"] and names["version"] and names["auth"] is False


def test_run_checks_the_forecast_month_for_summaries_and_the_past_month_for_recorded_deaths(monkeypatch):
    """The history check must ask for the recorded deaths; asking it for a forecast column
    would check the forecast twice and the history never."""
    _patch_get(monkeypatch, {"/ping": _resp(200, {"status": "ok"}),
                             "/version": _resp(200, {"version": "1.3.9", "deployed_tag": "v1.3.9"})})
    asked = []

    class Recording(_Client):
        def slice(self, level, outcome, *, places=None, months=None, columns=None):
            asked.append((months, columns))
            return _df(["IDN"], column=columns[0])

    healthy = {"status": "healthy", "appwrite_connected": True, "forecast_freshness": {"is_stale": False},
               "served_run": "chrome_box_x"}
    monkeypatch.setattr(smoke, "CrafdApiClient", lambda base, key, timeout: Recording(health=healthy))
    _patch_bulk(monkeypatch)
    _patch_get_extra = smoke.requests.get
    def get(url, headers=None, timeout=None, stream=False):
        if url.endswith("/ping"):
            return _resp(200, {"status": "ok"})
        if url.endswith("/version"):
            return _resp(200, {"version": "1.3.9", "deployed_tag": "v1.3.9"})
        return _patch_get_extra(url, headers=headers, timeout=timeout, stream=stream)
    monkeypatch.setattr(smoke.requests, "get", get)
    results = smoke.run("http://x", "key", expect_tag=None, forecast_month="2026-07", hist_month="2024-12")
    coverage = [r for r in results if "coverage" in r.name]
    assert len(coverage) == 2 and all(r.ok for r in coverage), results
    # Every other step of the run, on good data, passes too (the warm check is timing only).
    assert all(r.ok for r in results if r.name != "warm check"), [r for r in results if not r.ok]
    assert {"served run", "data: months", "data: summaries", "data: sb_mean adds up",
            "data: sb_actual adds up"} <= {r.name for r in results}
    assert asked[:2] == [(["2026-07"], ["sb_mean"]), (["2024-12"], ["sb_actual"])]


class TestExpectTagCannotSilentlyInspectNothing:
    """An empty `--expect-tag` must fail loudly, not skip the check.

    Found 2026-08-24, during the v0.7.0 deploy. `check_version` guarded the comparison with
    `if expect_tag and ...`, so an empty string disabled it and the check reported PASS. That
    would be a latent trap on its own; the release runbook turned it into a live one by
    instructing

        sudo -iu views-crafdapi-deploy
        ...
        .venv/bin/python scripts/smoke.py --expect-tag "$TAG"

    `sudo -i` starts a fresh login shell, and `TAG` was set in the *previous* user's shell and
    never exported. So `$TAG` is empty there by construction, and every deploy that followed the
    runbook literally ran the tag assertion against nothing while printing `version ... PASS`.

    This is the failure mode the repo keeps rediscovering: a check that silently inspects
    nothing is worse than no check, because it is credited as evidence.
    """

    def test_empty_expect_tag_is_refused(self):
        with pytest.raises(ValueError, match="--expect-tag was given an empty value"):
            smoke.check_version("http://localhost", expect_tag="")

    def test_whitespace_expect_tag_is_refused(self):
        """`--expect-tag "$TAG"` with TAG unset expands to `''`; with TAG=" " it expands to
        whitespace. Both mean the operator asked for a check they did not get."""
        with pytest.raises(ValueError, match="--expect-tag was given an empty value"):
            smoke.check_version("http://localhost", expect_tag="   ")

    def test_omitting_expect_tag_is_still_allowed(self, monkeypatch):
        """Not asking for the check is fine and stays fine — the default is None. Only *asking*
        for it and silently getting nothing is the defect."""
        monkeypatch.setattr(
            "views_crafdapi.smoke._get",
            lambda base, path, timeout: (
                200,
                {"version": "0.7.0", "deployed_tag": "v0.7.0", "served_contract_version": "1.5"},
                None,
            ),
        )
        assert smoke.check_version("http://localhost").ok

    def test_a_real_mismatch_still_fails(self, monkeypatch):
        monkeypatch.setattr(
            "views_crafdapi.smoke._get",
            lambda base, path, timeout: (
                200,
                {"version": "0.6.1", "deployed_tag": "v0.6.1", "served_contract_version": "1.5"},
                None,
            ),
        )
        assert not smoke.check_version("http://localhost", expect_tag="v0.7.0").ok


# ── #262: which run is served, and the data is right ───────────────────────
def test_served_run_must_be_the_expected_run():
    c = _Client(health={"served_run": "chrome_box_forecasting_20261112_210150"})
    assert smoke.check_served_run(c, "chrome_box_forecasting_20261112_210150").ok
    wrong = smoke.check_served_run(c, "rusty_bucket_forecasting_20260727_095355")
    assert not wrong.ok and "expected" in wrong.detail


def test_no_served_run_passes_unless_one_is_expected():
    assert smoke.check_served_run(_Client(health={}), None).ok          # an older release
    assert not smoke.check_served_run(_Client(health={}), "chrome_box_x").ok


def _bulk_response(table, made_in="2026-07"):
    import io
    buf = io.BytesIO()
    table.to_parquet(buf)
    r = SimpleNamespace(status_code=200, content=buf.getvalue(), headers={"X-Forecast-Made-In": made_in})
    r.raise_for_status = lambda: None
    r.iter_content = lambda chunk_size: [r.content]
    return r


def _country_table(draws=1024, country_total=7.0, recorded_total=5.0):
    rows = [{"month": "2026-06", "country_code": 1, "sb_actual": recorded_total, "sb_mean": float("nan")}]
    rows += [{"month": m, "country_code": 1, "sb_p_gt0": 0.5, "sb_p_gt25": 0.2, "sb_p_gt100": 0.1,
              "sb_p_gt500": 0.0, "sb_p_gt1000": 0.0, "sb_mean": country_total, "n_draws": draws,
              "sb_actual": float("nan")} for m in ("2026-07", "2026-08")]
    return pd.DataFrame(rows)


def _region_tables():
    """Grid cells (two regions, one cell in the country but no region) and admin-1 regions whose
    totals agree with a country of 7.0 expected and 5.0 recorded deaths."""
    grid = pd.DataFrame({"month": ["2026-07"] * 3 + ["2026-06"] * 3,
                         "admin1_code": [1, 2, None, 1, 2, None], "country_code": [1, 1, 1, 1, 1, 1],
                         "sb_mean": [3.0, 3.0, 1.0] + [float("nan")] * 3,
                         "sb_actual": [float("nan")] * 3 + [2.0, 2.0, 1.0]})
    admin1 = pd.DataFrame({"month": ["2026-07", "2026-07", "2026-06", "2026-06"], "admin1_code": [1, 2, 1, 2],
                           "sb_mean": [3.0, 3.0, float("nan"), float("nan")],
                           "sb_actual": [float("nan"), float("nan"), 2.0, 2.0]})
    return grid, admin1


def _patch_bulk(monkeypatch, country=None, grid=None, admin1=None):
    g, a = _region_tables()
    _patch_get(monkeypatch, {"/bulk/country/sb": _bulk_response(country if country is not None else _country_table()),
                             "/bulk/grid/sb": _bulk_response(grid if grid is not None else g),
                             "/bulk/admin1/sb": _bulk_response(admin1 if admin1 is not None else a)})


def test_data_checks_pass_on_right_data(monkeypatch):
    _patch_bulk(monkeypatch)
    results = smoke.check_data("http://x", "key", 1024, 10.0)
    assert len(results) == 4 and all(r.ok for r in results), results


def test_data_checks_fail_on_the_wrong_number_of_draws(monkeypatch):
    _patch_bulk(monkeypatch, country=_country_table(draws=128))
    results = {r.name: r for r in smoke.check_data("http://x", "key", 1024, 10.0)}
    assert not results["data: summaries"].ok


def test_data_checks_fail_when_the_forecast_does_not_add_up(monkeypatch):
    _patch_bulk(monkeypatch, country=_country_table(country_total=9.0))
    results = {r.name: r for r in smoke.check_data("http://x", "key", None, 10.0)}
    assert not results["data: sb_mean adds up"].ok and results["data: sb_actual adds up"].ok


def test_data_checks_fail_when_the_recorded_deaths_do_not_add_up(monkeypatch):
    """The recorded deaths are checked in the month before the forecast starts (2026-06)."""
    _patch_bulk(monkeypatch, country=_country_table(recorded_total=6.0))
    results = {r.name: r for r in smoke.check_data("http://x", "key", None, 10.0)}
    assert not results["data: sb_actual adds up"].ok and results["data: sb_mean adds up"].ok


def test_a_malformed_bulk_file_is_a_failed_check_not_a_crash(monkeypatch):
    _patch_bulk(monkeypatch, country=_country_table().drop(columns=["n_draws"]))
    results = {r.name: r for r in smoke.check_data("http://x", "key", None, 10.0)}
    assert not results["data: summaries"].ok


def test_an_unreachable_grid_file_fails_the_levels_check(monkeypatch):
    _patch_bulk(monkeypatch, grid=pd.DataFrame({"x": [1]}))   # no month column: unreadable for the check
    results = {r.name: r for r in smoke.check_data("http://x", "key", None, 10.0)}
    assert not results["data: levels"].ok


def test_data_checks_report_an_unreachable_bulk_file(monkeypatch):
    _patch_get(monkeypatch, {"/bulk/country/sb": requests.exceptions.ConnectionError("refused")})
    results = smoke.check_data("http://x", "key", None, 10.0)
    assert len(results) == 1 and not results[0].ok


def test_the_key_is_read_from_crafd_key():
    """The name partners use (docs/api/README.md); the shared project's key name is gone (#262)."""
    assert smoke._KEY_ENV == "CRAFD_KEY"


def test_an_empty_expected_run_is_refused_not_a_pass():
    with pytest.raises(ValueError, match="--expect-run"):
        smoke.check_served_run(_Client(health={"served_run": "x"}), "  ")


def test_the_month_before_a_january_forecast_is_december():
    assert smoke._month_before("2027-01") == "2026-12"
    assert smoke._month_before("2026-07") == "2026-06"


def test_an_unexpected_error_in_the_levels_check_is_a_failed_check(monkeypatch):
    _patch_bulk(monkeypatch)
    def boom(by_level, column):
        raise KeyError("month")
    monkeypatch.setattr(smoke, "check_levels_add_up", boom)
    results = {r.name: r for r in smoke.check_data("http://x", "key", None, 10.0)}
    assert not results["data: sb_mean adds up"].ok and "KeyError" in results["data: sb_mean adds up"].detail


def test_an_expected_run_that_is_not_reported_fails():
    r = smoke.check_served_run(_Client(health={}), "chrome_box_x")
    assert not r.ok and "not reported" in r.detail


def test_an_empty_expect_run_is_refused_before_any_request(monkeypatch):
    monkeypatch.setattr(smoke.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no request")))
    with pytest.raises(SystemExit):
        smoke.main(["--expect-run", " "])


def test_a_bulk_download_retries_once_after_a_dropped_connection(monkeypatch):
    calls = []
    good = _bulk_response(_country_table())
    good.iter_content = lambda chunk_size: [good.content]
    def get(url, headers=None, timeout=None, stream=False):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("dropped")
        return good
    monkeypatch.setattr(smoke.requests, "get", get)
    monkeypatch.setattr(smoke.time, "sleep", lambda s: None)
    table, made_in = smoke._read_bulk("http://x", "key", "country", 10.0)
    assert len(calls) == 2 and made_in == "2026-07" and len(table) == 3


def test_a_bulk_download_retries_once_after_a_502(monkeypatch):
    calls = []
    bad = SimpleNamespace(status_code=502, close=lambda: calls.append("closed"))
    good = _bulk_response(_country_table())
    good.iter_content = lambda chunk_size: [good.content]
    def get(url, headers=None, timeout=None, stream=False):
        calls.append(url)
        return bad if len([c for c in calls if c != "closed"]) == 1 else good
    monkeypatch.setattr(smoke.requests, "get", get)
    monkeypatch.setattr(smoke.time, "sleep", lambda s: None)
    table, made_in = smoke._read_bulk("http://x", "key", "country", 10.0)
    assert calls.count("closed") == 1 and made_in == "2026-07" and len(table) == 3
