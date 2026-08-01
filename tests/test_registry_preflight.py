"""þing-01 #278 (verdict D6): faoapi's in-process preflight validates its resolved environment
against the canonical PLATFORM-001 coordinate registry — coordinates must match, fail-loud naming
the offending variable. Transition-safe: no registry reachable ⇒ only the presence check runs."""

import pytest

from views_crafdapi.managers.api import (
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

[target.APPWRITE_CRAFD_BUCKET_ID]
class = "target"
value = "unfao_bucket"

[secret.APPWRITE_DATASTORE_API_KEY]
class = "secret"
issued_by = "operator"
"""


def _write_registry(tmp_path):
    reg = tmp_path / "coordinate_registry.toml"
    reg.write_text(_FIXTURE)
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
