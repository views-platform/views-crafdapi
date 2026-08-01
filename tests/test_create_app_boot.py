"""Regression: the production factory `create_app()` must boot from a clean checkout.

The 2026-07-20 v1.0.0 deploy failed in production because `create_app()` — the real
uvicorn entrypoint — constructs `APIPathManager("un_fao")`, which with the default
validate=True demands a `apis/un_fao/` model-training tree. That directory is gitignored
(a runtime artifact), so a clean server clone does not have it, and boot crashed twice:
first FileNotFoundError on the missing tree, then `TypeError: None / "datasets"` because
`.cache` came back None. Every prior app test used the `FAOApiManager.from_config(...)`
seam, which bypasses APIPathManager — so nothing exercised the production boot path.

These tests boot the real factory from an empty working directory (the deploy state).
"""

import pytest
from fastapi import FastAPI

import views_faoapi.managers.api as api_module

pytestmark = pytest.mark.layer4_infra

_REQUIRED_ENV = [
    "APPWRITE_ENDPOINT",
    "APPWRITE_DATASTORE_PROJECT_ID",
    "APPWRITE_UNFAO_BUCKET_ID",
    "APPWRITE_UNFAO_BUCKET_NAME",
    "APPWRITE_UNFAO_COLLECTION_ID",
    "APPWRITE_UNFAO_COLLECTION_NAME",
    "APPWRITE_METADATA_DATABASE_ID",
    "APPWRITE_METADATA_DATABASE_NAME",
]


@pytest.fixture
def clean_boot(monkeypatch, tmp_path):
    """Boot create_app() from a fresh checkout ROOT that lacks the gitignored
    apis/un_fao/ runtime tree — exactly the production deploy state. The project
    markers (.gitignore/.git/pyproject.toml) must be present so pyprojroot resolves
    the root, just as the real repo root has them; only apis/un_fao/ is absent."""
    (tmp_path / ".gitignore").write_text("apis/\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\nversion = '0'\n")
    for var in _REQUIRED_ENV:
        monkeypatch.setenv(var, "placeholder")
    monkeypatch.chdir(tmp_path)  # no apis/un_fao/ here — exactly like a fresh clone
    # create_app caches the app in module globals; reset so we build fresh under tmp cwd.
    monkeypatch.setattr(api_module, "app", None)
    monkeypatch.setattr(api_module, "_fao_manager", None)
    return api_module.create_app()


def test_create_app_boots_without_model_tree(clean_boot):
    """The core regression: the factory returns an app even with no apis/un_fao/ tree."""
    assert isinstance(clean_boot, FastAPI)


def test_booted_app_exposes_liveness_and_version(clean_boot):
    """A clean boot wires the unauthenticated probes (what the deploy verifies)."""
    paths = {route.path for route in clean_boot.routes}
    assert "/ping" in paths
    assert "/version" in paths
