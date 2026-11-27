"""The Appwrite file manager: download files, list buckets, read file and bucket details. Read only.

Extracted from the appwrite god-module (epic #325 S9).
"""
from typing import List
from pathlib import Path
from datetime import timedelta
import json
import os
import shutil
import uuid
import logging

from appwrite.client import Client
from appwrite.services.storage import Storage
from appwrite.services.databases import Databases
from appwrite.services.tables_db import TablesDB
from appwrite.services.users import Users
from appwrite.exception import AppwriteException
from appwrite.query import Query

from .constants import (
    DEFAULT_CACHE_TTL_HOURS, DEFAULT_PAGE_LIMIT, DEFAULT_CONNECT_TIMEOUT_SECONDS,
)
from .sdk_compat import _as_dict, _get
from .results import OperationResult
from .config import AppwriteConfig
from .auth import AuthFactory
from .file_cache import CacheManager, CacheValidationResult
from .metadata import MetadataManager
logger = logging.getLogger(__name__)


class AppWriteFileManager:
    def __init__(self, config: AppwriteConfig):
        self.config = config
        self.client = Client()
        self.client.set_endpoint(config.endpoint).set_project(config.project_id)

        self._inject_timeout(config.timeout_seconds)

        # Initialize authentication
        self.auth_manager = AuthFactory.create_auth(config.auth_method)
        auth_result = self.auth_manager.setup(self.client, config.credentials)
        if not auth_result.success:
            raise ValueError(f"Authentication failed: {auth_result.error}")

        # Initialize services
        self.storage = Storage(self.client)
        self.users = Users(self.client)
        self.databases = Databases(self.client)
        self.tables = TablesDB(self.client)

        # Initialize managers
        self.metadata_manager = MetadataManager(self.databases, config, self.tables)
        self.cache_manager = self._setup_cache()

    def _inject_timeout(self, timeout_seconds: int) -> None:
        """Wrap this client's call() to enforce a timeout on all SDK HTTP calls.

        The Appwrite Python SDK (19.x) calls requests.request() without a timeout
        parameter, so any SDK call can hang indefinitely. This wraps the instance's
        client.call() to inject timeout into the underlying requests call via a
        temporary module-level patch scoped to each call invocation.

        Not thread-safe: the module-level patch on appwrite.client.requests.request
        is restored in a try/finally, but concurrent calls from different threads
        can observe each other's patch. Acceptable while the API runs single-threaded
        (uvicorn single-worker). Revisit if moving to multi-threaded workers.
        """
        import appwrite.client as _appwrite_client
        _original_call = self.client.call
        timeout = (min(timeout_seconds, DEFAULT_CONNECT_TIMEOUT_SECONDS), timeout_seconds)

        def _timed_call(*args, **kwargs):
            saved = _appwrite_client.requests.request

            def _timed_request(*r_args, **r_kwargs):
                r_kwargs.setdefault("timeout", timeout)
                return saved(*r_args, **r_kwargs)

            _appwrite_client.requests.request = _timed_request
            try:
                return _original_call(*args, **kwargs)
            finally:
                _appwrite_client.requests.request = saved

        self.client.call = _timed_call

    def _setup_cache(self) -> CacheManager:
        try:
            if not self.config.cache_dir:
                cache_dir = (
                    getattr(self.config.path_manager, "cache", Path("."))
                    / "appwrite_cache"
                )
            else:
                cache_dir = Path(self.config.cache_dir)

            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_ttl = timedelta(hours=self.config.cache_ttl_hours)

            return CacheManager(cache_dir, cache_ttl)

        except Exception as e:
            logger.warning(f"Cache setup failed: {e}. Using default cache directory.")
            cache_dir = Path(".appwrite_cache")
            cache_dir.mkdir(exist_ok=True)
            return CacheManager(cache_dir, timedelta(hours=DEFAULT_CACHE_TTL_HOURS))

    def download_file(
        self,
        bucket_id: str,
        file_id: str,
        save_path: str = None,
        use_cache: bool = True,
        validate_cache: bool = True,
    ) -> OperationResult:
        try:
            # Get file metadata for cache validation
            file_metadata = None
            if validate_cache or use_cache:
                file_info = self.get_file(bucket_id, file_id)
                if file_info.success:
                    file_metadata = file_info.data

            # Check cache if enabled
            if use_cache:
                remote_updated = (
                    file_metadata.get("$updatedAt") if file_metadata else None
                )
                cache_validation = self.cache_manager.validate_cache(
                    bucket_id, file_id, remote_updated
                )

                if cache_validation == CacheValidationResult.VALID:
                    cache_result = self.cache_manager.get_cached_file_path(
                        bucket_id, file_id
                    )
                    if cache_result.success:
                        cache_path = Path(cache_result.data["cache_path"])

                        if save_path:
                            shutil.copy2(cache_path, save_path)
                            return OperationResult(
                                success=True,
                                data={"save_path": save_path, "from_cache": True},
                                code="SAVED_FROM_CACHE",
                            )
                        else:
                            with open(cache_path, "rb") as f:
                                file_bytes = f.read()

                            return OperationResult(
                                success=True,
                                data={"file_bytes": file_bytes, "from_cache": True},
                                code="RETURNED_FROM_CACHE",
                            )

            # Download from remote
            file_bytes = self.storage.get_file_download(bucket_id, file_id)

            # C-288 (#169): a body whose length differs from the store's declared size is a
            # cut-off (or corrupted) download, not the file. Only bytes received as bytes can be
            # compared: a deserialized manifest (below) is re-encoded and need not match.
            received_as_bytes = isinstance(file_bytes, (bytes, bytearray))
            declared_size = None
            if received_as_bytes and file_metadata:
                declared_size = file_metadata.get("sizeOriginal")
                if declared_size is not None and len(file_bytes) != declared_size:
                    return OperationResult(
                        success=False,
                        error=(f"Download failed: received {len(file_bytes)} bytes, the store "
                               f"declares {declared_size} for {file_id}"),
                        code="DOWNLOAD_INCOMPLETE",
                    )

            # #287 follow-up: the Appwrite SDK auto-deserializes a JSON file (the run manifest)
            # to a dict — not bytes — so the cache write (and callers expecting bytes) blow up
            # with "a bytes-like object is required, not 'dict'". This is the FIRST download in
            # the wire-ingest path, so a cold-cache manifest fetch refused the entire run. Parquet
            # shards/sidecar come back as bytes and are untouched. Normalize to bytes; the manifest
            # is not hash-verified (it is the root of trust), so re-serialization is safe.
            if isinstance(file_bytes, dict):
                file_bytes = json.dumps(file_bytes).encode("utf-8")
            elif isinstance(file_bytes, str):
                file_bytes = file_bytes.encode("utf-8")

            # Some downloads are served but not cached:
            # - C-288: the size could not be checked (the metadata lookup failed), and a cached
            #   entry is trusted on every later read;
            # - #216: the ids are not usable as a cache file name, or the folder cannot be made.
            cache_path = None
            if received_as_bytes and declared_size is None:
                reason = "file metadata unavailable, so its size cannot be checked"
            else:
                try:
                    cache_path = self.cache_manager._get_cache_path(bucket_id, file_id)
                    cache_path.parent.mkdir(exist_ok=True)
                    reason = None
                except (ValueError, OSError) as e:
                    cache_path = None
                    reason = f"no usable cache file ({e})"
            if cache_path is None:
                logger.warning("Serving %s/%s without caching it: %s", bucket_id, file_id, reason)
                if save_path:
                    Path(save_path).write_bytes(file_bytes)
                    return OperationResult(
                        success=True,
                        data={"save_path": save_path, "from_cache": False},
                        code="SAVED_FROM_REMOTE",
                    )
                return OperationResult(
                    success=True,
                    data={"file_bytes": file_bytes, "from_cache": False},
                    code="RETURNED_FROM_REMOTE",
                )

            # Save to cache atomically (C-288, the pattern of `disk_cache._write_meta`): a kill
            # mid-write leaves the previous file or none at the cache path, never a torn one that
            # the next read would trust.
            tmp = cache_path.with_name(f"{cache_path.name}.tmp-{uuid.uuid4().hex}")
            try:
                with open(tmp, "wb") as f:
                    f.write(file_bytes)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, cache_path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

            self.cache_manager.add_to_cache(
                bucket_id, file_id, cache_path, file_metadata
            )

            # Handle save_path
            if save_path:
                shutil.copy2(cache_path, save_path)
                return OperationResult(
                    success=True,
                    data={"save_path": save_path, "from_cache": False},
                    code="SAVED_FROM_REMOTE",
                )
            else:
                return OperationResult(
                    success=True,
                    data={"file_bytes": file_bytes, "from_cache": False},
                    code="RETURNED_FROM_REMOTE",
                )

        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"Download failed: {e.message}", code=e.type, status=e.code
            )
        except IOError as e:
            return OperationResult(
                success=False, error=f"File operation failed: {str(e)}", code="IO_ERROR"
            )
        except Exception as e:
            return OperationResult(
                success=False, error=f"Download failed: {str(e)}", code="UNEXPECTED_ERROR"
            )

    def list_files(
        self,
        bucket_id: str,
        queries: List[str] = None,
        limit: int = DEFAULT_PAGE_LIMIT,
        offset: int = 0,
        order_field: str = None,
        order_type: str = "ASC",
    ) -> OperationResult:
        try:
            if queries is None:
                queries = []

            query_list = queries.copy()
            query_list.append(Query.limit(limit))
            query_list.append(Query.offset(offset))

            if order_field:
                if order_type.upper() == "DESC":
                    query_list.append(Query.order_desc(order_field))
                else:
                    query_list.append(Query.order_asc(order_field))

            result = self.storage.list_files(bucket_id, query_list)

            return OperationResult(
                success=True,
                data={
                    "files": [_as_dict(f) for f in (_get(result, "files") or [])],
                    "total": _get(result, "total") or 0,
                },
            )

        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"List files failed: {e.message}", code=e.type
            )
        except Exception as e:
            return OperationResult(
                success=False, error=f"List files failed: {str(e)}", code="UNEXPECTED_ERROR"
            )

    def get_file(self, bucket_id: str, file_id: str) -> OperationResult:
        try:
            result = self.storage.get_file(bucket_id, file_id)
            return OperationResult(success=True, data=_as_dict(result))

        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"Get file failed: {e.message}", code=e.type, status=e.code
            )
        except Exception as e:
            return OperationResult(
                success=False, error=f"Get file failed: {str(e)}", code="UNEXPECTED_ERROR"
            )

    def get_bucket(self, bucket_id: str) -> OperationResult:
        try:
            result = self.storage.get_bucket(bucket_id)
            return OperationResult(success=True, data=_as_dict(result))

        except AppwriteException as e:
            # Check if this is a bucket not found error
            if "Storage bucket with the requested ID could not be found" in e.message:
                return OperationResult(
                    success=False, error=e.message, code="storage_bucket_not_found"
                )

            return OperationResult(
                success=False, error=f"Get bucket failed: {e.message}", code=e.type
            )
        except Exception as e:
            return OperationResult(
                success=False, error=f"Get bucket failed: {str(e)}", code="UNEXPECTED_ERROR"
            )

    def list_buckets(
        self, search: str = None, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
    ) -> OperationResult:
        try:
            queries = []
            if search:
                queries.append(Query.search("name", search))
            queries.extend([Query.limit(limit), Query.offset(offset)])

            result = self.storage.list_buckets(queries)
            return OperationResult(
                success=True,
                data={
                    "buckets": [_as_dict(b) for b in (_get(result, "buckets") or [])],
                    "total": _get(result, "total") or 0,
                },
            )

        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"List buckets failed: {e.message}", code=e.type,
                status=e.code,
            )
        except Exception as e:
            return OperationResult(
                success=False, error=f"List buckets failed: {str(e)}", code="UNEXPECTED_ERROR"
            )

