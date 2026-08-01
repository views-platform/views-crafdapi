"""Serving-isolation tripwire — epic #325, story S1 (issue #326).

faoapi is a read-only *serving* app, but it carries a producer write / provision /
delete subtree (``upload_predictions``, ``delete_prediction``, and the
``AppWriteFileManager`` / ``MetadataManager`` ``upload_*`` / ``create_*`` /
``delete_*`` methods). That subtree is dead on the serving path today **by
non-invocation, not by a guard**: no route reaches it.

That safety is a fact about the current route table, not a property of the code —
and a clone into a *write* API (views-productionapi) activates the subtree under a
caller's key at birth (see #322, Tier-1). This test locks "dead on serving" as an
**invariant**: no serving-layer module may *call* a producer method. If a future
edit wires a producer call into a served route, this test fails loudly.

Post-split (S9/S10) the producer methods move to dedicated ``writer.py`` /
``provisioning.py`` modules; at that point this tripwire is strengthened to also
assert the serving modules never *import* those modules. Until then, the call-site
check below is the guard.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer5_audit

# The write / provision / delete surface — the producer subtree that must stay
# unreachable from anything a request can trigger.
PRODUCER_METHODS = frozenset(
    {
        "upload_predictions",
        "delete_prediction",
        "update_prediction_metadata",
        "upload_file",
        "upload_file_from_bytes",
        "upload_file_with_metadata",
        "upload_file_from_bytes_with_metadata",
        "delete_file",
        "create_bucket",
        "create_database_if_not_exists",
        "create_metadata_collection_if_not_exists",
        "update_file_metadata",
        "delete_metadata_document",
    }
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "views_crafdapi"

# The serving path: the FastAPI routes + lifecycle (api.py), the read
# orchestration (dataset_service.py), and the value cache (disk_cache.py). AST
# walks into the nested route-handler closures defined inside api.py, so a
# producer call added to any route is caught here.
SERVING_MODULES = (
    _SRC / "managers" / "api.py",
    _SRC / "managers" / "dataset_service.py",
    _SRC / "managers" / "disk_cache.py",
)


def _producer_calls(source: str, filename: str):
    """Yield (method, lineno) for every ``x.<producer_method>(...)`` call site."""
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in PRODUCER_METHODS
        ):
            yield node.func.attr, node.lineno


def test_serving_modules_never_call_a_producer_method():
    violations = []
    for path in SERVING_MODULES:
        assert path.exists(), f"serving module moved — update SERVING_MODULES: {path}"
        for method, lineno in _producer_calls(path.read_text(), str(path)):
            violations.append(f"{path.name}:{lineno} calls producer method .{method}()")
    assert not violations, (
        "The serving path reaches the dead producer subtree. A clone into a write "
        "API would activate it under a caller's key (epic #325 / #322):\n  "
        + "\n  ".join(violations)
    )


def test_tripwire_bites_on_an_injected_producer_call():
    """The guard must actually detect a producer call — proven without touching
    real code by feeding it a synthetic route handler."""
    injected = "def route(manager):\n    return manager.upload_predictions(payload)\n"
    found = [m for m, _ in _producer_calls(injected, "<injected>")]
    assert found == ["upload_predictions"]
