"""Falsification probes for the claim: "all blockers to making views-faoapi public
are registered."

- The hardcoded-key probe is now a **hard gate** (xfail removed): the three key-bearing
  notebooks were deleted from the working tree (C-77, 2026-06), so no tracked notebook may
  reintroduce an Appwrite key. NOTE: this guards the *working tree* only — the key remains in
  ~183 commits of git history until scrubbed and rotated (C-77, still open).
- The GAUL-data-redistribution probe remains `xfail` — that blocker is unresolved.

See risk register: C-77 (hardcoded credential), and the GAUL-data-redistribution blocker.
"""

import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parent.parent

# Appwrite standard API-key signature: `standard_` + a long token. Pattern only —
# the actual secret value is never embedded here.
APPWRITE_KEY = re.compile(r"standard_[A-Za-z0-9]{30,}")


def test_no_hardcoded_appwrite_key_in_any_tracked_notebook():
    """No tracked notebook may contain a hardcoded Appwrite API key (use env vars)."""
    offenders = []
    for nb in sorted((REPO / "notebooks").glob("*.ipynb")):
        if APPWRITE_KEY.search(nb.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(nb.name)
    assert not offenders, f"hardcoded Appwrite key found in: {offenders}"


def test_no_unlicensed_thirdparty_data_bundled_for_public_release():
    """Bundled third-party geodata must ship with explicit license/terms clearing
    redistribution, or not be in the public tree at all."""
    # Was xfail until 2026-08-23: `src/views_crafdapi/shapefiles/` bundled FAO GAUL 2024
    # with no license or terms, which would have made a public flip redistribute data we
    # do not own under the repo-root MIT LICENSE. The directory was code-dead (C-239 — the
    # only `gpd.read_file` fetches Natural Earth from a URL) and was deleted, which removed
    # the blocker rather than documenting it. The guard stays live so re-bundling fails.
    shp_dir = REPO / "src" / "views_crafdapi" / "shapefiles"
    if not shp_dir.exists():
        return  # nothing bundled -> fine
    has_terms = any(
        p.name.lower().startswith(("license", "terms", "copyright", "disclaimer"))
        for p in shp_dir.rglob("*")
    )
    bundled = [p.name for p in shp_dir.rglob("*.shp")]
    assert has_terms, (
        f"third-party geodata bundled with no license/terms: {bundled} — "
        "redistribution rights for public release are unverified"
    )
