"""Falsification audit of the claim, 2026-08-17:

    "we are done here; nothing is broken and nothing is half done. No repo upstream or
     downstream is blocked by things in this repo."

Three hard falsifications. Each test below fails today and is the enforcement half of the
finding — the report is the other half. Delete a test only when its finding is genuinely fixed,
never to make the suite green.
"""
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.layer5_audit

REPO = Path(__file__).resolve().parent.parent


# ── HARD 1 — "nothing is broken" ──────────────────────────────────────────────────────
# /data/forecast/bulk returns 504 after 300s (nginx proxy_read_timeout) on a warm dataset,
# reproduced three times. The bulk parquet is a documented consumer product and cannot be
# downloaded at all. Tracked as #79.


# The #79 finding was FIXED and verified in production on 2026-08-18: `/data/forecast/bulk`
# returns 200 in 25.8 s at 6.0 G peak on the deployed v0.4.0, against 501 s / 504 / 14.8 G on
# v0.3.0. Its test is deleted rather than left passing — a falsification test whose finding is
# fixed has no subject. Cause: C-235 (per-row `collapse`) plus ADR-030 S7 never landing.


# ── HARD 2 — "nothing is half done" ──────────────────────────────────────────────────
# notebooks/README.md documents a refresh command that cannot run. `jupyter nbconvert
# --version` prints 7.16.6, but executing it raises ModuleNotFoundError: No module named
# 'nbconvert' and exits 1. This is the SAME defect class fixed earlier the same day (the
# README documented `uv run jupyter lab` while jupyterlab was absent from the dev group) —
# reintroduced in the same file, hours later, by documenting a command without running it.


# The Stage-1 finding (notebooks/README.md documenting a refresh command that could not run)
# was FIXED on 2026-08-17: the command is now `uv run --with nbconvert ...`, executed end to end
# before being written down. Its test is deleted rather than left passing — a falsification test
# whose finding is fixed has no subject.


# ── HARD 3 — "no repo upstream or downstream is blocked" ──────────────────────────────
# The vcr_/vmo_/vpp_ ADR prefix convention was proposed FROM this repo (issues filed in all
# three). views-models adopted vmo_017 across 5 files; views-postprocessing adopted vpp_017
# across 2. This repo has adopted it in ZERO, and docs/ADRs/active/033_*.md still carries 14
# bare "ADR-017" citations against 2 qualified ones — each bare one means views-models' ADR
# while this repo has its own ADR-017 about reference data. Tracked as #58.


@pytest.mark.xfail(strict=True, reason="#58 open: vcr_017 unadopted while vmo_/vpp_ are done")
def test_this_repo_adopted_the_adr_prefix_convention_it_proposed():
    """#58. The other two repos completed their half; this one has not started."""
    hits = subprocess.run(
        ["grep", "-rIl", "vcr_017", str(REPO / "docs")],
        capture_output=True, text=True,
    )
    assert hits.stdout.strip(), (
        "#58: `vcr_017` appears in no file under docs/, while views-models uses `vmo_017` in 5 "
        "files and views-postprocessing uses `vpp_017` in 2. A three-repo convention this repo "
        "proposed is adopted 2/3."
    )


@pytest.mark.xfail(strict=True, reason="#58 open: 14 bare ADR-017 citations in ADR-033")
def test_adr_033_citations_name_which_repos_adr_017_they_mean():
    """#58. A bare `ADR-017` in ADR-033 resolves to this repo's own ADR — the wrong document."""
    text = (REPO / "docs" / "ADRs" / "active" / "033_fail_visible_forecast_selection.md").read_text()
    # Count only `ADR-017` occurrences, and only the prefixed subset of *those* as qualified.
    # The earlier form added `vmo_017` hits — which contain no "ADR-017" substring — to
    # `qualified` while `total` counted "ADR-017" alone, so adopting the convention the way
    # this test's own failure message prescribes drove `bare` negative and the assertion could
    # never pass. A guard that cannot go green stops being a guard.
    total = text.count("ADR-017")
    qualified = text.count("views-models ADR-017")
    bare = total - qualified
    assert bare == 0, (
        f"#58: {bare} of {total} `ADR-017` citations in ADR-033 are unqualified. Every one means "
        f"views-models' ADR, but this repo has its own ADR-017 (Reference Data in Repository), "
        f"and docs/ADRs/README.md points a reader there."
    )


# ── SOFT — the next delivery, not this one ───────────────────────────────────────────
# views-models#403 is open: "Bump VIEWS_POSTPROCESSING_PIN 1.1.0 -> 1.1.1 in both launchers —
# 1.1.0 carries the download fail-open that broke the first CRAF'd delivery". The pin used by
# the delivery path still carries the defect this repo's first delivery exposed
# (views-postprocessing#268). Nothing is broken *now*; the next delivery meets it.


@pytest.mark.integration
def test_delivery_path_pin_carries_the_download_fix():
    """views-models#403. Not this repo's file to edit — recorded so it is not forgotten."""
    pytest.fail(
        "views-models#403: both launchers still pin views-postprocessing 1.1.0, which carries "
        "the _ContractStorePort.download fail-open (vpp#268) that made the first CRAF'd "
        "delivery fail unreadably. Bump to a pin containing the fix before the next delivery."
    )
