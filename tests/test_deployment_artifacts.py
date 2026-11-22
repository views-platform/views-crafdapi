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
import time
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


def _exec_start(unit: str) -> str:
    """The ExecStart command as systemd reads it: continued while a line ends in a backslash,
    skipping comment lines (`#` or `;`) inside it."""
    lines = unit.split("\n")
    i = next(n for n, line in enumerate(lines) if line.startswith("ExecStart="))
    parts, line = [], lines[i][len("ExecStart="):]
    while True:
        parts.append(line.rstrip().rstrip("\\"))
        if not line.rstrip().endswith("\\"):
            return " ".join(parts)
        i += 1
        while lines[i].lstrip().startswith(("#", ";")):
            i += 1
        line = lines[i]


class TestSystemdUnit:
    def test_runs_as_the_service_account(self):
        assert "User=views-crafdapi-deploy" in _UNIT

    def test_start_passes_the_deploy_gate(self):
        assert "ExecStartPre=" in _UNIT and "checkout-deploy-tag.sh" in _UNIT

    def test_serves_the_factory_app_on_localhost_8001(self):
        command = _exec_start(_UNIT)
        assert "views_crafdapi.managers.api:create_app" in command
        assert "--factory" in command
        # #236: open requests get a limited time, so the lifespan shutdown always runs, and
        # systemd's stop limit covers the whole stop.
        # The flag must be on the ExecStart command itself (continuation lines joined), not in a
        # comment.
        graceful = re.findall(r"--timeout-graceful-shutdown[ =](\S+)", command)
        assert graceful == ["30"]
        # systemd takes the last TimeoutStopSec line, so there must be exactly one, in seconds.
        # systemd reads it only in the [Service] section; anywhere else it is ignored.
        service = re.search(r"^\[Service\]$(.*?)(?=^\[|\Z)", _UNIT, re.M | re.S)[1]
        stops = re.findall(r"^\s*TimeoutStopSec\s*=(.*)$", _UNIT, re.M)
        assert len(stops) == 1 and stops[0].strip().isdigit()
        assert re.search(r"^\s*TimeoutStopSec\s*=", service, re.M)
        # Settings that would override that limit, or stop the service some way other than one
        # SIGTERM to uvicorn (which runs the lifespan shutdown), must not be in the unit.
        for setting in ("TimeoutSec", "KillSignal", "RestartKillSignal", "FinalKillSignal",
                        "KillMode", "ExecStop", "SendSIGKILL", "SendSIGHUP"):
            assert not re.search(rf"^\s*{setting}\s*=", _UNIT, re.M), setting
        from views_crafdapi.managers.api import CrafdApiManager
        assert int(stops[0]) >= int(graceful[0]) + CrafdApiManager._STOP_WAIT_SECONDS + 10
        assert re.findall(r"--host[ =](\S+)", command) == ["127.0.0.1"]  # nginx upstream contract — never 0.0.0.0
        # Port 8001, NOT 8000: crafdapi co-hosts with faoapi (:8000) on the same box;
        # a shared port would make the two services' units collide (address in use).
        assert re.findall(r"--port[ =](\S+)", command) == ["8001"]

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

    def test_requires_an_explicit_release_tag(self):
        assert 'RELEASE_TAG="${RELEASE_TAG:-}"' in _BOOTSTRAP
        assert "v0.1.0" not in _BOOTSTRAP

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
    """The memory limits must clear the measured start-up peak and leave the system room.

    Measured 2026-08-21 (v0.5.1): peak at start 7.3 G. The service runs alone on a 32 GB server
    (Hetzner CCX33, since November 2026); no other service shares it.
    """

    COLD_PEAK_G = 7.3
    BOX_G = 32
    OS_RESERVE_G = 4      # OS, nginx, page cache, and room to log in over ssh

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

    def test_a_hard_ceiling_and_no_soft_one(self):
        """MemoryHigh without swap slows the service to a crawl while it still looks running;
        a clean stop at MemoryMax is restarted by systemd (review 2026-11-18)."""
        cfg = self._unit()
        assert cfg.has_option("Service", "MemoryMax"), "no MemoryMax in the unit file"
        assert not cfg.has_option("Service", "MemoryHigh"), "MemoryHigh is back in the unit"
        assert cfg.get("Service", "MemorySwapMax") == "0", "MemorySwapMax=0 is missing"

    def test_the_ceiling_clears_the_measured_cold_start(self):
        """A limit above everyday use but below the start-up peak kills the service on every
        restart (C-263)."""
        mx = self._gb(self._unit().get("Service", "MemoryMax"))
        assert mx >= self.COLD_PEAK_G * 1.2, (
            f"MemoryMax={mx}G leaves under 20% headroom over the measured start-up peak "
            f"{self.COLD_PEAK_G}G"
        )

    def test_the_ceiling_leaves_the_system_room(self):
        mx = self._gb(self._unit().get("Service", "MemoryMax"))
        assert mx + self.OS_RESERVE_G <= self.BOX_G, (
            f"MemoryMax={mx}G + {self.OS_RESERVE_G}G for the system is over the "
            f"{self.BOX_G} GB server"
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
        assert _ENV["LOAD_URL"].endswith("?places=0")   # no hardcoded month to go stale

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
key = "load" if "/slice/" in url else "health"
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

    def test_the_load_request_names_a_real_route_and_outcome(self, tmp_path):
        from fastapi import FastAPI
        from fastapi.routing import APIRoute
        from starlette.routing import Match

        from views_crafdapi.managers.api import CrafdApiManager

        mgr = CrafdApiManager.from_config({}, cache_dir=tmp_path / "cache")
        mgr.app = FastAPI()
        mgr._register_routes()
        smoke = (_ROOT / "src" / "views_crafdapi" / "smoke.py").read_text()
        path, _, _ = _ENV["LOAD_URL"].removeprefix("https://crafdapi.viewsforecasting.org").partition("?")
        scope = {"type": "http", "path": path, "method": "GET"}
        assert any(
            route.matches(scope)[0] == Match.FULL
            for route in mgr.app.routes
            if isinstance(route, APIRoute) and route.include_in_schema   # not FastAPI's own pages
        )
        level, outcome = path.removeprefix("/slice/").split("/")
        assert (level, outcome) == ("grid", "sb")
        assert f'"{outcome}"' in smoke     # the outcome the smoke test reads from production


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


class TestDeployGateWithoutGitHub:
    """#212: a fetch that fails starts from the tag already on the server, and serves exactly that
    tag; only a refusal from origin (a refused key or login, a missing repository) stops the start. The gate runs for real in a scratch repo with a stand-in `uv`, isolated from
    the developer's git settings."""

    UNREACHABLE = "http://127.0.0.1:1/views-crafdapi.git"   # nothing listens on port 1: refused

    @staticmethod
    def _env(tmp_path, extra=None):
        home = tmp_path / "home"
        git_path = shutil.which("git")
        assert git_path, "these tests run the real deploy gate and need git on PATH"
        env = {"HOME": str(home), "PATH": f"{home}/.local/bin:{os.path.dirname(git_path)}:/usr/bin:/bin",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
        env.update(extra or {})
        return env

    @staticmethod
    def _git(repo, env, *args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                               "-c", "commit.gpgsign=false", *args],
                              check=True, capture_output=True, text=True, env=env).stdout.strip()

    @staticmethod
    def _script(tmp_path, name, body):
        path = tmp_path / name
        path.write_text("#!/usr/bin/env bash\n" + body + "\n")
        path.chmod(0o755)
        return str(path)

    def _server(self, tmp_path, tag, origin, version="0.0.1", extra_env=None, git_config=None,
                before_gate=None, offline_sync_fails=False, uv_fails_on=None):
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "scripts" / "checkout-deploy-tag.sh").write_text(_GATE)
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        env = self._env(tmp_path)
        git = lambda *a: self._git(repo, env, *a)   # noqa: E731
        git("init", "-q")
        git("commit", "-q", "--allow-empty", "-m", "tagged")
        git("tag", "v0.0.1")
        tagged = git("rev-parse", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "after the tag")   # HEAD is not on the tag
        git("remote", "add", "origin", origin)
        for key, value in (git_config or {}).items():
            git("config", key, value)
        # `uv sync` does nothing; `uv run ... python -c ...` prints the package version.
        offline = 'if [ "$1" = sync ] && [[ " $* " == *" --offline "* ]]; then echo "no network for package x" >&2; exit 1; fi' \
            if offline_sync_fails else ""
        if uv_fails_on:
            offline += f'\nif [ "$1" = {uv_fails_on} ]; then echo broken >&2; exit 1; fi'
        # Every uv call is recorded, so a test can see whether it ran offline.
        self._script(home / ".local" / "bin", "uv",
                     f'echo "$*" >> {tmp_path}/uv-calls\n{offline}\n'
                     f'if [ "$1" = run ]; then echo "warning: a uv notice" >&2; echo VERSION={version}; '
                     f'echo "warning: printed after the version" >&2; fi')
        tag_file = tmp_path / "tag"
        tag_file.write_text(tag + "\n")
        if before_gate:
            before_gate(repo)
        result = subprocess.run(
            ["bash", str(repo / "scripts" / "checkout-deploy-tag.sh")], capture_output=True, text=True,
            env=self._env(tmp_path, {"CRAFDAPI_DEPLOY_TAG_FILE": str(tag_file), **(extra_env or {})}),
        )
        return result, git("rev-parse", "HEAD"), tagged

    def _origin(self, tmp_path):
        """A reachable origin whose v0.0.1 points at a different commit (a re-cut tag)."""
        origin = tmp_path / "origin"
        origin.mkdir()
        env = self._env(tmp_path)
        self._git(origin, env, "init", "-q")
        self._git(origin, env, "commit", "-q", "--allow-empty", "-m", "re-cut")
        self._git(origin, env, "tag", "v0.0.1")
        return origin, self._git(origin, env, "rev-parse", "HEAD")

    def _served_from_disk(self, result, head, tagged):
        assert result.returncode == 0, result.stderr
        calls = (Path(result.args[1]).parents[2] / "uv-calls").read_text().splitlines()
        assert all("--offline" in c for c in calls), calls     # no network assumed after a failed fetch
        assert "could not fetch tags from origin" in result.stderr
        assert "serving tag v0.0.1" in result.stdout and "origin NOT checked" in result.stdout
        assert head == tagged          # it serves the tag, not whatever was checked out

    def test_a_refused_connection_serves_the_tag_on_the_server(self, tmp_path):
        self._served_from_disk(*self._server(tmp_path, "v0.0.1", self.UNREACHABLE))

    def test_a_name_lookup_failure_serves_the_tag_on_the_server(self, tmp_path):
        """DNS down: the case the gate most needs to survive."""
        result, head, tagged = self._server(tmp_path, "v0.0.1", "http://no-such-host.invalid/repo.git")
        self._served_from_disk(result, head, tagged)
        assert "resolve host" in result.stderr.lower()

    @pytest.mark.parametrize("ssh_says", [
        "Timeout, server github.com not responding.",               # ssh keep-alive gave up
        "kex_exchange_identification: read: Connection reset by peer",
        "Connection closed by 140.82.121.4 port 22",
        "fatal: unable to access 'https://github.com/x/': The requested URL returned error: 503",
        "fatal: unable to access 'https://github.com/x/': The requested URL returned error: 429",
        "fatal: unable to access 'https://github.com/x/': OpenSSL SSL_read: SSL_ERROR_SYSCALL, errno 0",
        "fatal: early EOF",
        "fatal: unable to access 'https://github.com/x/': The requested URL returned error: 408",
        # A bare 403 can be rate limiting: not proof that origin refused.
        "fatal: unable to access 'https://github.com/x/': The requested URL returned error: 403",
    ])
    def test_a_github_side_outage_serves_the_tag_on_the_server(self, tmp_path, ssh_says):
        ssh = self._script(tmp_path, "ssh", f"echo \"{ssh_says}\" >&2\nexit 255")
        result, head, tagged = self._server(tmp_path, "v0.0.1", "ssh://git@github.invalid/repo.git",
                                            extra_env={"GIT_SSH_COMMAND": ssh})
        self._served_from_disk(result, head, tagged)
        assert ssh_says in result.stderr      # git's own words reach the journal

    @pytest.mark.parametrize("origin_says", [
        "git@github.com: Permission denied (publickey).",
        "remote: Permission to x/y.git denied to someone.\nfatal: unable to access 'https://github.com/x/': The requested URL returned error: 403",
        "Received disconnect from 140.82.121.4 port 22:2: Too many authentication failures",
        "ERROR: Repository not found.",
        "fatal: unable to access 'https://github.com/x/': The requested URL returned error: 404",
        "fatal: unable to access 'https://github.com/x/': The requested URL returned error: 401",
        "Host key verification failed.",
        "remote: Invalid username or password.\nfatal: Authentication failed for 'https://github.com/x/'",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "fatal: could not read Password for 'https://user@github.com': terminal prompts disabled",
        "remote: Not Found\nfatal: repository 'https://github.com/x/y.git/' not found",
        "ERROR: Your account is suspended. Please check with your installation administrator.",
        "fatal: 'origin' does not appear to be a git repository",
        # A refusal that also mentions the connection closing is still a refusal.
        "git@github.com: Permission denied (publickey).\nConnection closed by 140.82.121.4 port 22",
    ])
    def test_a_no_from_origin_stops_the_start(self, tmp_path, origin_says):
        """Origin answered and refused: the server could otherwise serve a stale re-cut tag."""
        ssh = self._script(tmp_path, "ssh", f"echo \"{origin_says}\" >&2\nexit 255")
        result, _, _ = self._server(tmp_path, "v0.0.1", "ssh://git@github.invalid/repo.git",
                                    extra_env={"GIT_SSH_COMMAND": ssh})
        assert result.returncode == 1
        assert "origin refused the fetch" in result.stderr
        assert origin_says.splitlines()[0] in result.stderr

    @pytest.mark.parametrize("git_says", [
        "error: index-pack failed\nfatal: early EOF",                  # a transfer cut off
        "fatal: write error: No space left on device",                 # a fault on this server
        "Connection to github.com closed by remote host.",
        "error: RPC failed; curl 56 OpenSSL SSL_read: Connection reset by peer",
        "something no list foresaw",
    ])
    def test_any_failure_that_is_not_a_refusal_serves_the_tag_on_the_server(self, tmp_path, git_says):
        """Only a refusal stops the start; listing every way a fetch can fail does not work."""
        ssh = self._script(tmp_path, "ssh", f"echo \"{git_says}\" >&2\nexit 255")
        result, head, tagged = self._server(tmp_path, "v0.0.1", "ssh://git@github.invalid/repo.git",
                                            extra_env={"GIT_SSH_COMMAND": ssh})
        self._served_from_disk(result, head, tagged)
        assert "deploy-gate: git: " + git_says.splitlines()[0] in result.stderr

    @pytest.mark.parametrize("step", ["sync", "run"])
    def test_a_failing_uv_step_stops_the_start_with_a_fatal_line(self, tmp_path, step):
        """Without a FATAL line the runbook's journal search would find nothing."""
        origin, _ = self._origin(tmp_path)
        result, _, _ = self._server(tmp_path, "v0.0.1", str(origin), uv_fails_on=step)
        assert result.returncode == 1
        assert "FATAL deploy-gate:" in result.stderr and "deploy-gate: uv: broken" in result.stderr
        step_line = {"sync": "syncing the packages", "run": "reading the installed version"}[step]
        assert step_line in result.stderr          # the FATAL line names the step that failed
        assert "does not match package version" not in result.stderr   # it stopped at that step

    def test_offline_a_tag_that_needs_new_packages_stops_the_start_and_says_why(self, tmp_path):
        result, _, _ = self._server(tmp_path, "v0.0.1", self.UNREACHABLE, offline_sync_fails=True)
        assert result.returncode == 1
        assert "synced offline" in result.stderr and "may need packages this server does not have" in result.stderr
        calls = (tmp_path / "uv-calls").read_text().splitlines()
        assert not any(c.startswith("run") for c in calls)    # stopped at the sync, before the version check
        assert "deploy-gate: uv: no network for package x" in result.stderr

    @pytest.mark.parametrize("lock_path", [
        "refs/tags/v0.0.1.lock", "refs/heads/main.lock", "refs/remotes/origin/main.lock",
        "packed-refs.lock", "shallow.lock", "FETCH_HEAD.lock"])
    def test_a_lock_file_left_by_an_earlier_fetch_is_removed(self, tmp_path, lock_path):
        """A fetch killed mid-update leaves a lock that would fail every later fetch."""
        origin, recut = self._origin(tmp_path)

        def old_lock(repo):     # 90 seconds: past the one minute, early in systemd's retries
            lock = repo / ".git" / lock_path
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("")
            os.utime(lock, (time.time() - 90, time.time() - 90))
            # Real refs just as old must survive: only locks are removed.
            for ref in (repo / ".git" / "refs" / "tags" / "v0.0.1", repo / ".git" / "refs" / "heads" / "keep-me"):
                if not ref.exists():
                    ref.write_text((repo / ".git" / "refs" / "tags" / "v0.0.1").read_text())
                os.utime(ref, (time.time() - 90, time.time() - 90))

        result, head, _ = self._server(tmp_path, "v0.0.1", str(origin), before_gate=old_lock)
        assert result.returncode == 0, result.stderr
        assert not (tmp_path / "repo" / ".git" / lock_path).exists()
        assert (tmp_path / "repo" / ".git" / "refs" / "heads" / "keep-me").exists()
        assert "removed a leftover lock file" in result.stderr and head == recut
        assert "deploy-gate: git:" not in result.stderr          # nothing empty or stray logged

    def test_a_fresh_lock_file_is_left_and_the_tag_on_the_server_served(self, tmp_path):
        """A lock less than a minute old may belong to a running git: not touched. The fetch it
        blocks fails, which is not a refusal: the tag on the server is served, with git's words."""
        origin, _ = self._origin(tmp_path)
        lock = lambda repo: repo / ".git" / "refs" / "tags" / "v0.0.1.lock"   # noqa: E731
        result, head, tagged = self._server(tmp_path, "v0.0.1", str(origin),
                                            before_gate=lambda repo: lock(repo).write_text(""))
        self._served_from_disk(result, head, tagged)
        assert ".lock" in result.stderr and lock(tmp_path / "repo").exists()

    def test_a_lock_a_fetch_does_not_take_is_left_alone(self, tmp_path):
        """An operator's own git (say a `git gc`) holds other locks; only a fetch's are removed."""
        origin, _ = self._origin(tmp_path)

        def gc_lock(repo):
            lock = repo / ".git" / "gc.pid.lock"
            lock.write_text("")
            os.utime(lock, (time.time() - 600, time.time() - 600))

        result, _, _ = self._server(tmp_path, "v0.0.1", str(origin), before_gate=gc_lock)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "repo" / ".git" / "gc.pid.lock").exists()

    def test_a_stalled_connection_is_cut_off_and_serves_the_tag_on_the_server(self, tmp_path):
        ssh = self._script(tmp_path, "stall-ssh", "sleep 30")   # an ssh that never answers
        started = time.monotonic()
        result, head, tagged = self._server(
            tmp_path, "v0.0.1", "ssh://example.invalid/repo.git",
            extra_env={"GIT_SSH_COMMAND": ssh, "CRAFDAPI_FETCH_TIMEOUT": "1"})
        self._served_from_disk(result, head, tagged)
        assert time.monotonic() - started < 15, "the 1-second limit did not cut the stall off"
        assert "the fetch ran past 1 s" in result.stdout
        assert not re.search(r"deploy-gate: git: *$", result.stderr, re.M)   # no empty git lines

    def test_a_time_limit_setting_that_is_not_a_number_is_not_handed_to_timeout(self, tmp_path):
        """"soon" given to `timeout` would fail at once ("invalid time interval") and leave no
        limit at all; it must fall back to the default instead."""
        ssh = self._script(tmp_path, "refused-ssh", "echo 'Connection refused' >&2\nexit 255")
        result, head, tagged = self._server(tmp_path, "v0.0.1", "ssh://example.invalid/repo.git",
                                            extra_env={"GIT_SSH_COMMAND": ssh, "CRAFDAPI_FETCH_TIMEOUT": "soon"})
        self._served_from_disk(result, head, tagged)
        assert "invalid time interval" not in result.stderr
        assert "CRAFDAPI_FETCH_TIMEOUT=soon is not 1 to 90 seconds; using 60" in result.stderr
        assert "time limit 60 s" in result.stderr       # the value actually used

    @pytest.mark.parametrize("setting,used", [("0", 60), ("91", 60), ("90", 90), ("1", 1), (None, 60)])
    def test_the_time_limit_actually_used(self, tmp_path, setting, used):
        """0 would mean no limit at all for `timeout`; out-of-range values fall back to 60."""
        ssh = self._script(tmp_path, "refused-ssh", "echo 'Connection refused' >&2\nexit 255")
        env = {"GIT_SSH_COMMAND": ssh}
        if setting is not None:
            env["CRAFDAPI_FETCH_TIMEOUT"] = setting
        result, head, tagged = self._server(tmp_path, "v0.0.1", "ssh://example.invalid/repo.git", extra_env=env)
        self._served_from_disk(result, head, tagged)
        assert f"time limit {used} s" in result.stderr

    def test_a_huge_time_limit_setting_is_not_kept(self, tmp_path):
        """Twenty digits are too big for bash's comparison, which then lets the value through."""
        ssh = self._script(tmp_path, "refused-ssh", "echo 'Connection refused' >&2\nexit 255")
        result, head, tagged = self._server(tmp_path, "v0.0.1", "ssh://example.invalid/repo.git",
                                            extra_env={"GIT_SSH_COMMAND": ssh,
                                                       "CRAFDAPI_FETCH_TIMEOUT": "9" * 20})
        self._served_from_disk(result, head, tagged)
        assert "is not 1 to 90 seconds" in result.stderr

    def test_the_default_and_the_range_of_the_time_limit(self):
        """Read, not run: observing a 60-second limit, or 0 meaning "no limit", takes minutes."""
        assert 'CRAFDAPI_FETCH_TIMEOUT:-60' in _GATE
        assert '[ "$FETCH_SECONDS" -lt 1 ] || [ "$FETCH_SECONDS" -gt 90 ]' in _GATE
        # The package sync and version check have their own limits, and with the fetch's maximum
        # they fit systemd's 180-second start budget (read, not run).
        version = int(re.search(r"^VERSION_SECONDS=(\d+)$", _GATE, re.M)[1])
        assert "SYNC_SECONDS=$(( 170 - SECONDS - 5 - VERSION_SECONDS - 5 ))" in _GATE
        assert 'timeout -k 5 "$SYNC_SECONDS" uv sync' in _GATE
        assert 'timeout -k 5 "$VERSION_SECONDS" uv run' in _GATE
        assert (90 + 5) + (10 + 5) + (version + 5) <= 180     # the sync's floor still fits

    def test_git_never_waits_for_a_password(self):
        """Read, not run: a waiting prompt would pass for an outage (the tests' own environment
        already sets GIT_TERMINAL_PROMPT=0, so running cannot tell)."""
        assert re.search(r"^export GIT_TERMINAL_PROMPT=0$", _GATE, re.M) and "-o BatchMode=yes" in _GATE

    def test_a_repository_that_does_not_exist_stops_the_start(self, tmp_path):
        result, _, _ = self._server(tmp_path, "v0.0.1", str(tmp_path / "no-such-repo"))
        assert result.returncode == 1
        assert "origin refused the fetch" in result.stderr
        assert "does not appear to be a git repository" in result.stderr

    def test_the_ssh_wrapper_named_in_git_ssh_is_kept(self, tmp_path):
        used = tmp_path / "wrapper-used"
        ssh = self._script(tmp_path, "wrapper-ssh", f"touch {used}\necho 'Connection refused' >&2\nexit 255")
        self._served_from_disk(*self._server(tmp_path, "v0.0.1", "ssh://git@github.invalid/repo.git",
                                             extra_env={"GIT_SSH": ssh}))
        assert used.exists()

    def test_the_deploy_key_named_in_git_config_is_kept(self, tmp_path):
        """`core.sshCommand` may name the deploy key; the gate's own ssh limits must not
        override it."""
        used = tmp_path / "configured-ssh-used"
        ssh = self._script(tmp_path, "configured-ssh", f"touch {used}\necho 'Connection refused' >&2\nexit 255")
        self._served_from_disk(*self._server(tmp_path, "v0.0.1", "ssh://git@github.invalid/repo.git",
                                             git_config={"core.sshCommand": ssh}))
        assert used.exists()

    def test_a_tag_the_server_does_not_have_is_still_refused(self, tmp_path):
        result, _, _ = self._server(tmp_path, "v9.9.9", self.UNREACHABLE)
        assert result.returncode == 1
        assert "could not fetch tags from origin" in result.stderr
        assert "tag not found on this server, and origin could not be checked" in result.stderr

    def test_the_version_check_still_runs_without_origin(self, tmp_path):
        """And says the likely cause: a tag re-made on GitHub while it could not be reached."""
        result, _, _ = self._server(tmp_path, "v0.0.1", self.UNREACHABLE, version="0.0.2")
        assert result.returncode == 1 and "does not match package version" in result.stderr
        fatal = [line for line in result.stderr.splitlines() if "does not match package version" in line]
        assert fatal and "origin NOT checked" in fatal[0]       # the FATAL line itself says so
        assert "restart once the fetch works" in result.stderr

    def test_a_tag_that_cannot_be_checked_out_stops_the_start(self, tmp_path):
        """A leftover index.lock (not one a fetch takes) blocks the checkout."""
        result, _, _ = self._server(tmp_path, "v0.0.1", self.UNREACHABLE,
                                    before_gate=lambda repo: (repo / ".git" / "index.lock").write_text(""))
        assert result.returncode == 1 and "could not check out v0.0.1" in result.stderr
        assert "deploy-gate: git: " in result.stderr and "index.lock" in result.stderr   # git's words
        assert "serving tag" not in result.stdout

    def test_a_tag_re_cut_on_origin_replaces_the_servers_copy(self, tmp_path):
        """--force: when origin can be reached, the server converges on origin's tag (2026-08-15)."""
        origin, recut = self._origin(tmp_path)
        result, head, stale = self._server(tmp_path, "v0.0.1", str(origin))
        assert result.returncode == 0, result.stderr
        assert head == recut and head != stale and "origin checked" in result.stdout
        calls = (tmp_path / "uv-calls").read_text().splitlines()
        assert calls and not any("--offline" in c for c in calls)   # online when origin answered
