"""The deploy-time check that the built env file carries every coordinate the deployed code
requires (``deployment/check_required_env.py``, run by ``bootstrap.sh part2``; þing 04 D3)."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import views_crafdapi.managers.api as api
from views_crafdapi.managers.api import _REQUIRED_APPWRITE_ENV_VARS

pytestmark = pytest.mark.layer4_infra

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_required_env", _ROOT / "deployment" / "check_required_env.py"
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _env(skip=(), empty=()):
    lines = ["APPWRITE_REGISTRY_VERSION=1.10.0"]
    for name in _REQUIRED_APPWRITE_ENV_VARS:
        if name in skip:
            continue
        lines.append(f"{name}=" if name in empty else f"{name}=v")
    lines.append("APPWRITE_DATASTORE_API_KEY=k")
    return "\n".join(lines) + "\n"


def test_complete_env_passes():
    assert check.absent_names(_env()) == []


@pytest.mark.parametrize("missing", _REQUIRED_APPWRITE_ENV_VARS)
def test_each_required_name_missing_is_reported_alone(missing):
    # The default list is the deployed code's, so every one of its names is checked.
    assert check.absent_names(_env(skip=[missing])) == [missing]


def test_empty_or_blank_value_counts_as_absent():
    assert check.absent_names(_env(empty=["APPWRITE_CRAFD_PROJECT_ID"])) == [
        "APPWRITE_CRAFD_PROJECT_ID"
    ]
    blank = _env().replace("APPWRITE_CRAFD_PROJECT_ID=v", "APPWRITE_CRAFD_PROJECT_ID=  ")
    assert check.absent_names(blank) == ["APPWRITE_CRAFD_PROJECT_ID"]


def test_a_later_empty_line_wins_as_in_dotenv():
    assert check.absent_names(_env() + "APPWRITE_CRAFD_PROJECT_ID=\n") == [
        "APPWRITE_CRAFD_PROJECT_ID"
    ]


def test_the_required_list_is_read_from_the_deployed_code(monkeypatch):
    # A copy of the list in the script would pass today and drift on the next rename.
    monkeypatch.setattr(api, "_REQUIRED_APPWRITE_ENV_VARS", ["APPWRITE_SOME_FUTURE_NAME"])
    assert check.absent_names(_env()) == ["APPWRITE_SOME_FUTURE_NAME"]


def _run(env_text):
    return subprocess.run(
        [sys.executable, str(_ROOT / "deployment" / "check_required_env.py")],
        input=env_text, capture_output=True, text=True, check=False,
    )


def test_main_exits_zero_on_a_complete_file():
    result = _run(_env())
    assert (result.returncode, result.stdout) == (0, "")


def test_main_fails_and_prints_names_never_values():
    secret = "sk_never_print_this"
    text = _env(skip=["APPWRITE_CRAFD_PROJECT_ID"]).replace(
        "APPWRITE_DATASTORE_API_KEY=k", f"APPWRITE_DATASTORE_API_KEY={secret}"
    )
    result = _run(text)
    assert result.returncode == 1
    assert result.stdout.split() == ["APPWRITE_CRAFD_PROJECT_ID"]
    assert secret not in result.stdout + result.stderr


def test_a_comment_or_a_longer_name_does_not_stand_in():
    text = _env(skip=["APPWRITE_CRAFD_PROJECT_ID"])
    text += "# APPWRITE_CRAFD_PROJECT_ID=commented\nAPPWRITE_CRAFD_PROJECT_ID_OLD=x\n"
    assert check.absent_names(text) == ["APPWRITE_CRAFD_PROJECT_ID"]


def test_shared_project_id_does_not_stand_in_for_crafds():
    text = _env(skip=["APPWRITE_CRAFD_PROJECT_ID"]) + "APPWRITE_DATASTORE_PROJECT_ID=shared\n"
    assert check.absent_names(text) == ["APPWRITE_CRAFD_PROJECT_ID"]


def test_bootstrap_refuses_to_install_when_the_check_fails():
    # bootstrap.sh has no executable harness, so this pins the gate's shape on live lines:
    # the check reads the NEW temp file, its failure is not swallowed, and the failure branch
    # returns before the install. Comment lines are ignored so a disabled gate does not pass.
    lines = [
        ln.strip()
        for ln in (_ROOT / "deployment" / "bootstrap.sh").read_text().splitlines()
        if not ln.strip().startswith("#")
    ]
    gate = next(i for i, ln in enumerate(lines) if "check_required_env.py" in ln)
    install = lines.index('sudo mv "$_tmp_env" "${SVC_HOME}/.env.crafdapi"')
    assert lines[gate].startswith('if ! _absent="$(sudo cat "$_tmp_env" |')
    assert "|| true" not in lines[gate]
    branch = lines[gate + 1 : lines.index("fi", gate)]
    assert "return 1" in branch
    assert gate < install
