"""ADR-017 D2 (crafd) — bind this API's served document name to the public registry.

The producer uploads each delivery under a store-document ``name``; this API filters every
query on it. A silent drift makes the upload succeed while the endpoint returns empty with no
error (ADR-017 §1). This is the consumer half of ADR-017 §5: we check *our own* name (declared
once in :mod:`views_crafdapi.seam_contract`) against the public registry declaration, reading
neither the producer's source nor a copy (ADR-016) — only the authority, at an immutable tag.

The identity facts and the pure parser live in ``seam_contract``; this test owns the
environment plumbing (fetch the registry at the pinned tag via ``git show``; FAIL-not-skip in
CI, a silent skip there would re-open the invisible-delivery hole — ADR-016 §6).
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import PLATFORM_ROOT
from views_crafdapi.seam_contract import (
    CONSUMER_DOCUMENT_NAME,
    REGISTRY_CONTRACT_KEY,
    REGISTRY_PIN_TAG,
    REGISTRY_RELPATH,
    declared_value,
)

pytestmark = pytest.mark.layer4_infra


def _appwrite_repo() -> Path | None:
    """Locate the views-appwrite sibling checkout: $VIEWS_APPWRITE (set in CI) else the
    conventional sibling under PLATFORM_ROOT."""
    env = os.environ.get("VIEWS_APPWRITE", "").strip()
    candidate = Path(env) if env else (PLATFORM_ROOT / "views-appwrite")
    return candidate if candidate.exists() else None


def _git_show(repo: Path, tag: str, relpath: str) -> str | None:
    """Read `relpath` at `tag` from `repo`'s object store — the exact pinned content regardless
    of the working-tree ref. Returns None if the tag/path can't be resolved (read-only git)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{tag}:{relpath}"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    return result.stdout if result.returncode == 0 else None


def _pinned_registry_text() -> str:
    """The registry TOML at the pinned tag. Skips locally when the sibling/tag is absent, but
    FAILS under CI — a silent skip there would re-open the very invisible-delivery hole this
    guards (ADR-016 §6)."""
    repo = _appwrite_repo()
    text = _git_show(repo, REGISTRY_PIN_TAG, REGISTRY_RELPATH) if repo else None
    if text is None:
        msg = (
            f"cannot read views-appwrite {REGISTRY_RELPATH} at tag {REGISTRY_PIN_TAG}. "
            "Check out views-platform/views-appwrite as a sibling with its tags: set "
            "$VIEWS_APPWRITE (CI does), or place it at ../views-appwrite and `git fetch --tags`."
        )
        if os.environ.get("CI"):
            pytest.fail(f"D2 seam-contract check cannot run in CI — {msg}")
        pytest.skip(msg)
    return text


def _assert_bound(registry_toml_text: str) -> None:
    """Raise if our declared name does not equal the registry's declared value. Factored out so
    the drift test exercises this exact comparison, not a reimplementation."""
    declared = declared_value(registry_toml_text)
    if declared != CONSUMER_DOCUMENT_NAME:
        raise AssertionError(
            f"served document name {CONSUMER_DOCUMENT_NAME!r} != registry declaration "
            f"{declared!r} ([contract.{REGISTRY_CONTRACT_KEY}] at {REGISTRY_PIN_TAG}). A drift "
            "here makes the delivery INVISIBLE — the upload succeeds and the endpoint returns "
            "empty with no error (ADR-017 §1). If this is an intended contract amendment, re-pin "
            "both sides to the new registry edition (bump REGISTRY_PIN_TAG); never one side alone."
        )


def test_served_document_name_matches_the_registry_contract():
    """The consumer-side ADR-017 binding: our name == the pinned public declaration."""
    _assert_bound(_pinned_registry_text())


def test_the_binding_would_catch_a_drift():
    """Prove the guard bites: a registry declaring a different value must fail the comparison.
    Needs no sibling — exercises the real `seam_contract.declared_value` + compare."""
    drifted = (
        f"[contract.{REGISTRY_CONTRACT_KEY}]\n"
        'class = "contract"\n'
        f'value = "{CONSUMER_DOCUMENT_NAME}_RENAMED"\n'
    )
    with pytest.raises(AssertionError, match="INVISIBLE"):
        _assert_bound(drifted)


def test_declared_value_fails_clearly_when_the_row_is_absent():
    """A registry edition predating the UNCRAFD row (or a mis-lowered pin) must fail with a
    legible ValueError naming the row, not a bare KeyError (C-249)."""
    from views_crafdapi import seam_contract

    with pytest.raises(ValueError, match=r"no \[contract\."):
        seam_contract.declared_value('[meta]\nversion = "1.4.0"\n')


def test_the_config_carries_the_path_manager_so_the_name_filter_actually_applies(tmp_path):
    """The binding above is worthless if the query never carries the name.

    `AppwriteConfig.path_manager` defaults to `None`. When `_create_appwrite_config` omitted it,
    `PredictionStoreManager.model_path` was `None`, `hasattr(model_path, 'model_name')` was
    False, and `get_predictions_by_metadata` **silently skipped** `filters["name"]` — every
    prediction query ran unscoped across the collection while `seam_contract` and `README.md`
    both stated the opposite.

    No existing test could observe it: they inject a `MagicMock()` config, and a MagicMock's
    `.path_manager.model_name` auto-exists. This one builds the real thing.
    """
    import os
    from unittest.mock import patch

    from views_crafdapi.managers.api import CrafdApiManager
    from views_crafdapi.seam_contract import CONSUMER_DOCUMENT_NAME

    env = {
        "APPWRITE_ENDPOINT": "https://example.invalid/v1",
        "APPWRITE_CRAFD_PROJECT_ID": "proj",
        "APPWRITE_CRAFD_BUCKET_ID": "bucket",
        "APPWRITE_CRAFD_BUCKET_NAME": "bucket-name",
        "APPWRITE_CRAFD_COLLECTION_ID": "coll",
        "APPWRITE_CRAFD_COLLECTION_NAME": "coll-name",
        "APPWRITE_METADATA_DATABASE_ID": "db",
        "APPWRITE_METADATA_DATABASE_NAME": "db-name",
    }
    # Build it the way production does — `create_app()` -> CrafdApiManager(model_path=...).
    # `from_config` is a test-only alternate constructor that never sets `_model_path`, so
    # using it here would test a path no deployment takes.
    from views_crafdapi.managers.model import APIPathManager

    with patch.dict(os.environ, env, clear=False):
        mgr = CrafdApiManager(
            model_path=APIPathManager(CONSUMER_DOCUMENT_NAME)
        )
        config = mgr._create_appwrite_config("a-key")

    assert config.path_manager is not None, (
        "path_manager is None — the name filter will be silently skipped and every prediction "
        "query runs unscoped across the collection"
    )
    assert config.path_manager.model_name == CONSUMER_DOCUMENT_NAME


def test_the_pinned_edition_is_at_least_the_floor():
    """ADR-044 serves every partner one loaded copy, which the storage contract allows only from
    appwrite-seam-v1.9.0 (§5.8, "uniform entitlement"). A lower pin passes the binding above,
    because the UNCRAFD row exists from v1.5.2, so the floor is checked on its own. The floor is
    written here, not read from the code it checks, so one edit cannot lower both."""
    import tomllib

    version = tomllib.loads(_pinned_registry_text())["meta"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"registry version {version!r} is not X.Y.Z"
    assert tuple(int(p) for p in version.split(".")) >= (1, 9, 0), (
        f"REGISTRY_PIN_TAG {REGISTRY_PIN_TAG} is registry edition {version}, below 1.9.0. "
        "ADR-044 needs the uniform-entitlement mode of contract §5.8, first in v1.9.0."
    )


def test_the_pin_is_a_published_tag_not_a_branch():
    """A branch moves; the contract is referenced only at a published tag."""
    assert re.fullmatch(r"appwrite-seam-v\d+\.\d+\.\d+", REGISTRY_PIN_TAG), REGISTRY_PIN_TAG


def test_the_bound_name_and_row_are_crafds():
    """Both sides of the binding come from production code, so a swap of both (say to FAO's row
    and name) would still agree. The values themselves are pinned here."""
    assert (CONSUMER_DOCUMENT_NAME, REGISTRY_CONTRACT_KEY) == ("un_crafd", "UNCRAFD_CONSUMER_DOCUMENT_NAME")


def test_the_readme_links_the_contract_at_the_pinned_tag_and_no_other():
    """The README's contract link and the CI pin are one tag; they drifted apart before (C-250)."""
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    url = (f"https://github.com/views-platform/views-appwrite/blob/{REGISTRY_PIN_TAG}/"
           "docs/ADRs/platform/appwrite_seam_contract.md")
    assert url in readme
    linked_refs = set(re.findall(r"views-appwrite/blob/([^/\s]+)/", readme))
    assert linked_refs == {REGISTRY_PIN_TAG}, f"README links views-appwrite at {linked_refs}, never main or an old tag"


def test_the_runbook_copies_the_registry_at_the_pinned_tag():
    """The server's coordinates come from the registry the runbook copies; it must be the edition
    CI binds, not the operator's checkout."""
    runbook = (Path(__file__).resolve().parents[1] / "deployment" / "RELEASE_RUNBOOK.md").read_text()
    shown = set(re.findall(r"git -C \.\./views-appwrite show ([^:\s]+):docs/ADRs/platform/coordinate_registry\.toml", runbook))
    assert shown == {REGISTRY_PIN_TAG}, shown


def test_the_metadata_query_sent_to_the_store_is_filtered_on_the_served_name():
    """The binding is worthless if the query sent to the store does not carry the name."""
    from unittest.mock import MagicMock

    from views_crafdapi.managers.prediction.manager import PredictionStoreManager

    # Built around its constructor and two name-mangled attributes: there is no lighter way in
    # that still sends the real query, so a rename of those attributes breaks this test on purpose.
    pm = PredictionStoreManager.__new__(PredictionStoreManager)
    pm._require_consumer_name = lambda: CONSUMER_DOCUMENT_NAME
    store = MagicMock()
    store.metadata_manager.search_files_by_metadata.return_value.to_dict.return_value = {
        "success": True, "data": {"documents": []}}
    pm._PredictionStoreManager__appwrite_file_manager = store
    pm._PredictionStoreManager__appwrite_file_manager_config = MagicMock()
    pm.get_predictions_by_metadata(filters={"category": "historical"})
    sent = store.metadata_manager.search_files_by_metadata.call_args.kwargs["filters"]
    assert sent["name"] == CONSUMER_DOCUMENT_NAME, sent


def test_production_builds_its_paths_from_the_served_name():
    from views_crafdapi.managers.api import _service_paths

    assert _service_paths().model_name == CONSUMER_DOCUMENT_NAME
