#!/usr/bin/env python3
"""Can this build serve a staged wire-contract run? — thin CLI wrapper.

Logic lives in ``views_crafdapi.preflight`` (importable + unit-tested). Run this against a
producer staging directory BEFORE arming the upload interlock, so a malformed run is caught
locally in seconds instead of as a bare HTTP 503 after a multi-GB round trip.

    .venv/bin/python scripts/preflight_run.py <staging_dir>
    # or:  python -m views_crafdapi.preflight <staging_dir>

Exits non-zero if any gate fails. See issue #41 (epic #40).
"""
import sys

from views_crafdapi.preflight import main

if __name__ == "__main__":
    sys.exit(main())
