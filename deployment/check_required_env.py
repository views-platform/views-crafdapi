#!/usr/bin/env python3
"""Refuse an env file that lacks a coordinate the deployed code requires.

``bootstrap.sh part2`` builds ``.env.crafdapi`` from the registry, then pipes it here before
installing it. The required names come from the code being deployed
(``_REQUIRED_APPWRITE_ENV_VARS``), never from a copy, so a renamed coordinate (þing 04:
``APPWRITE_CRAFD_PROJECT_ID``) cannot be deployed ahead of its registry row. Without this,
the file would install and the service would loop on ``_validate_appwrite_env``.

Prints only the absent NAMES, never a value: the file carries the operator's key.

Usage:  <env file on stdin> | python3 check_required_env.py
"""

from __future__ import annotations

import sys


def absent_names(env_text: str, required: list[str] | None = None) -> list[str]:
    """Required names with no non-empty ``NAME=value`` line in ``env_text``."""
    if required is None:
        from views_crafdapi.managers.api import _REQUIRED_APPWRITE_ENV_VARS

        required = _REQUIRED_APPWRITE_ENV_VARS
    # Last line wins, as in the dotenv reader the service uses.
    values: dict[str, str] = {}
    for line in env_text.splitlines():
        name, sep, value = line.partition("=")
        if sep:
            values[name] = value
    return [name for name in required if not values.get(name, "").strip()]


def main() -> int:
    absent = absent_names(sys.stdin.read())
    if absent:
        print(" ".join(absent))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
