"""Disk-based cache for ForecastDataset VALUES with file-locking and schema versioning.

Persists the dataset's **value** (the `(N,S)` frame store + geo table + index + manifest,
via `ForecastDataset.to_value`/`from_value`), NOT a pickled object — so there is no
`pickle.load` on the cache read path (register C-149) and the cache version no longer
churns with the class signature (C-138). Extracted from FAOApiManager (C-36).
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from views_faoapi.data.handlers import ForecastDataset

import filelock

logger = logging.getLogger(__name__)

# S6a (#208): `source_kind` + `provenance` round-trip so a disk-served forecast keeps its kind
# (the §4.4 zero-manifest guard) and its C-86 lineage. Listing them here also auto-bumps
# CACHE_SCHEMA_VERSION (see _derive_cache_schema_version), invalidating pre-S6 caches on read.
_META_FIELDS = ("columns", "file_id", "provenance", "rows", "source_kind", "timestamp", "ttl_days")


def _derive_cache_schema_version() -> int:
    """Derive the cache schema version from the metadata sidecar layout + the on-disk
    VALUE format version (`value_format._VALUE_SCHEMA_VERSION`).

    Depends on the stable `data.value_format` leaf, not the volatile `ForecastDataset`
    module (the S11 SDP-inversion fix). Decoupled from `FAO_PGMDataset.__init__`'s
    signature (the C-138 fix): a class rename/refactor no longer invalidates the cache —
    only an actual change to the meta layout or the persisted value format does.
    """
    from views_faoapi.data.value_format import _VALUE_SCHEMA_VERSION

    fingerprint = f"{_META_FIELDS}|{_VALUE_SCHEMA_VERSION}"
    return int(hashlib.sha256(fingerprint.encode()).hexdigest()[:8], 16)


CACHE_SCHEMA_VERSION: int = _derive_cache_schema_version()

_DEFAULT_TTL_SECONDS: int = int(3.5 * 7 * 24 * 3600)  # 3.5 weeks


class FAODiskCacheManager:
    """Manages dataset-value caching on local disk with file-locking and TTL."""

    _SALT_FILE = ".partition_salt"

    def __init__(
        self,
        cache_dir: Path,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl_seconds
        self._salt_cache: Optional[bytes] = None
        # Resolve the partition salt once, at construction (startup), so _partition() stays a pure,
        # non-raising computation on the per-request serve path (#341 review, finding 1).
        self._salt()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def _salt(self) -> bytes:
        """The server-side partition salt — read-or-created once, stored beside the cache
        (``.partition_salt``). Write-once and shared by every worker on this box, so the partition
        label is stable across workers and restarts (#323). Resolved eagerly at construction so
        ``_partition`` never does I/O on the per-request serve path.

        If the salt cannot be read/created (read-only dir, ENOSPC, lock timeout) the cache **degrades
        to a process-local ephemeral salt** — a cold cache, never a 500 (#341 review, finding 1). The
        salt file must survive a cache-dir clear: deleting it under running workers splits the
        partition namespace until they restart (#341 review, finding 3)."""
        if self._salt_cache is not None:
            return self._salt_cache
        salt_path = self._cache_dir / self._SALT_FILE
        try:
            lock = filelock.FileLock(str(salt_path) + ".lock", timeout=30)
            with lock:
                if salt_path.exists():
                    self._salt_cache = salt_path.read_bytes()
                else:
                    salt = secrets.token_bytes(32)
                    tmp = salt_path.with_name(self._SALT_FILE + ".tmp")
                    tmp.write_bytes(salt)
                    os.replace(tmp, salt_path)
                    self._salt_cache = salt
        except Exception as e:
            logger.warning(
                "partition salt unavailable (%s); using an ephemeral per-process salt "
                "(disk cache degrades to cold, never a 500)", e
            )
            self._salt_cache = secrets.token_bytes(32)
        return self._salt_cache

    def _partition(self, api_key_hash: str) -> str:
        """The on-disk partition label for a caller. Derived from the caller's key-hash via the
        server-side salt (HMAC-SHA256) so the label is neither the key material nor derivable
        from it without the salt (#323). Deterministic — same key-hash -> same label; distinct
        key-hashes -> distinct labels."""
        return hmac.new(self._salt(), api_key_hash.encode(), hashlib.sha256).hexdigest()[:16]

    def _value_dir(self, api_key_hash: str, category: str) -> Path:
        return self._cache_dir / f"{self._partition(api_key_hash)}_{category}_value"

    def _meta_path(self, api_key_hash: str, category: str) -> Path:
        return self._cache_dir / f"{self._partition(api_key_hash)}_{category}_meta.json"

    def _lock_path(self, api_key_hash: str, category: str) -> Path:
        return self._cache_dir / f"{self._partition(api_key_hash)}_{category}.lock"

    def _cleanup_legacy_label(self, api_key_hash: str, category: str) -> None:
        """Remove pre-#323 cache entries labelled by the RAW key-hash (the old, unsalted scheme),
        now that the label is salted. Best-effort — a cache is re-derivable; never fatal."""
        if self._partition(api_key_hash) == api_key_hash:  # astronomically unlikely collision
            return
        for suffix in ("_value", "_meta.json", ".lock", "_dataset.pkl"):
            self._cleanup(self._cache_dir / f"{api_key_hash}_{category}{suffix}")

    def read(self, api_key_hash: str, category: str) -> Optional[Dict[str, Any]]:
        """Read a dataset reconstructed from its disk-cached value.

        Returns:
            Dict with 'dataset', 'file_id', 'timestamp' or None on cache miss/expired.
        """
        from views_faoapi.data.handlers import ForecastDataset

        value_dir = self._value_dir(api_key_hash, category)
        meta_path = self._meta_path(api_key_hash, category)
        lock_path = self._lock_path(api_key_hash, category)

        if not value_dir.exists() or not meta_path.exists():
            return None

        try:
            lock = filelock.FileLock(lock_path, timeout=30)
            with lock:
                with open(meta_path, "r") as f:
                    meta = json.load(f)

                cached_version = meta.get("schema_version")
                if cached_version != CACHE_SCHEMA_VERSION:
                    logger.warning(
                        "Disk cache schema version mismatch for %s/%s: "
                        "expected %s, found %s. Invalidating.",
                        api_key_hash, category,
                        CACHE_SCHEMA_VERSION, cached_version,
                    )
                    self._cleanup(value_dir, meta_path)
                    return None

                cache_age = time.time() - meta.get("timestamp", 0)
                if cache_age > self._ttl_seconds:
                    logger.info(
                        "Disk cache expired for %s/%s (age: %.1f days)",
                        api_key_hash, category, cache_age / 86400,
                    )
                    self._cleanup(value_dir, meta_path)
                    return None

                # S6b-1 (#208): mmap the sample store so a disk-served run pages on demand rather
                # than loading the whole (N,S) block resident — combined with per-month serving
                # (calculate_hdi_map streams months), this keeps the serve-side working set to ~one
                # month. Read-only; every serving read copies (astype/fancy-index) before any math.
                # (POSIX: an in-flight serve's memmap survives a concurrent cache-invalidation
                # rmtree — the unlinked inode stays live until the mapping is GC'd. Host is Linux.)
                dataset = ForecastDataset.from_value(value_dir, mmap=True)

                logger.info(
                    "Disk cache hit for %s/%s (file_id: %s, age: %.1f days)",
                    api_key_hash, category,
                    meta.get("file_id"), cache_age / 86400,
                )
                return {
                    "dataset": dataset,
                    "file_id": meta.get("file_id"),
                    "timestamp": meta.get("timestamp"),
                    # S6a (#208): default legacy so a pre-S6 meta (should already be invalidated by
                    # the schema bump) still repopulates a safe kind; provenance may be absent.
                    "source_kind": meta.get("source_kind", "legacy"),
                    "provenance": meta.get("provenance"),
                }
        except Exception as e:
            logger.warning("Failed to read disk cache: %s", e)
            return None

    def write(self, api_key_hash: str, category: str, dataset: "ForecastDataset", file_id: str,
              source_kind: str = "legacy", provenance: Optional[Dict[str, Any]] = None) -> bool:
        """Write a dataset's VALUE to disk cache (no pickle).

        S6a (#208): ``source_kind`` ("wire"/"legacy") and ``provenance`` round-trip through the
        meta so a disk-served forecast restores the §4.4 cache-identity kind and its C-86 lineage.

        Returns:
            True if successful, False otherwise.
        """
        value_dir = self._value_dir(api_key_hash, category)
        tmp_dir = self._cache_dir / f"{self._partition(api_key_hash)}_{category}_value.tmp"
        meta_path = self._meta_path(api_key_hash, category)
        lock_path = self._lock_path(api_key_hash, category)

        try:
            lock = filelock.FileLock(lock_path, timeout=30)
            with lock:
                self._cleanup_legacy_label(api_key_hash, category)
                # Write to a temp dir then atomically rename into place, so a crashed
                # write never leaves a torn value dir for the next reader.
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir)
                dataset.to_value(tmp_dir)
                if value_dir.exists():
                    shutil.rmtree(value_dir)
                os.replace(tmp_dir, value_dir)

                meta = {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "file_id": file_id,
                    "timestamp": time.time(),
                    "rows": len(dataset.dataframe),
                    "columns": list(dataset.dataframe.columns),
                    "ttl_days": self._ttl_seconds / 86400,
                    "source_kind": source_kind,
                    "provenance": provenance,
                }
                with open(meta_path, "w") as f:
                    json.dump(meta, f)

                logger.info(
                    "Wrote disk cache for %s/%s (file_id: %s, rows: %d)",
                    api_key_hash, category, file_id, len(dataset.dataframe),
                )
                return True
        except Exception as e:
            logger.warning("Failed to write disk cache: %s", e)
            return False

    def staging_dir(self, api_key_hash: str, category: str) -> Path:
        """S6b-2 (#208): a same-filesystem staging dir the wire assembler writes into, so
        :meth:`write_value_dir` can adopt it with an atomic ``os.replace``. **Unique per call**
        (uuid) so concurrent ingests — and :meth:`write`'s own ``_value.tmp`` — never share or
        clobber this path; the caller must :meth:`discard_staging` it on any failure path."""
        staging = self._cache_dir / f"{self._partition(api_key_hash)}_{category}_value.{uuid.uuid4().hex}.tmp"
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def discard_staging(self, staging_dir: Path) -> None:
        """Remove a staging dir — a no-op once :meth:`write_value_dir` has adopted (moved) it, so
        a failed assembly never leaks its full-N preallocated ``values.npy``."""
        shutil.rmtree(staging_dir, ignore_errors=True)

    def write_value_dir(self, api_key_hash: str, category: str, value_dir_src: Path, file_id: str,
                        rows: int, columns: Optional[list] = None, source_kind: str = "wire",
                        provenance: Optional[Dict[str, Any]] = None) -> bool:
        """S6b-2 (#208): adopt an ALREADY-assembled value-dir (written by the wire assembler,
        month-by-month) into the cache slot — the streaming counterpart of :meth:`write`, which
        would need a full in-RAM dataset to call ``to_value``. Atomic rename + meta write; keyed
        on ``file_id`` (the manifest fileId) exactly like :meth:`write`."""
        value_dir = self._value_dir(api_key_hash, category)
        meta_path = self._meta_path(api_key_hash, category)
        lock_path = self._lock_path(api_key_hash, category)
        try:
            lock = filelock.FileLock(lock_path, timeout=30)
            with lock:
                self._cleanup_legacy_label(api_key_hash, category)
                if value_dir.exists():
                    shutil.rmtree(value_dir)
                os.replace(value_dir_src, value_dir)  # atomic (same filesystem)
                meta = {
                    "schema_version": CACHE_SCHEMA_VERSION,
                    "file_id": file_id,
                    "timestamp": time.time(),
                    "rows": rows,
                    "columns": list(columns) if columns is not None else [],
                    "ttl_days": self._ttl_seconds / 86400,
                    "source_kind": source_kind,
                    "provenance": provenance,
                }
                with open(meta_path, "w") as f:
                    json.dump(meta, f)
                logger.info(
                    "Adopted disk cache value-dir for %s/%s (file_id: %s, rows: %d)",
                    api_key_hash, category, file_id, rows,
                )
                return True
        except Exception as e:
            logger.warning("Failed to adopt disk cache value dir: %s", e)
            return False

    def check_file_id(self, api_key_hash: str, category: str, expected_file_id: str) -> bool:
        """Check if disk cache has the expected file_id (for cache invalidation)."""
        meta_path = self._meta_path(api_key_hash, category)
        if not meta_path.exists():
            return False
        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
            return meta.get("file_id") == expected_file_id
        except Exception as e:
            logger.warning("Failed to check disk cache file_id: %s", e)
            return False

    @staticmethod
    def _cleanup(*paths: Path) -> None:
        for p in paths:
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            except Exception as err:
                logger.warning("Failed to clean stale cache entry: %s", err)
