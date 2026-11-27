"""Regression: the production factory `create_app()` must boot from a clean checkout.

The 2026-07-20 v1.0.0 deploy failed in production because `create_app()` — the real
uvicorn entrypoint — constructs `APIPathManager("un_crafd")`, which with the default
validate=True demands a `apis/un_crafd/` model-training tree. That directory is gitignored
(a runtime artifact), so a clean server clone does not have it, and boot crashed twice:
first FileNotFoundError on the missing tree, then `TypeError: None / "datasets"` because
`.cache` came back None. Every prior app test used the `CrafdApiManager.from_config(...)`
seam, which bypasses APIPathManager — so nothing exercised the production boot path.

These tests boot the real factory from an empty working directory (the deploy state).
"""

import pytest
from fastapi import FastAPI

import views_crafdapi.managers.api as api_module

pytestmark = pytest.mark.layer4_infra

_REQUIRED_ENV = [
    "APPWRITE_ENDPOINT",
    "APPWRITE_CRAFD_PROJECT_ID",
    "APPWRITE_CRAFD_BUCKET_ID",
    "APPWRITE_CRAFD_BUCKET_NAME",
    "APPWRITE_CRAFD_COLLECTION_ID",
    "APPWRITE_CRAFD_COLLECTION_NAME",
    "APPWRITE_METADATA_DATABASE_ID",
    "APPWRITE_METADATA_DATABASE_NAME",
]


@pytest.fixture
def clean_boot(monkeypatch, tmp_path):
    """Boot create_app() from a fresh checkout ROOT that lacks the gitignored
    apis/un_crafd/ runtime tree — exactly the production deploy state. The project
    markers (.gitignore/.git/pyproject.toml) must be present so pyprojroot resolves
    the root, just as the real repo root has them; only apis/un_crafd/ is absent."""
    (tmp_path / ".gitignore").write_text("apis/\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    for var in _REQUIRED_ENV:
        monkeypatch.setenv(var, "placeholder")
    monkeypatch.delenv("APPWRITE_REGISTRY", raising=False)  # placeholders would fail the registry
    monkeypatch.chdir(tmp_path)  # no apis/un_crafd/ here — exactly like a fresh clone
    # create_app caches the app in module globals; reset so we build fresh under tmp cwd.
    monkeypatch.setattr(api_module, "app", None)
    monkeypatch.setattr(api_module, "_fao_manager", None)
    return api_module.create_app()


def test_create_app_boots_without_model_tree(clean_boot):
    """The core regression: the factory returns an app even with no apis/un_crafd/ tree."""
    assert isinstance(clean_boot, FastAPI)


def test_a_clean_boot_resolves_the_service_folders(clean_boot, tmp_path):
    """The folders the service reads and writes, relative to the checkout it is started in. The
    cache folder holds gigabytes of prepared data on the server: a different path there means
    a cold reload, so these are pinned."""
    manager = api_module._fao_manager
    paths, root = manager._model_path, tmp_path.resolve()
    assert paths.model_name == "un_crafd"          # the name every store query is filtered on
    assert paths.dotenv == root / ".env"
    assert paths.cache == root / "apis" / "un_crafd" / "cache"
    assert paths.logging == root / "apis" / "un_crafd" / "logs" and paths.logging.is_dir()
    assert manager._disk_cache.cache_dir == root / "apis" / "un_crafd" / "cache" / "datasets"
    assert manager.configs == {}



def test_booted_app_exposes_liveness_and_version(clean_boot):
    """A clean boot wires the unauthenticated probes (what the deploy verifies)."""
    paths = {route.path for route in clean_boot.routes}
    assert "/ping" in paths
    assert "/version" in paths


def test_building_the_app_leaves_the_stop_signals_to_uvicorn(monkeypatch, tmp_path):
    """#236: a handler of our own replaced uvicorn's, so a stop skipped the lifespan shutdown."""
    import signal

    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    (tmp_path / ".gitignore").write_text("apis/\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    for var in _REQUIRED_ENV:
        monkeypatch.setenv(var, "placeholder")
    monkeypatch.delenv("APPWRITE_REGISTRY", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(api_module, "app", None)
    monkeypatch.setattr(api_module, "_fao_manager", None)
    api_module.create_app()
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before
    # A handler installed later, by the lifespan start-up, is caught by the real-SIGTERM test
    # below: a test client runs the lifespan outside the main thread, where none can be installed.


def test_a_real_sigterm_runs_the_lifespan_shutdown(tmp_path):
    """The production start (uvicorn with the factory), stopped the way systemd stops it. The
    lifespan shutdown must run: it stops the loader before the caches are cleared (ADR-044)."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    (tmp_path / ".gitignore").write_text("apis/\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    env = {k: v for k, v in os.environ.items()
           if k not in ("APPWRITE_REGISTRY", "APPWRITE_CRAFDAPI_SERVER_API_KEY")}   # no real store
    env.update({var: "placeholder" for var in _REQUIRED_ENV})
    # The server process imports the same code this test imports, whichever environment runs it.
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.dirname(api_module.__file__)))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = tmp_path / "server.log"
    with log.open("w") as out:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "views_crafdapi.managers.api:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port), "--timeout-graceful-shutdown", "30"],
            cwd=tmp_path, env=env, stdout=out, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while "Application startup complete" not in log.read_text():
                assert server.poll() is None, log.read_text()
                assert time.monotonic() < deadline, "the server did not start"
                time.sleep(0.2)
            server.send_signal(signal.SIGTERM)
            server.wait(timeout=30)
        finally:
            if server.poll() is None:
                server.kill()
    text = log.read_text()
    assert "Waiting for application shutdown" in text and "Cleared all caches" in text
    assert "did not stop within" not in text      # the idle loader stopped at once
    assert "Application shutdown complete" in text
    # #274: the start used to log three ERROR lines about config scripts the service never had.
    assert "failed AST safety check" not in text and "ERROR" not in text, text
    assert server.returncode == -signal.SIGTERM   # ended by the signal, as systemd expects
