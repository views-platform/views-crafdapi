"""Epic #184 (S3/S4): the deployment-as-code artifacts must not drift silently.

The systemd unit, the deploy-gate script, and the bootstrap are production
configuration living in the repo. These golden-string tests pin their
load-bearing lines — the same pattern as the wire-contract guard tests: a
change to any pinned value is a deliberate decision, not a refactor casualty.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.layer4_infra

_ROOT = Path(__file__).parent.parent
_UNIT = (_ROOT / "deployment" / "views-crafdapi.service").read_text()
_GATE = (_ROOT / "scripts" / "checkout-deploy-tag.sh").read_text()
_BOOTSTRAP = (_ROOT / "deployment" / "bootstrap.sh").read_text()


class TestSystemdUnit:
    def test_runs_as_the_service_account(self):
        assert "User=views-crafdapi-deploy" in _UNIT

    def test_start_passes_the_deploy_gate(self):
        assert "ExecStartPre=" in _UNIT and "checkout-deploy-tag.sh" in _UNIT

    def test_serves_the_factory_app_on_localhost_8001(self):
        assert "views_crafdapi.managers.api:create_app" in _UNIT
        assert "--factory" in _UNIT
        assert "--host 127.0.0.1" in _UNIT  # nginx upstream contract — never 0.0.0.0
        # Port 8001, NOT 8000: crafdapi co-hosts with faoapi (:8000) on the same box;
        # a shared port would make the two services' units collide (address in use).
        assert "--port 8001" in _UNIT

    def test_self_heals_and_survives_reboot(self):
        assert "Restart=always" in _UNIT
        assert "WantedBy=multi-user.target" in _UNIT

    def test_credentials_come_from_the_env_file(self):
        assert "EnvironmentFile=/home/views-crafdapi-deploy/.env.crafdapi" in _UNIT


class TestDeployGateScript:
    def test_fails_loud(self):
        assert "set -euo pipefail" in _GATE

    def test_tag_file_convention_matches_version_endpoint(self):
        """The gate and GET /version must read the same file, so the served
        version is verifiable remotely (S4 x S6)."""
        from views_crafdapi import version as version_mod
        assert version_mod._DEFAULT_DEPLOY_TAG_FILE == "~/.views-crafdapi-deploy-tag"
        assert ".views-crafdapi-deploy-tag" in _GATE
        assert "CRAFDAPI_DEPLOY_TAG_FILE" in _GATE  # same override env var

    def test_verifies_the_tag_exists_before_checkout(self):
        assert "git fetch --tags" in _GATE
        assert "rev-parse -q --verify" in _GATE

    def test_pins_the_environment_to_the_lockfile(self):
        assert "uv sync --frozen" in _GATE

    def test_refuses_a_tag_that_mismatches_the_package_version(self):
        """The gate fails loud if the checked-out tag != the package's declared
        version, so GET /version can never disagree with the deployed tag
        (epic #100 postmortem: a tag cut without a version bump served silently
        under the wrong label)."""
        assert 'v$INSTALLED" != "$TAG"' in _GATE
        assert "does not match package version" in _GATE
        assert "exit 1" in _GATE


class TestReleaseVersionConsistency:
    """Static, CI-time defense against tag/version drift: pyproject and the lock
    must agree on the package's own version. Complements the deploy-gate check
    (which additionally binds the git tag) by catching drift before a tag exists."""

    def _pyproject_version(self) -> str:
        import tomllib

        data = tomllib.loads((_ROOT / "pyproject.toml").read_text())
        return data["project"]["version"]

    def _lock_version(self) -> str:
        import tomllib

        lock = tomllib.loads((_ROOT / "uv.lock").read_text())
        pkg = next(p for p in lock["package"] if p["name"] == "views-crafdapi")
        return pkg["version"]

    def test_pyproject_and_lock_declare_the_same_version(self):
        assert self._pyproject_version() == self._lock_version(), (
            "pyproject.toml and uv.lock disagree on the package version — run `uv lock` "
            "after bumping the version, or a --frozen deploy will drift"
        )


class TestBootstrap:
    def test_creates_the_dedicated_service_account(self):
        assert "useradd -m -s /bin/bash" in _BOOTSTRAP
        assert 'SVC_USER="views-crafdapi-deploy"' in _BOOTSTRAP

    def test_deploy_key_is_read_only_by_instruction(self):
        assert "READ-ONLY deploy key" in _BOOTSTRAP

    def test_preserves_the_legacy_unit_as_rollback(self):
        assert "views-crafdapi-legacy.service" in _BOOTSTRAP

    # þing-01 #275 / PLATFORM-001 D2: the credential origin moved off the laptop `.env`.
    def test_credentials_no_longer_copied_from_a_personal_env(self):
        # the retired copy-chain: no personal home path, no grep-from-a-SOURCE_ENV origin.
        assert "/home/sonja" not in _BOOTSTRAP
        assert "SOURCE_ENV" not in _BOOTSTRAP

    def test_coordinates_read_from_the_owned_registry(self):
        assert "coordinate_registry.toml" in _BOOTSTRAP
        assert "registry_to_env.py" in _BOOTSTRAP  # read + emit, never copy the file

    def test_secret_comes_from_an_operator_slot_fail_loud(self):
        # the one secret is required from the environment (operator slot), never a .env.
        assert "APPWRITE_DATASTORE_API_KEY:?" in _BOOTSTRAP
        assert "chmod 600" in _BOOTSTRAP


class TestRegistryToEnv:
    """þing-01 #275: the registry→coordinates extraction emits non-secret coordinates only."""

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
note = "no value — a slot"

[excluded.APPWRITE_CRAFD_APPROVED_FILE_IDS]
class = "policy"
"""

    def _coords(self, tmp_path):
        import importlib.util

        reg = tmp_path / "registry.toml"
        reg.write_text(self._FIXTURE)
        spec = importlib.util.spec_from_file_location(
            "registry_to_env", _ROOT / "deployment" / "registry_to_env.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.coordinates(str(reg))

    def test_emits_connection_and_target_coordinates(self, tmp_path):
        lines = self._coords(tmp_path)
        assert "APPWRITE_ENDPOINT=https://example.appwrite.io/v1" in lines
        assert "APPWRITE_CRAFD_BUCKET_ID=unfao_bucket" in lines

    def test_never_emits_a_secret_or_an_exclusion(self, tmp_path):
        blob = "\n".join(self._coords(tmp_path))
        # a secret slot has no value; it must never appear, by name or otherwise.
        assert "APPWRITE_DATASTORE_API_KEY" not in blob
        # eligibility exclusions are governed elsewhere and are not coordinates.
        assert "APPWRITE_CRAFD_APPROVED_FILE_IDS" not in blob
