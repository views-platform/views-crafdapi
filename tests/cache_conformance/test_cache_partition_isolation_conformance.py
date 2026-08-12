"""Conformance vector — cache-partition isolation.

VECTOR_VERSION 1.0.0 · authored 2026-08-01 against views-faoapi v1.4.0 (post-salt).
Governed by the Appwrite Seam Contract §5.8 (this directory).

WHAT THIS IS
------------
The VIEWS platform has decided to run several independently written Appwrite
clients rather than one shared implementation (WET before DRY: duplication is
cheaper than the wrong abstraction, and one shared client is how a two-repo
defect once reached a third repo).

That decision was taken on ONE condition: the isolation guarantee must not
drift silently across the copies. This file is that condition, made executable.

    Two distinct callers never share a cache partition, and a partition label
    is neither the key value nor derivable from it.

HOW TO USE IT — copy, do not import
-----------------------------------
COPY this file into your own repo's test suite and rename it to `test_*.py`.
Do NOT import it from views-appwrite, and do NOT add views-appwrite as a
dependency. A copied test creates no dependency edge, which is what keeps this
arrangement compatible with the seam contract's §5.8 and keeps views-appwrite
parked. Sharing a *test* is not sharing an *implementation*.

Then implement `CachePartitionAdapter` against your own client — four methods,
roughly twenty lines. Nothing else needs to change.

Record the VECTOR_VERSION you ran against. The vector and the clients that run
it are reused together but released separately, so nothing forces a re-run when
this file changes; recording the version is what makes that drift visible
instead of silent. (views-appwrite C-38.)

WHAT IT DOES NOT GUARD
----------------------
This invariant, and nothing else. It does not stop three clients diverging in
any other respect. That residual is recorded, unresolved, as þing-02 Ó-7 and is
the standing question for þing-01 D8's trigger — not something this file
pretends to solve.
"""

from __future__ import annotations

import hashlib
from typing import Optional

# No pytest import is needed: every check below is a plain `assert`, and the two
# fixtures come from your conftest.py. Keeping the file import-light means a
# consumer can read it without a test runner installed.

VECTOR_VERSION = "1.0.0"

# Fixed inputs. Distinct, realistic-shaped Appwrite API keys. Never real values.
KEY_A = "standard_" + "a" * 64
KEY_B = "standard_" + "b" * 64
KEY_SAMPLE = tuple(f"standard_{i:064x}" for i in range(24))

CATEGORY = "conformance_probe"
PAYLOAD_A = b"payload-belonging-to-caller-A"
PAYLOAD_B = b"payload-belonging-to-caller-B"


class CachePartitionAdapter:
    """Bind the vector to your client. Implement all four; nothing else.

    A reference binding for views-faoapi v1.4.0::

        class FaoapiAdapter(CachePartitionAdapter):
            def __init__(self, cache_dir):
                self._dir = cache_dir
                self._c = FAODiskCacheManager(cache_dir=cache_dir)

            def partition_label(self, api_key):
                # the client's OWN key->hash step, then its partition step
                return self._c._partition(sha256(api_key.encode()).hexdigest()[:16])

            def write(self, api_key, category, payload):
                self._c.write(sha256(...)[:16], category, payload)

            def read(self, api_key, category):
                return self._c.read(sha256(...)[:16], category)

            def fresh_instance(self):
                return FaoapiAdapter(self._dir)          # same dir, new object
    """

    def partition_label(self, api_key: str) -> str:
        """The on-disk label this client uses to separate one caller's cache
        from another's, for `api_key`. Must be a stable string."""
        raise NotImplementedError

    def write(self, api_key: str, category: str, payload: bytes) -> None:
        """Store `payload` in this caller's partition."""
        raise NotImplementedError

    def read(self, api_key: str, category: str) -> Optional[bytes]:
        """Return what this caller has stored, or None."""
        raise NotImplementedError

    def fresh_instance(self) -> "CachePartitionAdapter":
        """A NEW adapter object over the SAME cache directory — simulates a
        worker restart. Must not carry state over in memory."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# TWO FIXTURES YOU MUST SUPPLY, IN YOUR OWN `conftest.py`
# ---------------------------------------------------------------------------
#
#   @pytest.fixture
#   def adapter(tmp_path):
#       return MyAdapter(tmp_path / "cache")
#
#   @pytest.fixture
#   def other_adapter(tmp_path):
#       # a DIFFERENT cache directory, so it holds a different server-side
#       # secret. Used only by the non-derivability test.
#       return MyAdapter(tmp_path / "other_deployment")
#
# They are deliberately NOT defined in this file. A fixture defined in a test
# module shadows one from conftest.py, so stubs here could not be overridden
# without editing the vector — and a stub that skips would report a green run
# for a client that was never tested. Absent fixtures make pytest fail loudly
# with "fixture 'adapter' not found", which is the correct signal.
# ---------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Criterion (i) — two distinct keys yield two distinct partitions
# --------------------------------------------------------------------------

def test_i_distinct_keys_yield_distinct_partitions(adapter):
    assert adapter.partition_label(KEY_A) != adapter.partition_label(KEY_B)


def test_i_no_collisions_across_a_sample(adapter):
    labels = [adapter.partition_label(k) for k in KEY_SAMPLE]
    assert len(set(labels)) == len(KEY_SAMPLE), (
        "two callers collided onto one partition label; they would share a cache"
    )


def test_i_partition_is_deterministic(adapter):
    """Not merely hygiene: a non-deterministic label means the cache never hits,
    which reads as a performance problem and hides an isolation defect."""
    assert adapter.partition_label(KEY_A) == adapter.partition_label(KEY_A)


def test_i_partition_survives_a_restart(adapter):
    """The server-side secret must be persisted, not per-process. A fresh worker
    must land on the same partition or every restart silently re-partitions."""
    before = adapter.partition_label(KEY_A)
    assert adapter.fresh_instance().partition_label(KEY_A) == before


# --------------------------------------------------------------------------
# Criterion (ii) — the label is not derivable from the key value
# --------------------------------------------------------------------------

def test_ii_label_is_not_the_key(adapter):
    label = adapter.partition_label(KEY_A)
    assert KEY_A not in label
    assert label not in KEY_A


def test_ii_label_is_not_a_plain_digest_of_the_key(adapter):
    """The pre-fix defect: the label WAS sha256(key)[:16]. A one-way hash is
    still derivable by anyone holding the key, so it leaks which partition is
    whose to anyone who can list the cache directory."""
    label = adapter.partition_label(KEY_A)
    raw = KEY_A.encode()
    digests = [
        h(raw).hexdigest()
        for h in (hashlib.sha256, hashlib.sha1, hashlib.md5, hashlib.sha512, hashlib.blake2b)
    ]
    # also the sha256-of-the-sha256 step some clients use for in-memory keying
    digests.append(hashlib.sha256(digests[0].encode()).hexdigest())
    for d in digests:
        for n in (8, 12, 16, 24, 32, 40, 64, len(d)):
            assert label != d[:n], (
                f"partition label is a plain digest of the key (truncated at {n}); "
                "it must be derived through a server-side secret"
            )


def test_ii_label_depends_on_a_server_side_secret(adapter, other_adapter):
    """The decisive one. Same key, two deployments -> different labels. If they
    match, the derivation contains no secret and (ii) fails however the label
    is computed."""
    assert adapter.partition_label(KEY_A) != other_adapter.partition_label(KEY_A), (
        "the same key produces the same label under a different server-side "
        "secret, so the label is a pure function of the key and is derivable"
    )


# --------------------------------------------------------------------------
# Criterion (iii) — key A never serves content cached for key B
# --------------------------------------------------------------------------

def test_iii_one_caller_never_reads_anothers_cache(adapter):
    adapter.write(KEY_A, CATEGORY, PAYLOAD_A)
    assert adapter.read(KEY_B, CATEGORY) != PAYLOAD_A, (
        "caller B was served content cached for caller A"
    )


def test_iii_writes_do_not_overwrite_across_callers(adapter):
    adapter.write(KEY_A, CATEGORY, PAYLOAD_A)
    adapter.write(KEY_B, CATEGORY, PAYLOAD_B)
    assert adapter.read(KEY_A, CATEGORY) == PAYLOAD_A
    assert adapter.read(KEY_B, CATEGORY) == PAYLOAD_B


def test_iii_isolation_survives_a_restart(adapter):
    """Isolation that holds only in memory is not isolation."""
    adapter.write(KEY_A, CATEGORY, PAYLOAD_A)
    revived = adapter.fresh_instance()
    assert revived.read(KEY_A, CATEGORY) == PAYLOAD_A
    assert revived.read(KEY_B, CATEGORY) != PAYLOAD_A
