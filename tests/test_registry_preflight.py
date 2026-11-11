"""þing-01 #278 (verdict D6): faoapi's in-process preflight validates its resolved environment
against the canonical PLATFORM-001 coordinate registry — coordinates must match, fail-loud naming
the offending variable. Transition-safe: no registry reachable ⇒ only the presence check runs."""

import tomllib

import pytest

from views_crafdapi.managers.api import (
    _REQUIRED_APPWRITE_ENV_VARS,
    _registry_coordinates,
    _validate_env_against_registry,
)

pytestmark = pytest.mark.layer4_infra

_FIXTURE = """
[meta]
contract = "PLATFORM-001"

[connection.APPWRITE_ENDPOINT]
class = "connection"
value = "https://example.appwrite.io/v1"

[connection.APPWRITE_DATASTORE_PROJECT_ID]
class = "connection"
value = "shared_project"

[connection.APPWRITE_CRAFD_PROJECT_ID]
class = "connection"
value = "crafd_project"

[target.APPWRITE_CRAFD_BUCKET_ID]
class = "target"
value = "unfao_bucket"

[target.APPWRITE_CRAFD_BUCKET_NAME]
class = "target"
value = "bucket-name"

[target.APPWRITE_CRAFD_COLLECTION_ID]
class = "target"
value = "coll"

[target.APPWRITE_CRAFD_COLLECTION_NAME]
class = "target"
value = "coll-name"

[target.APPWRITE_METADATA_DATABASE_ID]
class = "target"
value = "db"

[target.APPWRITE_METADATA_DATABASE_NAME]
class = "target"
value = "db-name"

[secret.APPWRITE_DATASTORE_API_KEY]
class = "secret"
issued_by = "operator"
"""


def _write_registry(tmp_path, text=_FIXTURE):
    reg = tmp_path / "coordinate_registry.toml"
    reg.write_text(text)
    return str(reg)


def test_registry_coordinates_reads_connection_and_target_not_secrets(tmp_path):
    coords = _registry_coordinates(_write_registry(tmp_path))
    assert coords["APPWRITE_ENDPOINT"] == "https://example.appwrite.io/v1"
    assert coords["APPWRITE_CRAFD_BUCKET_ID"] == "unfao_bucket"
    assert "APPWRITE_DATASTORE_API_KEY" not in coords  # a secret slot has no value


def test_env_matching_the_registry_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("APPWRITE_REGISTRY", _write_registry(tmp_path))
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.appwrite.io/v1")
    monkeypatch.setenv("APPWRITE_CRAFD_BUCKET_ID", "unfao_bucket")
    _validate_env_against_registry()  # no raise


def test_env_disagreeing_with_registry_raises_naming_the_var(tmp_path, monkeypatch):
    monkeypatch.setenv("APPWRITE_REGISTRY", _write_registry(tmp_path))
    monkeypatch.setenv("APPWRITE_CRAFD_BUCKET_ID", "a_typo_bucket")  # drifted from the registry
    with pytest.raises(ValueError, match="APPWRITE_CRAFD_BUCKET_ID"):
        _validate_env_against_registry()


def test_no_registry_reachable_is_a_no_op(tmp_path, monkeypatch):
    # transition-safe: registry not pointed at ⇒ only the presence check (elsewhere) runs.
    monkeypatch.delenv("APPWRITE_REGISTRY", raising=False)
    monkeypatch.setenv("APPWRITE_CRAFD_BUCKET_ID", "anything")
    assert _validate_env_against_registry() is None


@pytest.mark.parametrize("missing", _REQUIRED_APPWRITE_ENV_VARS)
def test_required_coordinate_missing_from_the_registry_raises_naming_only_it(
    tmp_path, monkeypatch, missing
):
    # þing 04 orð_10: a required name the registry does not carry must refuse, not pass
    # unchecked. CRAF'd's project is the live case: the shared project's row does not stand in.
    registry = tomllib.loads(_FIXTURE)
    for cls in ("connection", "target"):
        registry[cls].pop(missing, None)
    lines = []
    for cls in ("connection", "target"):
        for name, entry in registry[cls].items():
            lines += [f"[{cls}.{name}]", f'class = "{cls}"', f'value = "{entry["value"]}"', ""]
    monkeypatch.setenv("APPWRITE_REGISTRY", _write_registry(tmp_path, "\n".join(lines)))
    # Set in the environment, as a hand-built .env would have it: still refused, because
    # nothing can check it.
    monkeypatch.setenv(missing, "set_in_env")
    with pytest.raises(ValueError) as excinfo:
        _validate_env_against_registry()
    message = str(excinfo.value)
    assert f"{missing} has no value in the registry" in message
    for present in _REQUIRED_APPWRITE_ENV_VARS:
        if present != missing:
            assert present not in message


def test_crafd_project_id_disagreeing_with_registry_raises(tmp_path, monkeypatch):
    # The shared project's id in CRAF'd's slot is the one mistake that would put partners back
    # next to FAO's data and the shelf.
    monkeypatch.setenv("APPWRITE_REGISTRY", _write_registry(tmp_path))
    monkeypatch.setenv("APPWRITE_CRAFD_PROJECT_ID", "shared_project")
    with pytest.raises(ValueError, match="APPWRITE_CRAFD_PROJECT_ID"):
        _validate_env_against_registry()
