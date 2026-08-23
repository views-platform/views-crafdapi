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
_FRESHNESS = (_ROOT / ".github" / "workflows" / "data-freshness.yml").read_text()


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


class TestRestartLoopIsBounded:
    """The deploy gate's refusals are deterministic, so an unbounded retry retries forever.

    `Restart=always` + `RestartSec=5` against a tag that does not exist on origin, or a
    tag/version mismatch, means the service 502s indefinitely while `systemctl status` reports
    `activating` rather than `failed` — the shape of the 47-minute incident on 2026-08-15.
    """

    def _unit(self):
        import configparser
        cfg = configparser.ConfigParser(strict=False)
        cfg.optionxform = str
        cfg.read_string(_UNIT)
        return cfg

    def test_start_limit_directives_are_in_the_unit_section_not_service(self):
        """systemd >= 229 reads these from [Unit]. Under [Service] they are IGNORED — the
        hardening would be present in the file and absent in effect, which is worse than
        missing. Ubuntu 24.04 ships systemd 255."""
        cfg = self._unit()
        for key in ("StartLimitIntervalSec", "StartLimitBurst"):
            assert cfg.has_option("Unit", key), f"{key} must be under [Unit]"
            assert not cfg.has_option("Service", key), (
                f"{key} is under [Service], where systemd >= 229 ignores it silently"
            )

    def test_the_retry_budget_is_finite(self):
        cfg = self._unit()
        assert int(cfg.get("Unit", "StartLimitBurst")) > 0
        assert int(cfg.get("Unit", "StartLimitIntervalSec")) > 0

    def test_start_has_a_timeout(self):
        """Without this a hung `uv sync`/`git fetch` in ExecStartPre blocks forever, and the
        burst limit never trips because no attempt ever completes."""
        cfg = self._unit()
        assert cfg.has_option("Service", "TimeoutStartSec"), (
            "ExecStartPre does network I/O; an untimed start can hang indefinitely"
        )
        assert int(cfg.get("Service", "TimeoutStartSec")) > 0


class TestMemoryCeiling:
    """C-262/#99. The ceiling must be derivable from the measured COLD START, not the steady
    state — sizing it from the latter is what C-263 exists to prevent.

    Measured 2026-08-21 (v0.5.1): cold peak 7.3 G, steady 3.7 G, box 22 GiB.
    """

    COLD_PEAK_G = 7.3
    BOX_G = 22            # `free -g` on the box, 2026-08-21 — NOT the 24 GB views-faoapi#368 assumed
    NEIGHBOUR_MAX_G = 11  # views-faoapi/deployment/views-faoapi.service
    OS_RESERVE_G = 2      # OS + nginx + page cache; the box has no swap, so this is not optional

    def _unit(self):
        import configparser
        cfg = configparser.ConfigParser(strict=False)
        cfg.optionxform = str
        cfg.read_string(_UNIT)
        return cfg

    @staticmethod
    def _gb(value: str) -> float:
        assert value.endswith("G"), f"expected a G-suffixed size, got {value!r}"
        return float(value[:-1])

    def test_a_ceiling_is_declared_in_the_unit_file(self):
        """Not via `set-property --runtime`, which lives in /run and vanishes on reboot — a
        ceiling nobody can read from the repo is what this replaced."""
        cfg = self._unit()
        assert cfg.has_option("Service", "MemoryMax"), "no MemoryMax in the unit file"
        assert cfg.has_option("Service", "MemoryHigh"), (
            "no MemoryHigh — #99 asks for back-pressure before the hard kill, which is a "
            "materially different failure than a cliff for a once-per-restart transient"
        )

    def test_the_ceiling_clears_the_measured_cold_start(self):
        """The trap C-263 documents: a ceiling above the steady state (3.7 G) but below the cold
        start (7.3 G) kills the service on every single restart, and looks generous until then."""
        mx = self._gb(self._unit().get("Service", "MemoryMax"))
        assert mx > self.COLD_PEAK_G, (
            f"MemoryMax={mx}G is at or below the measured cold-start peak "
            f"{self.COLD_PEAK_G}G — the service would be killed on every restart"
        )
        assert mx >= self.COLD_PEAK_G * 1.2, (
            f"MemoryMax={mx}G leaves under 20% headroom over the measured peak. The margin is "
            f"this tight because the box is shared — see the co-tenancy test below, which is "
            f"what caps it from the other side."
        )

    def test_the_pair_of_ceilings_fits_the_box(self):
        """The constraint that actually sets our number. views-faoapi#368 sized its pair from a
        24 GB box; `free -g` says 22 GiB, so 11 + 11 leaves nothing for the OS — and with no swap
        that is an immediate OOM, which is precisely what the ceilings exist to prevent."""
        mx = self._gb(self._unit().get("Service", "MemoryMax"))
        total = mx + self.NEIGHBOUR_MAX_G + self.OS_RESERVE_G
        assert total <= self.BOX_G, (
            f"MemoryMax={mx}G + views-faoapi {self.NEIGHBOUR_MAX_G}G + {self.OS_RESERVE_G}G OS "
            f"reserve = {total}G, over the {self.BOX_G} GiB box. Both services at their ceiling "
            f"must not be able to exhaust the machine."
        )

    def test_high_is_below_max_and_above_the_cold_start(self):
        """A MemoryHigh at or below the real peak throttles every cold start and reads as a
        fault rather than as margin."""
        cfg = self._unit()
        hi, mx = self._gb(cfg.get("Service", "MemoryHigh")), self._gb(cfg.get("Service", "MemoryMax"))
        assert hi < mx, f"MemoryHigh={hi}G must be below MemoryMax={mx}G"
        assert hi > self.COLD_PEAK_G, (
            f"MemoryHigh={hi}G is at or below the measured cold peak {self.COLD_PEAK_G}G — "
            f"back-pressure would apply on every normal restart"
        )

    def test_the_ceiling_leaves_the_neighbour_room_on_the_shared_box(self):
        """C-262 is about co-tenancy: this box also runs views-faoapi (4.8 G observed), and the
        original 16.8 G cold start left -0.7 GiB of headroom."""
        mx = self._gb(self._unit().get("Service", "MemoryMax"))
        assert mx <= self.BOX_G * 0.6, (
            f"MemoryMax={mx}G takes more than 60% of the {self.BOX_G} GiB box, leaving too "
            f"little for views-faoapi, the OS and page cache"
        )


# The keys `data-freshness.yml` reads out of the `/health` body. Renaming one in
# `api.py` without renaming it here leaves the monitor parsing a field that no longer
# exists — it would report healthy forever while inspecting nothing, which is the exact
# failure the workflow's own comment warns about.
_HEALTH_KEYS_THE_MONITOR_READS = ("status", "appwrite_connected", "forecast_freshness", "is_stale")


class TestDataFreshnessMonitor:
    """The daily poller that answers "is the served data still current?" — the question
    Better Stack's /ping monitor cannot. It has no tests of its own (it is YAML GitHub
    runs), so what is pinned here is the one thing that would silently disable it:
    drift between the keys it parses and the keys `/health` emits."""

    def test_the_monitor_polls_health_not_ping(self):
        assert "https://crafdapi.viewsforecasting.org/health" in _FRESHNESS
        assert "/ping" not in _FRESHNESS, (
            "this workflow must not duplicate Better Stack's liveness check — two alarms "
            "for one event teach people to ignore both"
        )

    def test_it_runs_on_a_schedule_and_can_be_triggered_by_hand(self):
        assert "schedule:" in _FRESHNESS and "cron:" in _FRESHNESS
        assert "workflow_dispatch:" in _FRESHNESS

    def test_it_does_not_open_an_issue_when_the_fetch_fails(self):
        """An unreachable service is an availability problem Better Stack already owns."""
        assert "fetched=false" in _FRESHNESS
        assert "if: steps.fetch.outputs.fetched == 'true'" in _FRESHNESS

    def test_it_sends_the_api_key_and_refuses_to_run_without_it(self):
        """`/health` requires X-API-Key. Polling it without one returns 422, which carries no
        `status` — a monitor that carried on regardless would open a false alarm every day
        until muted, which is worse than no monitor. It fails the run instead."""
        assert "X-API-Key: ${KEY}" in _FRESHNESS
        assert "secrets.APPWRITE_DATASTORE_API_KEY" in _FRESHNESS
        assert 'exit 1' in _FRESHNESS

    def test_gh_repo_is_set_because_there_is_no_checkout(self):
        """views-datafactory's equivalent failed 10 runs out of 10 without this."""
        assert "GH_REPO:" in _FRESHNESS
        assert "actions/checkout" not in _FRESHNESS

    @pytest.mark.parametrize("key", _HEALTH_KEYS_THE_MONITOR_READS)
    def test_the_workflow_names_every_health_key_it_depends_on(self, key):
        assert f'"{key}"' in _FRESHNESS or f"'{key}'" in _FRESHNESS or f"`{key}`" in _FRESHNESS

    # The binding half — that `/health` actually emits these — lives in
    # test_api_endpoints.py, where the `app_client` fixture already is.
