"""The check run before a partner key is sent (``deployment/partner_key_check.py``,
``deployment/PARTNER_KEYS.md`` step 3; þing 04 D7)."""

import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from views_crafdapi.managers.api import CrafdApiManager
from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager

pytestmark = pytest.mark.layer4_infra

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deployment" / "partner_key_check.py"
_spec = importlib.util.spec_from_file_location("partner_key_check", _SCRIPT)
pkc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pkc)

KEY = "sk_partner_secret_value_42"


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / ".env.crafdapi"
    # As the real server env is: the shared project's id is there too, and a value may be quoted.
    path.write_text(
        "APPWRITE_ENDPOINT=https://store.example.invalid/v1\n"
        "APPWRITE_DATASTORE_PROJECT_ID=shared_project\n"
        "APPWRITE_CRAFD_PROJECT_ID=crafd_project\n"
        'APPWRITE_CRAFD_BUCKET_ID="crafd_bucket"\n'
        "APPWRITE_DATASTORE_API_KEY=server_key\n"
    )
    return path


class _Response(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _store(buckets=None, status=200, http_error=None, seen=None, raises=None):
    def urlopen(request, timeout):
        if seen is not None:
            seen.append(request)
        if raises:
            raise raises
        if http_error:
            raise urllib.error.HTTPError(request.full_url, http_error, "refused", {}, None)
        return _Response(status, {"buckets": [{"$id": b} for b in (buckets or [])]})
    return patch.object(pkc.urllib.request, "urlopen", urlopen)


def test_fenced_key_passes_and_gets_the_services_own_partition_label(env_file, tmp_path):
    cache_dir = _salted(tmp_path / "datasets")
    with _store(["crafd_bucket"]):
        fenced, report = pkc.check(KEY, env_file, cache_dir)
    expected = CrafdDiskCacheManager(cache_dir)._partition(
        CrafdApiManager._get_api_key_hash(None, KEY)
    )
    assert fenced
    assert f"Partition label: {expected}" in report


def test_it_asks_the_configured_project_and_endpoint(env_file, tmp_path):
    seen = []
    with _store(["crafd_bucket"], seen=seen):
        pkc.check(KEY, env_file, tmp_path / "datasets")
    (request,) = seen
    assert request.full_url == "https://store.example.invalid/v1/storage/buckets"
    assert request.get_header("X-appwrite-project") == "crafd_project"
    assert request.get_header("X-appwrite-key") == KEY  # the key under test, not anonymous


@pytest.mark.parametrize("buckets", [
    ["crafd_bucket", "unfao_bucket"],
    ["crafd_bucket", "production_forecasts"],
    ["unfao_bucket"],
    [],
])
def test_any_other_reach_fails(env_file, tmp_path, buckets):
    with _store(buckets):
        fenced, report = pkc.check(KEY, env_file, tmp_path / "datasets")
    assert not fenced and "NOT FENCED" in report


@pytest.mark.parametrize("code", [401, 403, 404])
def test_a_refused_check_fails_rather_than_reading_as_empty(env_file, tmp_path, code):
    with _store(http_error=code):
        fenced, report = pkc.check(KEY, env_file, tmp_path / "datasets")
    assert not fenced and f"HTTP {code}" in report


@pytest.mark.parametrize("status", [201, 204])
def test_a_non_200_success_status_fails(env_file, tmp_path, status):
    with _store(["crafd_bucket"], status=status):
        fenced, _ = pkc.check(KEY, env_file, tmp_path / "datasets")
    assert not fenced


def test_server_not_on_crafds_project_fails(tmp_path):
    env = tmp_path / ".env.crafdapi"
    env.write_text("APPWRITE_ENDPOINT=https://fra.cloud.appwrite.io/v1\n"
                   "APPWRITE_DATASTORE_PROJECT_ID=shared\nAPPWRITE_CRAFD_BUCKET_ID=crafd_bucket\n")
    fenced, report = pkc.check(KEY, env, tmp_path / "datasets")
    assert not fenced and "APPWRITE_CRAFD_PROJECT_ID" in report


def _salted(cache_dir):
    CrafdDiskCacheManager(cache_dir)  # the running service has created its salt
    return cache_dir


_SCENARIOS = {
    "fenced-labelled": ({"buckets": ["crafd_bucket"]}, True, 0),
    "fenced-unlabelled": ({"buckets": ["crafd_bucket"]}, False, 0),
    "not-fenced": ({"buckets": ["crafd_bucket", "unfao_bucket"]}, True, 1),
    "refused": ({"http_error": 401}, True, 1),
    "could-not-run": ({"raises": OSError("network down")}, True, 1),
}


@pytest.mark.parametrize("scenario", list(_SCENARIOS))
def test_main_exit_code_and_no_part_of_the_key_on_any_output(
    env_file, tmp_path, monkeypatch, capsys, scenario
):
    store, salted, exit_code = _SCENARIOS[scenario]
    cache_dir = _salted(tmp_path / "datasets") if salted else tmp_path / "datasets"
    monkeypatch.setattr(pkc, "DEFAULT_ENV_FILE", env_file)
    monkeypatch.setattr(pkc, "DEFAULT_CACHE_DIR", cache_dir)
    monkeypatch.setattr(pkc.sys, "stdin", io.StringIO(KEY + "\n"))
    with _store(**store):
        assert pkc.main() == exit_code
    out = capsys.readouterr()
    shown = out.out + out.err
    assert not any(KEY[i:i + 6] in shown for i in range(len(KEY) - 5))


def test_server_env_missing_crafd_project_never_shows_the_key(tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env.crafdapi"
    env.write_text("APPWRITE_ENDPOINT=https://store.example.invalid/v1\n")
    monkeypatch.setattr(pkc, "DEFAULT_ENV_FILE", env)
    monkeypatch.setattr(pkc.sys, "stdin", io.StringIO(KEY))
    assert pkc.main() == 1
    out = capsys.readouterr()
    assert KEY[:6] not in out.out + out.err


def test_existing_cache_dir_without_a_salt_gets_no_label_and_no_salt(env_file, tmp_path):
    cache_dir = tmp_path / "datasets"
    cache_dir.mkdir()  # the service directory exists, but the service never created its salt
    with _store(["crafd_bucket"]):
        fenced, report = pkc.check(KEY, env_file, cache_dir)
    assert fenced and "Partition label: UNAVAILABLE" in report
    assert list(cache_dir.iterdir()) == []


def test_main_exits_nonzero_on_an_empty_stdin():
    result = subprocess.run([sys.executable, str(_SCRIPT)], input="", capture_output=True,
                            text=True, check=False)
    assert result.returncode == 1 and "no key" in result.stdout


def test_no_service_salt_gives_no_label_and_creates_nothing(env_file, tmp_path):
    cache_dir = tmp_path / "wrong_path"
    with _store(["crafd_bucket"]):
        fenced, report = pkc.check(KEY, env_file, cache_dir)
    assert fenced
    assert "Partition label: UNAVAILABLE" in report
    assert not cache_dir.exists()
