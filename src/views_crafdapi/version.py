"""Deployed-version reporting (S6 / #191).

A tiny, dependency-light surface so a deploy can be verified *remotely*: the installed
package version (`importlib.metadata`) and the git tag this deploy was pinned to by the
S4 deploy gate (`~/.views-faoapi-deploy-tag`), if present. No auth, no side effects.
"""

from __future__ import annotations

import os
from importlib import metadata
from pathlib import Path
from typing import Optional

from views_crafdapi.forecast.contract import SERVED_CONTRACT_VERSION

_PACKAGE = "views-crafdapi"
_DEFAULT_DEPLOY_TAG_FILE = "~/.views-faoapi-deploy-tag"


def _pyproject_version() -> Optional[str]:
    """``[project] version`` from the source ``pyproject.toml`` beside this package, or ``None``
    (e.g. a wheel-installed layout with no source tree next to the module)."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        import tomllib

        with pyproject.open("rb") as f:
            version = tomllib.load(f).get("project", {}).get("version")
    except (OSError, ValueError, ImportError):
        return None
    return str(version) if version else None


def installed_version() -> str:
    """The version of the deployed code, for ``GET /version``.

    Prefer the checked-out ``pyproject.toml`` (``[project] version``): the S4 deploy gate pins the
    git tag to equal it, so this always agrees with ``deployed_tag``. ``importlib.metadata`` is a
    fallback only — an *editable* install's recorded metadata can lag a git-checkout deploy that
    did not force-reinstall (it would report the version at last reinstall, not the live code).
    Returns ``"unknown"`` if neither source is available.
    """
    version = _pyproject_version()
    if version:
        return version
    try:
        return metadata.version(_PACKAGE)
    except metadata.PackageNotFoundError:
        return "unknown"


def deployed_tag() -> Optional[str]:
    """The git tag this deploy was pinned to (S4 deploy gate), or ``None`` if unpinned.

    Reads the deploy-tag file (path overridable via ``FAOAPI_DEPLOY_TAG_FILE``); an
    absent or empty file yields ``None`` — e.g. before the S4 gate lands, or for a
    branch-tracking (``@main``) deploy.
    """
    path = Path(
        os.path.expanduser(os.environ.get("FAOAPI_DEPLOY_TAG_FILE", _DEFAULT_DEPLOY_TAG_FILE))
    )
    try:
        tag = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return tag or None


def version_info() -> dict:
    """``{"version", "deployed_tag", "served_contract_version"}`` — for ``GET /version``.

    ``served_contract_version`` is the wire-contract dialect this build renders (S5/#250, C-171):
    a run whose manifest declares a version this build cannot serve is refused, not degraded-served,
    so exposing it here makes deploy/serve capability skew remotely diagnosable."""
    return {
        "version": installed_version(),
        "deployed_tag": deployed_tag(),
        "served_contract_version": SERVED_CONTRACT_VERSION,
    }
