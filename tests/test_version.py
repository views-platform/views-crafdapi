"""The package version and the newest release tag must agree.

The deploy gate compares them at service start and refuses to serve on a mismatch, so a
disagreement here is a production outage rather than a test failure.
"""
import pytest

pytestmark = pytest.mark.layer4_infra

def test_a_tagged_commit_declares_the_same_version_as_its_tag():
    """The deploy gate refuses to serve when the tag and the package version disagree.

    On 2026-08-15 `v0.2.0` was cut without bumping `pyproject.toml`. The gate did its job —
    `FATAL deploy-gate: tag v0.2.0 does not match package version v0.1.0` — but it did so on
    the production box, after the restart, with the service down for the length of the
    rollback. Nothing in the repo compared the two.

    Asserted only when **HEAD is itself tagged**, which is the state the gate ever sees. A
    bumped version ahead of the newest tag is normal release preparation, not drift — an
    earlier draft of this test asserted against `--abbrev=0` and so failed on every release
    between the bump and the tag, which would have taught people to ignore it.
    """
    import subprocess

    from views_crafdapi.version import installed_version

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment guard
        pytest.skip("git unavailable")
    if result.returncode != 0:
        pytest.skip("HEAD is not tagged — nothing for the deploy gate to disagree with")

    tag = result.stdout.strip().lstrip("v")
    assert installed_version() == tag, (
        f"HEAD is tagged {result.stdout.strip()!r} but pyproject declares "
        f"{installed_version()!r}. The deploy gate will refuse to start the service: "
        f"bump pyproject.toml + uv.lock, or re-cut the tag."
    )
