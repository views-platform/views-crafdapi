#!/usr/bin/env python3
"""Post-deploy smoke test + cache warmer for the live CRAF'd API — thin CLI wrapper.

Logic lives in ``views_crafdapi.smoke`` (importable + unit-tested). Run right after a deploy, from the
deploy shell, with a reader key from the `crafdapi` project in `CRAFD_KEY` (typed at a hidden
prompt, see RELEASE_RUNBOOK.md section 1):

    .venv/bin/python scripts/smoke.py --expect-tag vX.Y.Z --expect-run RUN_ID --expect-draws 1024

Equivalent: ``python -m views_crafdapi.smoke --expect-tag vX.Y.Z``. Exits non-zero if any check fails.
"""
import sys

from views_crafdapi.smoke import main

if __name__ == "__main__":
    sys.exit(main())
