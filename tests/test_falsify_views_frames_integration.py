"""Integration guards for views-frames in the serving path.

Originally a falsification probe ("views-frames is 100% integrated" — FALSIFIED on
2026-06-24, when it was a dev-only dependency used only by the spike parity test). As of
the M1 estimator swap, the serving path's MAP/HDI is computed by views-frames
(`tower_point`/`hdi_tower`, wired in `data/handlers.py`) and views-frames is a runtime
dependency. These now ENFORCE that integration (regular asserts, no longer xfail).
"""

import pathlib
import re
import tomllib

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "views_crafdapi"


def test_views_frames_is_used_by_production_code():
    """Some module under src/views_crafdapi must import views_frames for it to be 'integrated'."""
    imports = [
        p.relative_to(REPO).as_posix()
        for p in SRC.rglob("*.py")
        if re.search(r"^\s*(import|from)\s+views_frames", p.read_text(encoding="utf-8", errors="ignore"), re.M)
    ]
    assert imports, "no production module under src/ imports views_frames"


def test_views_frames_is_a_runtime_dependency():
    """An integrated dependency must be a runtime dependency, not dev-only."""
    meta = tomllib.load(open(REPO / "pyproject.toml", "rb"))
    runtime = " ".join(meta["project"].get("dependencies", []))
    assert "views-frames" in runtime, "views-frames is not a runtime dependency (dev-only)"
