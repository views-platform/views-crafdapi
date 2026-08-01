#!/usr/bin/env python3
"""Post-deploy smoke test + cache warmer for the live CRAF'd API — thin CLI wrapper.

Logic lives in ``views_crafdapi.smoke`` (importable + unit-tested). Run right after a deploy, from the
deploy shell, with a caller/read-scoped key exported:

    APPWRITE_DATASTORE_API_KEY=<caller key> .venv/bin/python scripts/smoke.py --expect-tag vX.Y.Z

Equivalent: ``python -m views_crafdapi.smoke --expect-tag vX.Y.Z``. Exits non-zero if any check fails.
"""
import sys

from views_crafdapi.smoke import main

if __name__ == "__main__":
    sys.exit(main())
