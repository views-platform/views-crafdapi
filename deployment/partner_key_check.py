#!/usr/bin/env python3
"""Before a partner key is sent: prove it is fenced, and name its disk partition.

Run on the server as the service user, with the key on stdin (``deployment/PARTNER_KEYS.md`` step 3):

1. **Fenced.** Lists the buckets the key can read, in the project and at the endpoint the running
   service is configured with (read from its env file, never copied into a document). Passes only on
   HTTP 200 with exactly one bucket, the configured CRAF'd bucket. An error response is a failure, not
   an empty list.
2. **Partition label.** Prints the label of the key's on-disk dataset copy, computed the way the
   service computes it (key hash, then the server's salted partition). Revoking a key later deletes
   ``<label>_*``. Deriving it, rather than spotting a new folder, cannot pick up another key's copy.

Never prints the key. Exit 0 only when fenced.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENV_FILE = Path("/home/views-crafdapi-deploy/.env.crafdapi")
DEFAULT_CACHE_DIR = Path("/home/views-crafdapi-deploy/views-crafdapi/apis/un_crafd/cache/datasets")


def read_env(env_file: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep:
            values[name.strip()] = value.strip().strip('"')
    return values


def readable_buckets(endpoint: str, project_id: str, key: str) -> list[str]:
    """Bucket ids the key can list. Raises on any non-200 answer, so a failed check never looks
    like an empty, fenced one."""
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/storage/buckets",
        headers={"X-Appwrite-Project": project_id, "X-Appwrite-Key": key},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"the store answered HTTP {response.status}")
        body = json.load(response)
    return sorted(bucket["$id"] for bucket in body["buckets"])


def partition_label(key: str, cache_dir: Path) -> str | None:
    """The service's label for this key, or None when the service's salt is not there. The cache
    manager would create a fresh salt (and directory) where none exists, giving a label nothing on
    disk carries, so the salt must already exist."""
    from views_crafdapi.managers.api import CrafdApiManager
    from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager

    if not (cache_dir / CrafdDiskCacheManager._SALT_FILE).is_file():
        return None
    key_hash = CrafdApiManager._get_api_key_hash(None, key)
    return CrafdDiskCacheManager(cache_dir)._partition(key_hash)


def check(key: str, env_file: Path = DEFAULT_ENV_FILE,
          cache_dir: Path = DEFAULT_CACHE_DIR) -> tuple[bool, str]:
    """(fenced, report). The report names buckets and the label, never the key."""
    env = read_env(env_file)
    missing = [n for n in ("APPWRITE_ENDPOINT", "APPWRITE_CRAFD_PROJECT_ID", "APPWRITE_CRAFD_BUCKET_ID")
               if not env.get(n)]
    if missing:
        return False, f"FAIL: {env_file} has no value for {', '.join(missing)} (server not on CRAF'd's project?)"
    expected = env["APPWRITE_CRAFD_BUCKET_ID"]
    try:
        buckets = readable_buckets(env["APPWRITE_ENDPOINT"], env["APPWRITE_CRAFD_PROJECT_ID"], key)
    except urllib.error.HTTPError as e:
        return False, f"FAIL: the store refused the check (HTTP {e.code}). Wrong key, or a key from another project."
    except Exception as e:  # noqa: BLE001 — any failure is a failed check, reported by type only
        return False, f"FAIL: the check could not run ({type(e).__name__})."
    if buckets != [expected]:
        return False, f"FAIL: NOT FENCED. The key reads {buckets}, expected only [{expected!r}]. Revoke it."
    label = partition_label(key, cache_dir)
    if label is None:
        return True, (f"PASS: fenced, the key reads only {expected!r}.\n"
                      f"Partition label: UNAVAILABLE (no service salt in {cache_dir}); "
                      "run this again after the service has served once.")
    return True, f"PASS: fenced, the key reads only {expected!r}.\nPartition label: {label}"


def main() -> int:
    key = sys.stdin.read().strip()
    if not key:
        print("FAIL: no key on stdin.")
        return 1
    fenced, report = check(key, DEFAULT_ENV_FILE, DEFAULT_CACHE_DIR)
    print(report)
    return 0 if fenced else 1


if __name__ == "__main__":
    sys.exit(main())
