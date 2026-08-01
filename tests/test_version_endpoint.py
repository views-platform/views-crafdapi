"""S6 (#191): unauthenticated /version probe for remote deploy verification.

Exposes the installed package version and the pinned deploy tag (S4 gate) so an operator
can confirm *which* version is live from outside — no API key, no side effects.
"""

import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from views_faoapi import version as version_mod
from views_faoapi.managers.api import FAOApiManager

pytestmark = pytest.mark.layer3_http


def _pyproject_version() -> str:
    pyproject = Path(version_mod.__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


@pytest.fixture
def client(tmp_path):
    mgr = FAOApiManager.from_config({}, cache_dir=tmp_path / "cache")
    mgr.app = FastAPI()
    mgr._register_routes()
    return TestClient(mgr.app)


def test_installed_version_matches_pyproject():
    """version reflects the CHECKED-OUT pyproject (authoritative for the deployed code — the S4
    gate pins the tag to it), which can differ from a stale editable-install importlib.metadata."""
    assert version_mod.installed_version() == _pyproject_version()


def test_installed_version_falls_back_to_metadata(monkeypatch):
    """When no source pyproject is reachable (e.g. a wheel-installed layout), fall back to
    importlib.metadata rather than reporting the wrong version."""
    import importlib.metadata as md

    monkeypatch.setattr(version_mod, "_pyproject_version", lambda: None)
    assert version_mod.installed_version() == md.version("views-faoapi")


def test_deployed_tag_none_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAOAPI_DEPLOY_TAG_FILE", str(tmp_path / "does-not-exist"))
    assert version_mod.deployed_tag() is None


def test_deployed_tag_reads_the_pin_file(tmp_path, monkeypatch):
    pin = tmp_path / "tag"
    pin.write_text("v1.2.3\n")
    monkeypatch.setenv("FAOAPI_DEPLOY_TAG_FILE", str(pin))
    assert version_mod.deployed_tag() == "v1.2.3"


def test_version_endpoint_unauth(client, tmp_path, monkeypatch):
    monkeypatch.setenv("FAOAPI_DEPLOY_TAG_FILE", str(tmp_path / "no-pin"))  # deterministic
    resp = client.get("/version")  # deliberately no X-API-Key
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == _pyproject_version()
    assert body["deployed_tag"] is None


def test_version_discoverable_from_root(client):
    body = client.get("/").json()
    assert body["endpoints"].get("version") == "/version"


def test_version_exposes_served_contract_capability(client, tmp_path, monkeypatch):
    """S5 (#250, ADR-033 §7, C-171): /version exposes the wire-contract dialect this build renders,
    so deploy/serve capability skew (a producer delivering a dialect the deploy lags) is remotely
    diagnosable."""
    from views_faoapi.forecast.contract import SERVED_CONTRACT_VERSION

    monkeypatch.setenv("FAOAPI_DEPLOY_TAG_FILE", str(tmp_path / "no-pin"))
    body = client.get("/version").json()  # unauth
    assert body["served_contract_version"] == SERVED_CONTRACT_VERSION
