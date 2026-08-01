"""S2 (#187 / C-85): an unauthenticated liveness probe for external uptime monitoring.

`/ping` must answer 200 with **no** API key and **no** Appwrite dependency — it is the
target an external monitor polls so a downed service is detected (and alerted) instantly,
rather than discovered by hand. It is deliberately distinct from `/health`, which requires
an API key and verifies the Appwrite connection (readiness, not liveness).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from views_crafdapi.managers.api import CrafdApiManager

pytestmark = pytest.mark.layer3_http


@pytest.fixture
def client(tmp_path):
    mgr = CrafdApiManager.from_config({}, cache_dir=tmp_path / "cache")
    mgr.app = FastAPI()
    mgr._register_routes()
    return TestClient(mgr.app)


def test_ping_returns_200_without_api_key(client):
    resp = client.get("/ping")  # deliberately no X-API-Key header
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_ping_needs_no_appwrite(client):
    """Liveness must not depend on Appwrite — two calls with no creds both succeed."""
    assert client.get("/ping").status_code == 200
    assert client.get("/ping").status_code == 200


def test_ping_is_discoverable_from_root(client):
    body = client.get("/").json()
    assert body["endpoints"].get("liveness_ping") == "/ping"
