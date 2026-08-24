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
    def fake_get(url, headers=None, timeout=None):
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

    def fetch_subset(self, level, time_ids, entity_ids, *, data_type="historical", features=None):
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
def _df(countries):
    return pd.DataFrame({"country_iso_a3": countries, "pg_xcoord": range(len(countries))})


def test_coverage_present_passes():
    c = _Client(fetch=_df(["IDN", "IDN", "IDN"]))
    r = smoke.check_coverage(c, "country", 559, "IDN", "forecast", ["pred_lr_ged_sb"])
    assert r.ok and "IDN present=True" in r.detail


def test_coverage_empty_fails():
    c = _Client(fetch=_df([]))
    assert not smoke.check_coverage(c, "country", 559, "IDN", "historical").ok


def test_coverage_country_absent_fails():
    # served something, but the requested non-African country isn't in it (a regional-scope regression)
    c = _Client(fetch=_df(["NGA", "ETH"]))
    r = smoke.check_coverage(c, "country", 540, "IDN", "historical")
    assert not r.ok and "IDN present=False" in r.detail


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
    r = smoke.check_coverage(c, "country", 559, "IDN", "forecast", ["pred_lr_ged_sb"])
    assert r.ok  # first call timed out, retry succeeded


# ── run() wiring ─────────────────────────────────────────────────────────────
def test_run_without_key_flags_auth(monkeypatch):
    _patch_get(monkeypatch, {"/ping": _resp(200, {"status": "ok"}),
                             "/version": _resp(200, {"version": "1.3.9", "deployed_tag": "v1.3.9"})})
    results = smoke.run("http://x", "", expect_tag=None, forecast_month=559, hist_month=540)
    names = {r.name: r.ok for r in results}
    assert names["ping"] and names["version"] and names["auth"] is False


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
