"""Downloaded-file cache (local disk, keyed by file id) — distinct from the dataset value cache.

Extracted from the appwrite god-module (epic #325 S9).
"""
from typing import Optional, Dict, Any
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta
from enum import Enum
import json
import logging


from .results import OperationResult

logger = logging.getLogger(__name__)


class CacheValidationResult(Enum):
    VALID = "valid"
    INVALID_TTL = "invalid_ttl"
    INVALID_TIMESTAMP = "invalid_timestamp"
    NOT_FOUND = "not_found"


@dataclass
class CacheMetadata:
    bucket_id: str
    file_id: str
    path: str
    cached_at: str
    size_bytes: int
    filename: str
    remote_updated_at: Optional[str] = None


class CacheManager:
    def __init__(self, cache_dir: Path, cache_ttl: timedelta):
        self.cache_dir = cache_dir
        self.cache_ttl = cache_ttl
        self.cache_metadata_file = cache_dir / "cache_metadata.json"
        self.cache_metadata: Dict[str, CacheMetadata] = {}
        self._load_cache_metadata()

    def _load_cache_metadata(self):
        if self.cache_metadata_file.exists():
            try:
                with open(self.cache_metadata_file, "r") as f:
                    data = json.load(f)
                    self.cache_metadata = {
                        k: CacheMetadata(**v) for k, v in data.items()
                    }
            except (json.JSONDecodeError, IOError, TypeError) as e:
                logger.warning(f"Failed to load cache metadata: {e}")
                self.cache_metadata = {}

    def _save_cache_metadata(self):
        try:
            data = {k: v.__dict__ for k, v in self.cache_metadata.items()}
            with open(self.cache_metadata_file, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.warning(f"Failed to save cache metadata: {e}")

    def _get_cache_key(self, bucket_id: str, file_id: str) -> str:
        return f"{bucket_id}_{file_id}"

    def _get_cache_path(
        self, bucket_id: str, file_id: str, filename: str = None
    ) -> Path:
        bucket_cache_dir = self.cache_dir / bucket_id
        bucket_cache_dir.mkdir(exist_ok=True)

        if filename:
            return bucket_cache_dir / filename
        return bucket_cache_dir / file_id

    def validate_cache(
        self, bucket_id: str, file_id: str, remote_updated_at: str = None
    ) -> CacheValidationResult:
        cache_key = self._get_cache_key(bucket_id, file_id)

        if cache_key not in self.cache_metadata:
            return CacheValidationResult.NOT_FOUND

        metadata = self.cache_metadata[cache_key]
        cache_path = Path(metadata.path)

        if not cache_path.exists():
            return CacheValidationResult.NOT_FOUND

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

    def remove_from_cache(self, bucket_id: str, file_id: str):
        cache_key = self._get_cache_key(bucket_id, file_id)

        if cache_key in self.cache_metadata:
            cache_path = Path(self.cache_metadata[cache_key].path)
            if cache_path.exists():
                try:
                    cache_path.unlink()
                except OSError as e:
                    logger.warning(f"Failed to delete cache file {cache_path}: {e}")

            del self.cache_metadata[cache_key]
            self._save_cache_metadata()

    def get_cached_file_path(self, bucket_id: str, file_id: str) -> OperationResult:
        cache_key = self._get_cache_key(bucket_id, file_id)

        if cache_key not in self.cache_metadata:
            return OperationResult(
                success=False, error="File not in cache", code="NOT_CACHED"
            )

        cache_path = Path(self.cache_metadata[cache_key].path)

        if not cache_path.exists():
            return OperationResult(
                success=False, error="Cache file missing", code="CACHE_FILE_MISSING"
            )

        return OperationResult(
            success=True,
            data={
                "cache_path": str(cache_path),
                "metadata": self.cache_metadata[cache_key].__dict__,
            },
        )

    def clear_cache(
        self, bucket_id: str = None, older_than_hours: int = None
    ) -> OperationResult:
        deleted_count = 0
        deleted_bytes = 0
        errors = []
        keys_to_delete = []

        for cache_key, metadata in self.cache_metadata.items():
            should_delete = False

            if bucket_id and metadata.bucket_id != bucket_id:
                continue

            if older_than_hours:
                cached_at = datetime.fromisoformat(metadata.cached_at)
                if datetime.now() - cached_at < timedelta(hours=older_than_hours):
                    continue

            should_delete = True

            if should_delete:
                cache_path = Path(metadata.path)
                if cache_path.exists():
                    try:
                        size = cache_path.stat().st_size
                        cache_path.unlink()
                        deleted_count += 1
                        deleted_bytes += size
                    except OSError as e:
                        errors.append(f"Failed to delete {cache_path}: {e}")

                keys_to_delete.append(cache_key)

        for key in keys_to_delete:
            del self.cache_metadata[key]

        self._save_cache_metadata()

        return OperationResult(
            success=True,
            data={
                "deleted_files": deleted_count,
                "deleted_bytes": deleted_bytes,
                "errors": errors if errors else None,
            },
        )

    def get_stats(self) -> Dict[str, Any]:
        total_files = len(self.cache_metadata)
        total_bytes = 0
        by_bucket = {}

        for metadata in self.cache_metadata.values():
            bucket_id = metadata.bucket_id
            size = metadata.size_bytes
            total_bytes += size

            if bucket_id not in by_bucket:
                by_bucket[bucket_id] = {"files": 0, "bytes": 0}

            by_bucket[bucket_id]["files"] += 1
            by_bucket[bucket_id]["bytes"] += size

        return {
            "total_files": total_files,
            "total_size_bytes": total_bytes,
            "total_size_mb": round(total_bytes / (1024 * 1024), 2),
            "cache_dir": str(self.cache_dir),
            "by_bucket": by_bucket,
        }
