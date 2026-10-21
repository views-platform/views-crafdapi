"""Epic #184 (S3/S4): the deployment-as-code artifacts must not drift silently.

The systemd unit, the deploy-gate script, and the bootstrap are production
configuration living in the repo. These golden-string tests pin their
load-bearing lines — the same pattern as the wire-contract guard tests: a
change to any pinned value is a deliberate decision, not a refactor casualty.
"""

import json
import re
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

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
_HEALTH_KEYS_THE_MONITOR_READS = (
    "status", "appwrite_connected", "forecast_freshness", "is_stale", "age_days", "created_at",
)


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


_STEPS = {s["name"]: s for s in yaml.safe_load(_FRESHNESS)["jobs"]["freshness"]["steps"]}
_ENV = yaml.safe_load(_FRESHNESS)["env"]


def _assess_script() -> str:
    """The judgement the workflow runs, as it runs it: the `assess.py` heredoc of the Assess
    step, read through YAML so a re-indent of the workflow cannot break the extraction."""
    run = _STEPS["Assess"]["run"]
    marker = "cat > assess.py <<'PY'\n"
    assert marker in run and "\nPY\n" in run, "the Assess step no longer writes assess.py"
    return run.split(marker, 1)[1].split("\nPY\n", 1)[0]


def _outputs(text):
    """Parse GITHUB_OUTPUT text: `key=value` lines and `key<<EOF ... EOF` blocks."""
    lines, outputs = iter(text.splitlines()), {}
    for line in lines:
        if line.endswith("<<EOF"):
            outputs[line[:-5]] = "\n".join(iter(lines.__next__, "EOF"))
        elif "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return outputs


def _assess(tmp_path, health, today):
    """Run the workflow's judgement on a `/health` body, on a given day; return its outputs."""
    (tmp_path / "health.json").write_text(json.dumps(health) if not isinstance(health, str) else health)
    (tmp_path / "assess.py").write_text(_assess_script())
    out = subprocess.run(
        [sys.executable, "assess.py"], cwd=tmp_path, capture_output=True, text=True, check=True,
        env={**os.environ, "FRESHNESS_TODAY": today, "DELIVERY_DAY": _ENV["DELIVERY_DAY"]},
    ).stdout
    return _outputs(out)


def _run_step(tmp_path, name, env, open_issues=()):
    """Run one workflow step's bash under a stand-in `gh` that records every call. Its `issue
    list` behaves like GitHub's keyword search, returning every one of `open_issues` (titles,
    numbered from 900), and applies the step's own `--jq` filter with the real `jq`, so the
    step's exact-title matching is what is tested. Returns (outputs, gh calls)."""
    if shutil.which("jq") is None:
        pytest.skip("jq is needed to apply the workflow's own --jq filter")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    issues = [{"number": 900 + i, "title": title} for i, title in enumerate(open_issues)]
    (tmp_path / "issues.json").write_text(json.dumps(issues))
    (bindir / "gh").write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        with open({str(tmp_path / "calls.log")!r}, "a") as f:
            f.write(json.dumps(sys.argv[1:]) + "\\n")
        if sys.argv[1:3] == ["issue", "list"]:
            import subprocess
            jq = sys.argv[sys.argv.index("--jq") + 1]
            listed = open({str(tmp_path / "issues.json")!r}).read()
            print(subprocess.run(["jq", "-r", jq], input=listed, capture_output=True,
                                 text=True, check=True).stdout, end="")
        elif sys.argv[1:3] == ["issue", "create"]:
            print("https://github.com/x/y/issues/999")
        """))
    (bindir / "gh").chmod(0o755)
    output = tmp_path / "out.txt"
    output.write_text("")
    subprocess.run(
        ["bash", "-c", _STEPS[name]["run"]], cwd=tmp_path, check=True, capture_output=True, text=True,
        env={**os.environ, **{k: str(v) for k, v in _ENV.items()}, **env,
             "PATH": f"{bindir}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output)},
    )
    log = tmp_path / "calls.log"
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return _outputs(output.read_text()), calls


def _health(created_at, *, stale=False, age=10.0, status="healthy"):
    return {
        "status": status,
        "appwrite_connected": True,
        "forecast_freshness": {
            "sla_days": 45.0, "created_at": created_at, "age_days": age, "is_stale": stale,
        },
    }


class TestTheAlarmHasAnOwnerAndADeadline:
    """#173. The first alarm opened an unassigned issue on day 45 and commented daily; #153
    then sat three weeks with nobody acting. These run the workflow's own steps."""

    @pytest.mark.parametrize("created_at, today, due, since", [
        # the 2026-08-14 delivery, seen on 2026-10-21: September's delivery was missed
        ("2026-08-14T18:35:54.962+00:00", "2026-10-21", "true", "2026-09-22"),
        # a delivery made on the 22nd itself satisfies that month
        ("2026-10-22T09:00:00+00:00", "2026-10-22", "false", "2026-10-22"),
        # a run from before the 22nd is due again once the 22nd has passed
        ("2026-10-01T09:00:00+00:00", "2026-10-23", "true", "2026-10-22"),
        # ...but not before it
        ("2026-10-01T09:00:00+00:00", "2026-10-21", "false", "2026-09-22"),
        # January looks back to December
        ("2026-12-01T00:00:00Z", "2027-01-05", "true", "2026-12-22"),
        # judged in UTC: 23:30 at -02:00 on the 21st is the 22nd in UTC, so on time
        ("2026-10-21T23:30:00-02:00", "2026-10-23", "false", "2026-10-22"),
    ])
    def test_a_delivery_is_due_once_the_served_run_predates_the_last_delivery_day(
            self, tmp_path, created_at, today, due, since):
        out = _assess(tmp_path, _health(created_at), today)
        assert (out["due"], out["due_since"]) == (due, since)

    def test_the_delivery_day_is_the_one_the_runbook_states(self):
        """The workflow's day and MONTHLY_REFRESH.md's must move together."""
        runbook = (_ROOT / "deployment" / "MONTHLY_REFRESH.md").read_text()
        assert f"on or after the {_ENV['DELIVERY_DAY']}nd" in runbook

    def test_a_fresh_run_is_neither_degraded_nor_due(self, tmp_path):
        out = _assess(tmp_path, _health("2026-10-23T00:00:00Z"), "2026-10-25")
        assert (out["degraded"], out["due"], out["stale"]) == ("false", "false", "false")

    def test_a_stale_run_is_degraded_and_stale_with_its_age(self, tmp_path):
        out = _assess(tmp_path, _health("2026-08-14T00:00:00Z", stale=True, age=66.0,
                                        status="degraded"), "2026-10-21")
        assert (out["degraded"], out["stale"], out["age_days"]) == ("true", "true", "66.0")
        assert "past its 45.0-day SLA" in out["problems"]

    @pytest.mark.parametrize("freshness", [
        {"error": "freshness check failed"},                                         # api.py's C-264 shape
        {"sla_days": 45.0, "created_at": None, "age_days": None, "is_stale": None},  # no date
        "not a dict",                                                                # malformed
    ])
    def test_an_unmeasured_freshness_is_an_alarm_not_a_pass(self, tmp_path, freshness):
        """The old judgement read `{"error": ...}` as fine, and crashed on `age_days: None`."""
        health = {"status": "healthy", "appwrite_connected": True, "forecast_freshness": freshness}
        out = _assess(tmp_path, health, "2026-10-21")
        assert out["degraded"] == "true" and "unmeasured" in out["problems"]

    def test_an_unparseable_body_is_an_alarm(self, tmp_path):
        out = _assess(tmp_path, "<html>bad gateway</html>", "2026-10-21")
        assert (out["degraded"], out["due"], out["stale"]) == ("true", "false", "false")

    def test_every_issue_it_opens_is_assigned_after_it_exists(self):
        """Assigned in a second call that may fail: an unassignable maintainer must not cost
        the alarm itself."""
        creates = [s for s in _STEPS.values() if "gh issue create" in s.get("run", "")]
        assert len(creates) == 3, [s["name"] for s in creates]   # stale, delivery-due, key rejected
        for step in creates:
            assert "--assignee" not in step["run"], step["name"]
            assert 'gh issue edit "$url" --add-assignee "$MAINTAINER" || echo "::warning::' in step["run"]

    def test_issue_lookups_match_the_exact_title(self, tmp_path):
        """A person's issue that shares the words is neither closed nor mistaken for the alarm."""
        _, calls = _run_step(
            tmp_path, "Close the tracking issue if freshness recovered",
            {"AGE": "3.0", "GH_TOKEN": "x"},
            open_issues=["Is the served data is stale or degraded check right?"],
        )
        assert not any(c[:2] == ["issue", "close"] for c in calls), calls

    @pytest.mark.parametrize("first, again, expected", [
        # a second look, after the load, wins
        ({"degraded": "true", "due": "true", "stale": "true", "age": "66.0"},
         {"degraded": "false", "due": "false", "stale": "false", "age": "0.4"},
         {"degraded": "false", "due": "false", "stale": "false", "age_days": "0.4"}),
        # no second look: the first verdict stands
        ({"degraded": "false", "due": "true", "stale": "false", "age": "29.0"},
         {"degraded": "", "due": "", "stale": "", "age": ""},
         {"degraded": "false", "due": "true", "stale": "false", "age_days": "29.0"}),
    ])
    def test_the_verdict_is_the_last_look(self, tmp_path, first, again, expected):
        env = {"DUE_SINCE": "2026-10-22"}
        for prefix, vals in (("FIRST", first), ("AGAIN", again)):
            env.update({f"{prefix}_DEGRADED": vals["degraded"], f"{prefix}_DUE": vals["due"],
                        f"{prefix}_STALE": vals["stale"], f"{prefix}_AGE": vals["age"],
                        f"{prefix}_PROBLEMS": ""})
        out, _ = _run_step(tmp_path, "Settle the verdict", env)
        assert {k: out[k] for k in expected} == expected

    @pytest.mark.parametrize("due, stale, open_issues, action", [
        ("true", "false", [], "create"),                          # reminder opens once...
        ("true", "false", ["Monthly delivery due"], None),        # ...and is never repeated
        ("true", "true", ["Monthly delivery due"], "close"),      # stale takes over
        ("false", "false", ["Monthly delivery due"], "close"),    # a new run is served
        ("false", "false", [], None),
    ])
    def test_the_reminder_opens_once_and_yields_only_to_staleness(
            self, tmp_path, due, stale, open_issues, action):
        """The reminder keys on staleness, not on any degradation: a store blip must not
        close it one day and open a second the next."""
        _, calls = _run_step(
            tmp_path, "Open, or close, the delivery-due issue",
            {"DUE": due, "STALE": stale, "DUE_SINCE": "2026-10-22", "AGE": "29.0", "GH_TOKEN": "x"},
            open_issues=open_issues,
        )
        verbs = [c[1] for c in calls if c[0] == "issue" and c[1] in ("create", "close", "comment")]
        assert verbs == ([action] if action else []), calls
        assert "DEGRADED" not in _STEPS["Open, or close, the delivery-due issue"]["env"]

    def test_a_bad_verdict_is_judged_again_after_loading_the_run(self):
        """C-337 in views-faoapi: /health reports the SERVED run, so without a load the
        alarm cannot clear itself after a delivery."""
        load = _STEPS["Load the delivered run, then look again"]
        assert load["if"] == "steps.assess.outputs.degraded == 'true'"
        assert "$LOAD_URL" in load["run"] and "-o health2.json" in load["run"]
        assert "mv health2.json health.json" in load["run"]   # a cut-off fetch never replaces it
        assert _STEPS["Assess again"]["run"].startswith("python3 assess.py")
        assert "time_ids=0&entity_ids=0" in _ENV["LOAD_URL"]   # no hardcoded month to go stale

    def test_a_rejected_key_raises_an_alarm_and_fails_the_run(self, tmp_path):
        """C-345 in views-faoapi: an expired key made their monitor exit quietly."""
        assert '[ "$code" = "401" ] || [ "$code" = "403" ]' in _STEPS["Fetch /health"]["run"]
        report = _STEPS["Report a rejected key"]
        assert report["if"] == "steps.fetch.outputs.key_rejected == 'true'"
        assert report["run"].rstrip().endswith("exit 1")
        with pytest.raises(subprocess.CalledProcessError):
            _run_step(tmp_path, "Report a rejected key", {"GH_TOKEN": "x", "RUN_URL": "u"})
        calls = [json.loads(line) for line in (tmp_path / "calls.log").read_text().splitlines()]
        assert ["issue", "create"] == calls[1][:2] and "--add-assignee" in calls[2]

    def test_the_rejected_key_issue_closes_once_the_key_works(self, tmp_path):
        """An alarm that never clears is the ignored alarm this story removes."""
        close = _STEPS["Close the rejected-key issue once the key works"]
        assert close["if"] == "steps.fetch.outputs.fetched == 'true'"
        _, calls = _run_step(tmp_path, "Close the rejected-key issue once the key works",
                             {"GH_TOKEN": "x"},
                             open_issues=["Data-freshness monitor cannot authenticate"])
        assert any(c[:3] == ["issue", "close", "900"] for c in calls), calls


# ---------------------------------------------------------------------------------------------
# The whole job, step by step (#173 guard audit). The step tests above run one step's code; the
# audit found that what joins the steps -- each `if:`, each `${{ steps.X.outputs.Y }}` binding,
# the Fetch and Load bash -- was checked only as text, and 34 of 49 mutations of it survived.
# This runs the job as GitHub would: steps in order, `if:` evaluated, outputs carried, the
# first failing step ending the job, under a stand-in `curl` (a scripted /health) and `gh`
# (an issue tracker that remembers what was created and closed).
# ---------------------------------------------------------------------------------------------

_IF = re.compile(r"^steps\.(\w+)\.outputs\.(\w+) == '(\w*)'$")
_EXPR = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")

_FAKE_CURL = '''#!{python}
import json, sys
args = sys.argv[1:]
state = json.load(open({state!r}))
url = next(a for a in args if a.startswith("http"))
key = "load" if "/data/forecast/subset" in url else "health"
with open({log!r}, "a") as f:
    f.write(json.dumps([key] + args) + "\\n")
queue = state[key]
code, body = queue.pop(0) if len(queue) > 1 else queue[0]
json.dump(state, open({state!r}, "w"))
out = args[args.index("-o") + 1] if "-o" in args else None
fail = any(a.startswith("-") and not a.startswith("--") and "f" in a for a in args)
if "-w" in args:
    sys.stdout.write(args[args.index("-w") + 1].replace("%{{http_code}}", str(code).zfill(3))
                     .replace("%{{time_total}}", "0.1").replace("\\\\n", "\\n"))
if code == 0:
    sys.exit(6)
if fail and code >= 400:
    sys.exit(22)
if out and out != "/dev/null":
    open(out, "w").write(body)
'''

_FAKE_GH = '''#!{python}
import json, subprocess, sys
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
issues = json.load(open({issues!r}))
if args[:2] == ["issue", "list"]:
    jq = args[args.index("--jq") + 1]
    print(subprocess.run(["jq", "-r", jq], input=json.dumps(issues), capture_output=True,
                         text=True, check=True).stdout, end="")
elif args[:2] == ["issue", "create"]:
    n = 900 + len(issues) + 50
    issues.append({{"number": n, "title": args[args.index("--title") + 1]}})
    print(f"https://github.com/x/y/issues/{{n}}")
elif args[:2] == ["issue", "close"]:
    issues = [i for i in issues if str(i["number"]) != args[2]]
json.dump(issues, open({issues!r}, "w"))
'''


def _health_body(created_at="2026-10-23T00:00:00Z", *, age=1.0, stale=False,
                 status="healthy", connected=True):
    return json.dumps({
        "status": status, "appwrite_connected": connected,
        "forecast_freshness": {"sla_days": 45.0, "created_at": created_at,
                               "age_days": age, "is_stale": stale},
    })


_STALE = _health_body("2026-08-14T18:35:54Z", age=66.0, stale=True, status="degraded")
_FRESH = _health_body("2026-10-22T10:00:00Z", age=0.4)


def _run_job(tmp_path, *, health, load=((200, "{}"),), issues=(), today="2026-10-25"):
    """Run the freshness job. `health` is the sequence of (code, body) answers /health gives,
    the last repeating; `issues` the titles already open. Returns (open titles, gh calls,
    curl calls, whether the job failed)."""
    if shutil.which("jq") is None:
        pytest.skip("jq is needed to apply the workflow's own --jq filters")
    bindir, work = tmp_path / "bin", tmp_path / "work"
    bindir.mkdir()
    work.mkdir()
    state, issues_file = tmp_path / "curl.json", tmp_path / "issues.json"
    curl_log, gh_log = tmp_path / "curl.log", tmp_path / "gh.log"
    state.write_text(json.dumps({"health": [list(h) for h in health], "load": [list(x) for x in load]}))
    issues_file.write_text(json.dumps([{"number": 900 + i, "title": t} for i, t in enumerate(issues)]))
    for name, src in (("curl", _FAKE_CURL.format(python=sys.executable, state=str(state), log=str(curl_log))),
                      ("gh", _FAKE_GH.format(python=sys.executable, issues=str(issues_file), log=str(gh_log)))):
        (bindir / name).write_text(src)
        (bindir / name).chmod(0o755)

    context = {"secrets.APPWRITE_DATASTORE_API_KEY": "probe-key", "github.token": "t",
               "github.repository": "o/r", "github.server_url": "https://github.com",
               "github.run_id": "1"}
    outputs: dict = {}

    def resolve(expr):
        m = re.fullmatch(r"steps\.(\w+)\.outputs\.(\w+)", expr)
        if m:
            return outputs.get(m.group(1), {}).get(m.group(2), "")
        assert expr in context, f"the job uses an expression this harness does not model: {expr}"
        return context[expr]

    job_env = {k: _EXPR.sub(lambda m: resolve(m.group(1)), str(v)) for k, v in _ENV.items()}
    failed = False
    for step in yaml.safe_load(_FRESHNESS)["jobs"]["freshness"]["steps"]:
        if "if" in step:
            m = _IF.match(step["if"])
            assert m, f"a condition this harness does not model: {step['if']!r}"
            if outputs.get(m.group(1), {}).get(m.group(2), "") != m.group(3):
                continue
        env = {**os.environ, **job_env, "FRESHNESS_TODAY": today,
               "PATH": f"{bindir}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(tmp_path / "out")}
        env.update({k: _EXPR.sub(lambda m: resolve(m.group(1)), str(v))
                    for k, v in (step.get("env") or {}).items()})
        (tmp_path / "out").write_text("")
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                                cwd=work, env=env, capture_output=True, text=True)
        if "id" in step:
            outputs[step["id"]] = _outputs((tmp_path / "out").read_text())
        if result.returncode != 0:
            failed = True
            break
    def read(log):
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return ([i["title"] for i in json.loads(issues_file.read_text())], read(gh_log), read(curl_log), failed)


_DECOYS = [  # a person's issues that CONTAIN each alarm title; the bot must never touch them
    "Served data is stale or degraded — why?",
    "Monthly delivery due date: is the 22nd right?",
    "Data-freshness monitor cannot authenticate (old notes)",
]


def _touched(gh_calls, number):
    return [c for c in gh_calls if c[:2] in (["issue", "close"], ["issue", "comment"], ["issue", "edit"])
            and c[2] == str(number)]


class TestTheWholeJob:
    """The job end to end. Each case names the behaviour a mutation in the audit broke."""

    def test_a_healthy_day_opens_nothing_and_touches_no_person_s_issue(self, tmp_path):
        titles, gh, _, failed = _run_job(tmp_path, health=[(200, _FRESH)], issues=_DECOYS)
        assert not failed and titles == _DECOYS
        assert not [c for c in gh if c[:2] in (["issue", "create"], ["issue", "close"], ["issue", "comment"])]

    def test_a_stale_forecast_opens_the_stale_issue_assigned_and_no_reminder(self, tmp_path):
        titles, gh, curl, failed = _run_job(tmp_path, health=[(200, _STALE)], issues=_DECOYS)
        assert not failed
        assert titles == _DECOYS + ["Served data is stale or degraded"]
        assert any(c[:2] == ["issue", "edit"] and "--add-assignee" in c for c in gh)
        load = [c for c in curl if c[0] == "load"]
        assert len(load) == 1 and int(load[0][load[0].index("--max-time") + 1]) >= 300

    def test_an_open_stale_issue_is_reassigned_and_commented_not_duplicated(self, tmp_path):
        titles, gh, _, _ = _run_job(tmp_path, health=[(200, _STALE)],
                                    issues=["Served data is stale or degraded"] + _DECOYS)
        assert titles.count("Served data is stale or degraded") == 1
        assert [c[1] for c in _touched(gh, 900)] == ["edit", "comment"]
        for n in range(901, 904):
            assert not _touched(gh, n)

    def test_a_delivery_landing_clears_both_issues_after_the_load(self, tmp_path):
        """The first look is stale; the load brings the new run; the second look wins."""
        titles, _, _, failed = _run_job(
            tmp_path, health=[(200, _STALE), (200, _FRESH)],
            issues=["Served data is stale or degraded", "Monthly delivery due"] + _DECOYS)
        assert not failed and titles == _DECOYS

    def test_the_reminder_opens_on_the_delivery_day_once(self, tmp_path):
        due = _health_body("2026-09-24T00:00:00Z", age=29.0)
        titles, gh, _, _ = _run_job(tmp_path, health=[(200, due)], issues=_DECOYS, today="2026-10-22")
        assert titles == _DECOYS + ["Monthly delivery due"]
        assert any(c[:2] == ["issue", "edit"] and "--add-assignee" in c for c in gh)
        (tmp_path / "next").mkdir()
        again, gh2, _, _ = _run_job(tmp_path / "next", health=[(200, due)], today="2026-10-23",
                                    issues=["Monthly delivery due"])
        assert again == ["Monthly delivery due"] and not [c for c in gh2 if c[1] in ("create", "close", "comment")]

    @pytest.mark.parametrize("body", [
        # a run from before the 22nd, so the reminder is legitimately open
        _health_body("2026-10-01T00:00:00Z", age=24.0, status="degraded"),   # status alone
        _health_body("2026-10-01T00:00:00Z", age=24.0, connected=None),      # store state unknown
    ])
    def test_a_degradation_other_than_staleness_alarms_but_leaves_the_reminder(self, tmp_path, body):
        titles, _, _, _ = _run_job(tmp_path, health=[(200, body)],
                                   issues=["Monthly delivery due"], today="2026-10-25")
        assert titles == ["Monthly delivery due", "Served data is stale or degraded"]

    def test_a_rejected_key_opens_its_issue_and_fails_the_job(self, tmp_path):
        titles, gh, _, failed = _run_job(tmp_path, health=[(401, '{"detail":"no"}')], issues=_DECOYS)
        assert failed
        assert titles == _DECOYS + ["Data-freshness monitor cannot authenticate"]
        assert any(c[:2] == ["issue", "edit"] and "--add-assignee" in c for c in gh)

    def test_the_rejected_key_issue_closes_once_the_key_works(self, tmp_path):
        titles, _, _, _ = _run_job(tmp_path, health=[(200, _FRESH)],
                                   issues=["Data-freshness monitor cannot authenticate"] + _DECOYS)
        assert titles == _DECOYS

    @pytest.mark.parametrize("answer", [(0, ""), (503, "<html>bad gateway</html>")])
    def test_an_unreachable_service_opens_nothing(self, tmp_path, answer):
        """Availability is Better Stack's; a duplicate alarm teaches people to ignore both."""
        titles, gh, _, failed = _run_job(tmp_path, health=[answer], issues=_DECOYS)
        assert not failed and titles == _DECOYS
        assert not [c for c in gh if c[:2] != ["issue", "list"]]

    @pytest.mark.parametrize("second", [(503, "<html>bad gateway</html>"), (0, "")])
    def test_a_failed_second_look_keeps_the_first_verdict(self, tmp_path, second):
        titles, gh, _, _ = _run_job(tmp_path, health=[(200, _STALE), second])
        assert titles == ["Served data is stale or degraded"]
        body = next(c for c in gh if c[:2] == ["issue", "create"])
        assert "66.0 days old" in body[body.index("--body") + 1]

    @pytest.mark.parametrize("created_at", ["not a date", "2026-10-23T00:00:00"])
    def test_an_odd_created_at_never_crashes_the_job(self, tmp_path, created_at):
        titles, _, _, failed = _run_job(tmp_path, health=[(200, _health_body(created_at))])
        assert not failed and titles == []

    def test_staleness_is_the_service_s_verdict_not_a_second_threshold(self, tmp_path):
        just_stale = _health_body("2026-09-08T00:00:00Z", age=46.0, stale=True, status="degraded")
        titles, _, _, _ = _run_job(tmp_path, health=[(200, just_stale)], issues=["Monthly delivery due"])
        assert titles == ["Served data is stale or degraded"]     # escalated, reminder closed


class TestTheJobsFixedPoints:
    """Values that, changed, quietly break the alarm without failing any behaviour above."""

    def test_it_fires_daily(self):
        assert yaml.safe_load(_FRESHNESS)[True]["schedule"] == [{"cron": "15 7 * * *"}]

    def test_the_issue_titles_continue_the_ones_already_open(self):
        """#153 is open under this title; a rename would orphan it and open a second."""
        assert _ENV["STALE_TITLE"] == "Served data is stale or degraded"

    def test_the_maintainer_is_the_github_login_that_owns_the_alarm(self):
        """`Polichinel` is the maintainer's login; `Polichinl` (the git author name) is not a
        GitHub user, and an unassignable login would leave every alarm unowned."""
        assert _ENV["MAINTAINER"] == "Polichinel"

    def test_the_load_request_names_a_real_route_and_feature(self):
        api = (_ROOT / "src" / "views_crafdapi" / "managers" / "api.py").read_text()
        smoke = (_ROOT / "src" / "views_crafdapi" / "smoke.py").read_text()
        path, _, query = _ENV["LOAD_URL"].removeprefix("https://crafdapi.viewsforecasting.org").partition("?")
        assert f'"{path}"' in api
        feature = dict(kv.split("=") for kv in query.split("&"))["features"]
        assert f'"{feature}"' in smoke     # the feature the smoke test reads from production


class TestTheSecondLookIsTheOneActedOn:
    """After the load, every later step must act on the second look, not the first. Each case
    is a first/second pair where the two disagree, so reading the wrong one shows."""

    def test_a_delivery_before_the_day_reopens_the_reminder_not_the_alarm(self, tmp_path):
        early = _health_body("2026-10-20T00:00:00Z", age=5.0)     # fresh, but before the 22nd
        titles, _, _, _ = _run_job(tmp_path, health=[(200, _STALE), (200, early)], today="2026-10-25")
        assert titles == ["Monthly delivery due"]

    def test_a_recovered_second_look_never_comments_on_the_alarm(self, tmp_path):
        _, gh, _, _ = _run_job(tmp_path, health=[(200, _STALE), (200, _FRESH)],
                               issues=["Served data is stale or degraded"])
        assert [c[1] for c in _touched(gh, 900)] == ["close"]

    def test_a_degraded_but_fresh_first_look_is_also_looked_at_again(self, tmp_path):
        blip = _health_body("2026-10-23T00:00:00Z", status="degraded")
        titles, _, curl, _ = _run_job(tmp_path, health=[(200, blip), (200, _FRESH)])
        assert titles == [] and [c[0] for c in curl].count("health") == 2

    def test_the_alarm_reports_the_second_look_s_problems(self, tmp_path):
        later = _health_body("2026-08-14T18:35:54Z", age=66.5, stale=True, status="degraded")
        _, gh, _, _ = _run_job(tmp_path, health=[(200, _STALE), (200, later)])
        body = next(c for c in gh if c[:2] == ["issue", "create"])
        assert "66.5 days old" in body[body.index("--body") + 1]

    def test_the_reminder_names_its_day(self, tmp_path):
        due = _health_body("2026-09-24T00:00:00Z", age=29.0)
        _, gh, _, _ = _run_job(tmp_path, health=[(200, due)], today="2026-10-22")
        body = next(c for c in gh if c[:2] == ["issue", "create"])
        assert "2026-10-22" in body[body.index("--body") + 1]


class TestTheDateIsUTCWhereverTheRunnerIs:
    """GitHub's runners are UTC today; the judgement must not depend on that."""

    @pytest.mark.parametrize("tz", ["Pacific/Kiritimati", "Etc/GMT+12"])  # UTC+14 and UTC-12
    def test_today_is_the_utc_date(self, tmp_path, tz):
        from datetime import datetime, timezone
        (tmp_path / "health.json").write_text(_health_body("2026-01-01T00:00:00Z"))
        (tmp_path / "assess.py").write_text(_assess_script())
        env = {k: v for k, v in os.environ.items() if k != "FRESHNESS_TODAY"}
        before = datetime.now(timezone.utc).date()
        out = _outputs(subprocess.run(
            [sys.executable, "assess.py"], cwd=tmp_path, capture_output=True, text=True, check=True,
            env={**env, "TZ": tz, "DELIVERY_DAY": _ENV["DELIVERY_DAY"]}).stdout)
        after = datetime.now(timezone.utc).date()
        day = int(_ENV["DELIVERY_DAY"])

        def last_delivery_day(d):
            if d.day >= day:
                return d.replace(day=day)
            return (d.replace(year=d.year - 1, month=12, day=day) if d.month == 1
                    else d.replace(month=d.month - 1, day=day))

        assert out["due_since"] in {last_delivery_day(before).isoformat(), last_delivery_day(after).isoformat()}

    def test_a_date_without_an_offset_is_read_as_utc_not_runner_local(self, tmp_path):
        """23:30 on the 21st, unmarked, is the 21st in UTC, so the run predates the 22nd. Read
        as UTC-12 local time it would become the 22nd and wrongly count as on time."""
        (tmp_path / "health.json").write_text(_health_body("2026-10-21T23:30:00"))
        (tmp_path / "assess.py").write_text(_assess_script())
        out = _outputs(subprocess.run(
            [sys.executable, "assess.py"], cwd=tmp_path, capture_output=True, text=True, check=True,
            env={**os.environ, "TZ": "Etc/GMT+12", "FRESHNESS_TODAY": "2026-10-23",
                 "DELIVERY_DAY": _ENV["DELIVERY_DAY"]}).stdout)
        assert out["due"] == "true"
