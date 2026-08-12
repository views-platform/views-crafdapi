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
