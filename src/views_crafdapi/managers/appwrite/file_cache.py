"""Downloaded-file cache (local disk, keyed by file id) — distinct from the dataset value cache.

Extracted from the appwrite god-module (epic #325 S9).
"""
from typing import Optional, Dict, Any
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta
from enum import Enum
import json
import uuid
import os
import logging


from .results import OperationResult

logger = logging.getLogger(__name__)


def _single_path_part(name: str) -> str:
    """A name usable as one file or folder name; anything else is refused, never shortened (#216).

    Refused: empty, ``.``, ``..``, and any name containing a folder separator. Shortening
    ``run1/x`` and ``run2/x`` to ``x`` would make two ids share one cache file.
    """
    part = str(name)
    if (part in ("", ".", "..") or Path(part).name != part or os.sep in part or "/" in part
            or any(ord(ch) < 32 for ch in part)):
        raise ValueError(f"not usable as a file name in the cache: {name!r}")
    return part


class CacheValidationResult(Enum):
    VALID = "valid"
    INVALID_TTL = "invalid_ttl"
    INVALID_TIMESTAMP = "invalid_timestamp"
    INVALID_SIZE = "invalid_size"
    NOT_FOUND = "not_found"


@dataclass
class CacheMetadata:
    bucket_id: str
    file_id: str
    path: str  # recorded for inspection only; never read (#216: the path is derived from the ids)
    cached_at: str
    # C-288 (#169): the size of a whole copy. Recorded once, from a file that `download_file` has
    # just written atomically and checked against the store's `sizeOriginal`, so a later read
    # that finds any other size (torn, truncated, overwritten) knows the copy is not the file.
    size_bytes: int
    filename: str
    remote_updated_at: Optional[str] = None


class CacheManager:
    def __init__(self, cache_dir: Path, cache_ttl: timedelta):
        self.cache_dir = cache_dir
        self._root = Path(cache_dir).resolve()
        # "<root>/" — what every cache path must start with; correct for a root of "/" too.
        self._root_prefix = os.path.join(str(self._root), "")
        self.cache_ttl = cache_ttl
        self.cache_metadata_file = cache_dir / "cache_metadata.json"
        self.cache_metadata: Dict[str, CacheMetadata] = {}
        self._load_cache_metadata()

    def _load_cache_metadata(self):
        if self.cache_metadata_file.exists():
            try:
                with open(self.cache_metadata_file, "r") as f:
                    data = json.load(f)
                    loaded = {k: CacheMetadata(**v) for k, v in data.items()}
                self.cache_metadata = self._drop_old_entries(loaded)
            except (json.JSONDecodeError, IOError, TypeError) as e:
                logger.warning(f"Failed to load cache metadata: {e}")
                self.cache_metadata = {}

    def _drop_old_entries(self, loaded: Dict[str, "CacheMetadata"]) -> Dict[str, "CacheMetadata"]:
        """Keep entries written in the current form; remove the files of the others (#216).

        Before #216 the key was "<bucket>_<file>" and the file was named after the store's file
        name. Those entries are never matched again, so their files are deleted here, but only a
        file that lies inside the cache folder and is reached through no link.
        """
        kept = {}
        for key, entry in loaded.items():
            if key == self._get_cache_key(entry.bucket_id, entry.file_id):
                kept[key] = entry
                continue
            old = os.path.normpath(os.path.join(self._root, os.path.relpath(entry.path, self._root)))
            # realpath(old) == old: no part of the path is a link, not the file and not a folder.
            if (old.startswith(self._root_prefix) and os.path.isfile(old)
                    and os.path.realpath(old) == old):
                try:
                    os.unlink(old)
                except OSError as e:
                    logger.warning("Could not delete an old cache file: %s", e)
        if len(kept) != len(loaded):
            logger.info("Dropped %d cache entries written before #216", len(loaded) - len(kept))
        return kept

    def _save_cache_metadata(self):
        """Written to a temporary file and moved into place, so a kill never leaves it torn."""
        tmp = self.cache_metadata_file.with_name(
            f"{self.cache_metadata_file.name}.tmp-{uuid.uuid4().hex}"
        )
        try:
            data = {k: v.__dict__ for k, v in self.cache_metadata.items()}
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.cache_metadata_file)
        except IOError as e:
            logger.warning(f"Failed to save cache metadata: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _get_cache_key(self, bucket_id: str, file_id: str) -> str:
        # "/" cannot appear in a usable id, so two (bucket, file) pairs never share a key. With "_"
        # they could: ("a_b", "c") and ("a", "b_c").
        return f"{bucket_id}/{file_id}"

    def _get_cache_path(self, bucket_id: str, file_id: str) -> Path:
        """Where a downloaded file is cached: ``<cache folder>/<bucket id>/<file id>`` (#216).

        Only the ids are used, never the file's name in the store: the store accepts any name, and
        a name such as ``../../x`` or ``/etc/x`` would put the file outside the cache folder. The
        file id is also unique, where two names can be the same. The result is checked to be
        inside the cache folder. It only computes the path; the download creates the folder.

        A bucket folder that is a link is refused, and a cached file that is a link is never read
        (see ``_cached_path``). Not covered: a process that can already write inside the cache
        folder could create such a link between this check and the write. Only the service account
        can write there.
        """
        # os.path.realpath + a prefix check: the form CodeQL recognises as a path guard. Only the
        # bucket folder is resolved; the file part is a single name, so a cached file that is
        # itself a link is returned as the link, never as its target.
        bucket_dir = os.path.realpath(os.path.join(self._root, _single_path_part(bucket_id)))
        # The bucket folder must lie strictly below the cache folder (not be the cache folder
        # itself, where cache_metadata.json lives).
        if not bucket_dir.startswith(self._root_prefix):
            raise ValueError(f"bucket folder for {bucket_id!r} falls outside the cache folder")
        # A bucket folder that is a link could make two bucket ids share one folder.
        if bucket_dir != os.path.join(str(self._root), _single_path_part(bucket_id)):
            raise ValueError(f"bucket folder for {bucket_id!r} is a link; not used")
        full = os.path.normpath(os.path.join(bucket_dir, _single_path_part(file_id)))
        # Given the check above this cannot fail. It is kept on purpose: CodeQL accepts a path as
        # safe only when the value used is normalised and then checked in its own statement.
        if not full.startswith(self._root_prefix):
            raise ValueError(f"cache path for {file_id!r} falls outside the cache folder")
        return Path(full)

    def _cached_path(self, metadata: "CacheMetadata") -> Optional[Path]:
        """The cache file for an entry, derived from its ids, never from the stored path (#216)."""
        try:
            path = self._get_cache_path(metadata.bucket_id, metadata.file_id)
        except ValueError as e:
            logger.warning("Cache entry %s/%s cannot be used: %s", metadata.bucket_id,
                           metadata.file_id, e)
            return None
        if path.is_symlink():
            logger.warning("Cache entry %s/%s is a link; not used", metadata.bucket_id,
                           metadata.file_id)
            return None
        return path

    def validate_cache(
        self, bucket_id: str, file_id: str, remote_updated_at: str = None
    ) -> CacheValidationResult:
        cache_key = self._get_cache_key(bucket_id, file_id)

        if cache_key not in self.cache_metadata:
            return CacheValidationResult.NOT_FOUND

        metadata = self.cache_metadata[cache_key]
        cache_path = self._cached_path(metadata)
        if cache_path is None:
            return CacheValidationResult.NOT_FOUND

        size = self._size_on_disk(cache_path)
        if size is None:
            return CacheValidationResult.NOT_FOUND
        if size != metadata.size_bytes:
            return CacheValidationResult.INVALID_SIZE

        cached_at = datetime.fromisoformat(metadata.cached_at)
        if datetime.now() - cached_at > self.cache_ttl:
            return CacheValidationResult.INVALID_TTL

        if remote_updated_at:
            try:
                remote_updated = datetime.fromisoformat(
                    remote_updated_at.replace("Z", "+00:00")
                )
                cached_at_aware = cached_at.replace(tzinfo=remote_updated.tzinfo)
                if remote_updated > cached_at_aware:
                    return CacheValidationResult.INVALID_TIMESTAMP
            except (ValueError, AttributeError):
                pass

        return CacheValidationResult.VALID

    def add_to_cache(
        self,
        bucket_id: str,
        file_id: str,
        file_path: Path,
        file_metadata: Dict[str, Any] = None,
    ):
        cache_key = self._get_cache_key(bucket_id, file_id)

        self.cache_metadata[cache_key] = CacheMetadata(
            bucket_id=bucket_id,
            file_id=file_id,
            path=str(file_path),
            cached_at=datetime.now().isoformat(),
            size_bytes=file_path.stat().st_size if file_path.exists() else 0,
            filename=file_metadata.get("name") if file_metadata else file_path.name,
            remote_updated_at=(
                file_metadata.get("$updatedAt") if file_metadata else None
            ),
        )

        self._save_cache_metadata()

    @staticmethod
    def _size_on_disk(cache_path: Path) -> Optional[int]:
        """The file's size, or None if it is gone — including removed since the caller looked."""
        try:
            return cache_path.stat().st_size
        except FileNotFoundError:
            return None

    def get_cached_file_path(self, bucket_id: str, file_id: str) -> OperationResult:
        cache_key = self._get_cache_key(bucket_id, file_id)

        if cache_key not in self.cache_metadata:
            return OperationResult(
                success=False, error="File not in cache", code="NOT_CACHED"
            )

        metadata = self.cache_metadata[cache_key]
        cache_path = self._cached_path(metadata)
        if cache_path is None:
            return OperationResult(
                success=False, error="File not in cache", code="NOT_CACHED"
            )
        size = self._size_on_disk(cache_path)
        if size is None:
            return OperationResult(
                success=False, error="Cache file missing", code="CACHE_FILE_MISSING"
            )

        if size != metadata.size_bytes:
            return OperationResult(
                success=False,
                error=f"cached copy is {size} bytes, a whole copy is {metadata.size_bytes}",
                code="CACHE_FILE_WRONG_SIZE",
            )

        return OperationResult(
            success=True,
            data={
                "cache_path": str(cache_path),
                "metadata": metadata.__dict__,
            },
        )
