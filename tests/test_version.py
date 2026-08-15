"""The package version and the newest release tag must agree.

The deploy gate compares them at service start and refuses to serve on a mismatch, so a
disagreement here is a production outage rather than a test failure.
"""
import pytest

pytestmark = pytest.mark.layer4_infra

def test_the_package_version_matches_the_newest_release_tag():
    """The deploy gate refuses to serve when the tag and the package version disagree.

    On 2026-08-15 `v0.2.0` was cut without bumping `pyproject.toml`. The gate did its job —
    `FATAL deploy-gate: tag v0.2.0 does not match package version v0.1.0` — but it did so on
    the production box, after the restart, with the service down for the length of the
    rollback. Nothing in the repo could have caught it first.

    Skips when the working tree has no tags (a shallow CI checkout), rather than failing for
    a reason that is not about this repo's contents.
    """
    import subprocess

    from views_crafdapi.version import installed_version

    try:
        tag = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment guard
        pytest.skip("git unavailable")
    if tag.returncode != 0 or not tag.stdout.strip():
        pytest.skip("no tags in this checkout")

    newest = tag.stdout.strip().lstrip("v")
    assert installed_version() == newest, (
        f"pyproject version {installed_version()!r} != newest tag {newest!r}. "
        f"The deploy gate will refuse to start the service. Bump pyproject.toml + uv.lock, "
        f"or re-cut the tag."
    )
