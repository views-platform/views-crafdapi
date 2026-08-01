"""Seam-contract conformance: the shipped client imports no shared runtime.

The Appwrite Seam Contract §5.8 (pinned by URL@tag in the README) and
`joining_the_seam.md §3` require each consumer API to **write its own thin
Appwrite client** and to NOT import ``views_pipeline_core.modules.{appwrite,
datastore}``. That import edge is precisely how a two-repo defect became a
three-repo defect; forbidding it is the whole point of WET-before-DRY here.

This is a tripwire, not a migration: ``src/`` ships clean today, and this test
fails loudly the moment a future edit reintroduces the shared-runtime import.

Scope is deliberately the **shipped package** (``src/``). Cross-repo
*equivalence* tests may ``importorskip`` a pipeline-core module to compare
signatures — that is a conformance check against the contract, not a runtime
dependency of the served surface — so tests are out of scope here.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer4_infra

SRC = Path(__file__).resolve().parent.parent / "src"

# The two subpackages the seam contract names explicitly. A banned import is any
# dotted path at or below one of these — whether imported as a module
# (``import views_pipeline_core.modules.appwrite``), from-imported
# (``from views_pipeline_core.modules.appwrite import X``), or named as the
# leaf of its parent (``from views_pipeline_core.modules import appwrite``).
_BANNED_ROOTS = (
    "views_pipeline_core.modules.appwrite",
    "views_pipeline_core.modules.datastore",
)


def _is_banned(dotted: str) -> bool:
    """True if a dotted module path is one of the banned roots or below it."""
    return any(dotted == root or dotted.startswith(root + ".") for root in _BANNED_ROOTS)


def _violations_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_banned(alias.name):
                    hits.append(f"{path.name}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # Absolute imports only (level 0); the module may itself be banned,
            # or a banned leaf may be pulled from its parent package.
            if node.level != 0 or node.module is None:
                continue
            if _is_banned(node.module):
                hits.append(f"{path.name}:{node.lineno}: from {node.module} import ...")
            else:
                for alias in node.names:
                    if _is_banned(f"{node.module}.{alias.name}"):
                        hits.append(
                            f"{path.name}:{node.lineno}: from {node.module} import {alias.name}"
                        )
    return hits


def _src_modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


class TestNoPipelineCoreSharedRuntimeImport:

    def test_scan_is_not_vacuous(self):
        """Guard the guard: the scan must actually reach the shipped package.

        A broken glob or moved ``src/`` would make the ban below pass
        vacuously; anchor on a module we know ships.
        """
        modules = _src_modules()
        assert modules, f"no source modules found under {SRC} — the ban would pass vacuously"
        assert (SRC / "views_crafdapi" / "managers" / "api.py") in modules

    def test_no_module_imports_shared_appwrite_or_datastore(self):
        """No shipped module imports views_pipeline_core.modules.{appwrite,datastore}."""
        violations: list[str] = []
        for path in _src_modules():
            violations.extend(_violations_in(path))
        assert not violations, (
            "Seam contract §5.8 forbids importing the shared Appwrite/datastore "
            "runtime — write this API's own thin client instead:\n  "
            + "\n  ".join(violations)
        )
