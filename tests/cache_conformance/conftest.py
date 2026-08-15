"""crafd's binding of the shared cache-partition-isolation conformance vector (þing-02 A4).

The vector — `test_cache_partition_isolation_conformance.py` in this directory — is **copied
verbatim** from views-appwrite (`docs/ADRs/platform/conformance_vector_cache_partition_isolation.py`,
**VECTOR_VERSION 1.0.0**), per the Appwrite Seam Contract §5.8: a copied *test*, no import edge,
so views-appwrite stays parked and þing-01 D8's extraction trigger is untouched. This file supplies
the two fixtures the vector requires and binds them to crafd's real `CrafdDiskCacheManager` salt+HMAC
partitioning. Re-vendor + bump the copy when VECTOR_VERSION changes upstream (views-appwrite C-38).

Recorded: ran against **VECTOR_VERSION 1.0.0** (views-crafdapi, mirror of views-faoapi#324 / epic #383).
"""
import hashlib
from pathlib import Path

import pytest

from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager


class _CrafdCachePartitionAdapter:
    """Binds the vector to crafd's client. Exercises the REAL partitioning: `partition_label`
    routes through `CrafdDiskCacheManager._partition` (HMAC of the key-hash under the persisted
    server-side salt), and write/read drop a probe file into the partition-scoped value dir, so
    isolation is tested on the real on-disk layout rather than a stand-in."""

    def __init__(self, cache_dir: Path):
        self._dir = Path(cache_dir)
        self._client = CrafdDiskCacheManager(cache_dir=self._dir)

    @staticmethod
    def _key_hash(api_key: str) -> str:
        # the client's own key->hash step (sha256(key)[:16]) that precedes partitioning
        return hashlib.sha256(api_key.encode()).hexdigest()[:16]

    def partition_label(self, api_key: str) -> str:
        return self._client._partition(self._key_hash(api_key))

    def _probe_path(self, api_key: str, category: str) -> Path:
        return self._client._value_dir(self._key_hash(api_key), category) / "conformance_probe.bin"

    def write(self, api_key: str, category: str, payload: bytes) -> None:
        path = self._probe_path(api_key, category)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def read(self, api_key: str, category: str):
        path = self._probe_path(api_key, category)
        return path.read_bytes() if path.exists() else None

    def fresh_instance(self) -> "_CrafdCachePartitionAdapter":
        # same dir, new object — the persisted `.partition_salt` is re-read, simulating a restart
        return _CrafdCachePartitionAdapter(self._dir)


@pytest.fixture
def adapter(tmp_path):
    return _CrafdCachePartitionAdapter(tmp_path / "cache")


@pytest.fixture
def other_adapter(tmp_path):
    # a DIFFERENT cache dir → a different server-side salt, so criterion (ii)'s decisive test
    # (same key, two deployments → different labels) has a real second secret to compare against.
    return _CrafdCachePartitionAdapter(tmp_path / "other_deployment")
