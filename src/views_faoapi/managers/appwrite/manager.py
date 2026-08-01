"""The Appwrite file manager — read (download/list/get) fused with write/provision (upload/create/delete).

Extracted from the appwrite god-module (epic #325 S9).
"""
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime, timedelta
import json
import shutil
import logging
import hashlib

from appwrite.client import Client
from appwrite.services.storage import Storage
from appwrite.services.databases import Databases
from appwrite.services.users import Users
from appwrite.input_file import InputFile
from appwrite.id import ID
from appwrite.exception import AppwriteException
from appwrite.query import Query

from .constants import (
    DEFAULT_CACHE_TTL_HOURS, DEFAULT_PAGE_LIMIT, DEFAULT_CONNECT_TIMEOUT_SECONDS,
)
from .sdk_compat import _as_dict, _get
from .results import OperationResult
from .provisioning import ProvisioningError, _require_provisioning
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

        # Initialize managers
        self.metadata_manager = MetadataManager(self.databases, config)
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

    def _calculate_file_hash(
        self, file_path: str = None, file_bytes: bytes = None
    ) -> str:
        sha256_hash = hashlib.sha256()

        if file_path:
            with open(file_path, "rb") as f:
                for byte_block in iter(lambda: f.read(4096), b""):
                    sha256_hash.update(byte_block)
        elif file_bytes:
            sha256_hash.update(file_bytes)
        else:
            raise ValueError("Either file_path or file_bytes must be provided")

        return sha256_hash.hexdigest()

    def _file_exists_by_hash(
        self, bucket_id: str, file_hash: str, filename: str = None
    ) -> OperationResult:
        try:
            # First try to find by hash in metadata
            search_result = self.metadata_manager.check_file_exists_by_hash(
                file_hash,
                self.config.collection_name,
                self.config.collection_id,
                self.config.database_id,
            )

            if search_result.success:
                return OperationResult(
                    success=True, data=search_result.data, code="FOUND_BY_HASH"
                )

            # Fallback to filename check if hash not found - but use efficient query
            if filename:
                try:
                    # Use query instead of listing all files
                    result = self.storage.list_files(
                        bucket_id, [Query.equal("name", filename), Query.limit(1)]
                    )

                    files = _get(result, "files") or []
                    if files:
                        return OperationResult(
                            success=True, data=_as_dict(files[0]), code="FOUND_BY_NAME"
                        )
                except AppwriteException as query_error:
                    logger.warning(
                        f"Filename query failed, falling back to list: {query_error}"
                    )
                    # Fallback to original list-based approach if query fails
                    all_files = []
                    offset = 0
                    limit = DEFAULT_PAGE_LIMIT

                    while True:
                        result = self.storage.list_files(
                            bucket_id, [Query.limit(limit), Query.offset(offset)]
                        )
                        files_chunk = _get(result, "files") or []
                        all_files.extend(files_chunk)

                        if len(files_chunk) < limit:
                            break
                        offset += limit

                    for file in all_files:
                        if _get(file, "name") == filename:
                            return OperationResult(
                                success=True, data=_as_dict(file), code="FOUND_BY_NAME"
                            )

            return OperationResult(success=False, code="NOT_FOUND")

        except AppwriteException as e:
            return OperationResult(success=False, error=e.message, code=e.type)

    def _build_metadata_document(
        self,
        file_id: str,
        bucket_id: str,
        filename: str,
        upload_result: Dict[str, Any],
        metadata: Dict[str, Any],
        file_hash: str = None,
    ) -> Dict[str, Any]:
        base_document = {
            "fileId": file_id,
            "bucketId": bucket_id,
            "filename": filename,
            "mime_type": metadata.get("mime_type", "application/octet-stream"),
            "uploaded_at": datetime.now().isoformat(),
            "file_hash": file_hash,
            **metadata,
        }

        if "data" in upload_result and "sizeOriginal" in upload_result["data"]:
            base_document["file_size"] = upload_result["data"]["sizeOriginal"]

        return {k: v for k, v in base_document.items() if v is not None}

    def _store_metadata_document(
        self,
        database_id: str,
        collection_id: str,
        file_id: str,
        metadata_document: Dict[str, Any],
    ) -> OperationResult:
        try:
            existing_docs = self.databases.list_documents(
                database_id, collection_id, queries=[Query.equal("fileId", file_id)]
            )

            if _get(existing_docs, "total") > 0:
                doc = _as_dict(_get(existing_docs, "documents")[0])
                doc_id = doc.get("$id")
                result = self.databases.update_document(
                    database_id, collection_id, doc_id, metadata_document
                )
                return OperationResult(success=True, data=_as_dict(result), code="UPDATED")
            else:
                result = self.databases.create_document(
                    database_id, collection_id, ID.unique(), metadata_document
                )
                return OperationResult(success=True, data=_as_dict(result), code="CREATED")

        except AppwriteException as e:
            return OperationResult(success=False, error=e.message, code=e.type)

    def upload_file(
        self,
        bucket_id: str,
        file_path: str,
        file_id: str = None,
        permissions: List[str] = None,
        check_duplicates: bool = True,
        overwrite: bool = False,
    ) -> OperationResult:
        try:
            filename = Path(file_path).name
            file_hash = None

            if check_duplicates:
                file_hash = self._calculate_file_hash(file_path=file_path)
                duplicate_check = self._file_exists_by_hash(
                    bucket_id, file_hash, filename
                )

                if duplicate_check.success:
                    existing_file = duplicate_check.data

                    if overwrite:
                        delete_result = self.delete_file(
                            bucket_id, existing_file["$id"]
                        )
                        if not delete_result.success:
                            return delete_result
                    else:
                        return OperationResult(
                            success=True, data=existing_file, code="EXISTS"
                        )

            file_id = file_id or ID.unique()
            permissions = permissions or []

            input_file = InputFile.from_path(file_path)
            result = self.storage.create_file(
                bucket_id=bucket_id,
                file_id=file_id,
                file=input_file,
                permissions=permissions,
            )

            return OperationResult(success=True, data=_as_dict(result), code="CREATED")

        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"Upload failed: {e.message}", code=e.type
            )
        except Exception as e:
            return OperationResult(
                success=False, error=f"Unexpected error: {str(e)}", code="UNKNOWN_ERROR"
            )

    def upload_file_from_bytes(
        self,
        bucket_id: str,
        file_bytes: bytes,
        filename: str,
        file_id: str = None,
        permissions: List[str] = None,
        check_duplicates: bool = True,
        overwrite: bool = False,
    ) -> OperationResult:
        try:
            file_hash = None

            if check_duplicates:
                file_hash = self._calculate_file_hash(file_bytes=file_bytes)
                duplicate_check = self._file_exists_by_hash(
                    bucket_id, file_hash, filename
                )

                if duplicate_check.success:
                    existing_file = duplicate_check.data

                    if overwrite:
                        delete_result = self.delete_file(
                            bucket_id, existing_file["$id"]
                        )
                        if not delete_result.success:
                            return delete_result
                    else:
                        return OperationResult(
                            success=True, data=existing_file, code="EXISTS"
                        )

            file_id = file_id or ID.unique()
            permissions = permissions or []

            input_file = InputFile.from_bytes(file_bytes, filename=filename)
            result = self.storage.create_file(
                bucket_id=bucket_id,
                file_id=file_id,
                file=input_file,
                permissions=permissions,
            )

            return OperationResult(success=True, data=_as_dict(result), code="CREATED")

        except AppwriteException as e:
            return OperationResult(
                success=False,
                error=f"Upload from bytes failed: {e.message}",
                code=e.type,
            )

    def upload_file_with_metadata(
    self,
    bucket_id: str,
    file_path: str,
    filename: str,
    metadata: Dict[str, Any],
    file_id: str = None,
    permissions: List[str] = None,
    collection_name: str = None,
    collection_id: str = None
) -> OperationResult:
        """
        Upload a file to Appwrite storage and store its metadata in a database collection.

        Args:
            bucket_id: The ID of the bucket to upload to
            file_path: Path to the file to upload
            filename: Name to give the file in storage
            metadata: Dictionary of metadata to store
            file_id: Optional file ID (if None, one will be generated)
            permissions: Optional list of permissions for the file
            collection_name: Optional collection name (defaults to config)
            collection_id: Optional collection ID (defaults to config)

        Returns:
            OperationResult with success status and data/error information
        """
        # Use defaults from config if not provided
        if collection_name is None:
            collection_name = self.config.collection_name
        if collection_id is None:
            collection_id = self.config.collection_id

        # Calculate file hash for metadata
        file_hash = self._calculate_file_hash(file_path=file_path)

        # Check if file already exists by hash in metadata
        existing_metadata = self.metadata_manager.check_file_exists_by_hash(
            file_hash, collection_name, collection_id, self.config.database_id
        )

        # CRITICAL FIX: Verify file exists in BOTH metadata AND storage
        should_update_metadata_only = False
        if existing_metadata.success and existing_metadata.code == "FOUND_BY_HASH" and not file_id:
            existing_file_id = existing_metadata.data.get("fileId")
            
            # Verify the file actually exists in storage
            if existing_file_id:
                file_check = self.get_file(bucket_id, existing_file_id)
                
                if file_check.success:
                    # File exists in both metadata and storage
                    should_update_metadata_only = self.config.allow_metadata_only_updates
                    logger.info(f"File {existing_file_id} exists in both metadata and storage")
                elif file_check.code == "storage_file_not_found" or (
                    # The SDK leaves e.type (-> code) None/variant for non-JSON error bodies (e.g. a
                    # 404 via a proxy), so also trust the message like the sibling get_bucket does.
                    # Scope the message strictly to a *file* not-found: a wrong/missing bucket_id yields
                    # storage_bucket_not_found whose message ALSO contains "could not be found", but the
                    # file may still exist in the correct bucket — deleting its metadata then would be the
                    # exact false-orphan (C-231) this gate exists to prevent. Exclude it by code AND by the
                    # "bucket" keyword. (#341 max review, finding 1.)
                    file_check.code != "storage_bucket_not_found"
                    and file_check.error is not None
                    and "could not be found" in file_check.error.lower()
                    and "bucket" not in file_check.error.lower()
                ):
                    # The file is GENUINELY gone (a true orphan) - clean up the metadata and re-upload.
                    logger.warning(f"File {existing_file_id} found in metadata but missing from storage, will re-upload")
                    existing_doc_id = existing_metadata.data.get("$id")
                    if existing_doc_id:
                        try:
                            self.databases.delete_document(
                                database_id=self.config.database_id,
                                collection_id=collection_id,
                                document_id=existing_doc_id
                            )
                            logger.info(f"Deleted orphaned metadata document: {existing_doc_id}")
                        except Exception as e:
                            logger.warning(f"Failed to delete orphaned metadata: {str(e)}")
                else:
                    # get_file failed for a reason OTHER than a genuine not-found - e.g. the key lacks
                    # storage-read scope, or an unexpected error. We CANNOT conclude the metadata is
                    # orphaned, and deleting it here on a mis-scoped key destroys valid metadata (the
                    # C-231 shape, #322). Refuse to delete; fail visible with the underlying code.
                    logger.error(
                        f"Cannot verify storage for {existing_file_id}: get_file failed with "
                        f"code={file_check.code!r} ({file_check.error}). Refusing to delete metadata."
                    )
                    return OperationResult(
                        success=False,
                        error=(
                            f"Could not verify whether file {existing_file_id} exists in storage "
                            f"(code={file_check.code!r}); refusing to delete metadata as a false orphan."
                        ),
                        code=file_check.code,
                    )

        if should_update_metadata_only:
            logger.info(f"File with hash {file_hash} already exists, updating metadata only")

            # Get existing document ID
            existing_doc_id = existing_metadata.data.get("$id")
            existing_file_id = existing_metadata.data.get("fileId")

            if not existing_doc_id:
                logger.warning("Existing metadata found but no document ID available")
                # Fall through to normal upload
            else:
                # Update the metadata document
                updated_metadata = {**metadata, "file_hash": file_hash}

                update_result = self.metadata_manager.update_file_metadata(
                    file_id=existing_file_id,
                    metadata_updates=updated_metadata,
                    collection_name=collection_name,
                    collection_id=collection_id,
                    database_id=self.config.database_id
                )

                if update_result.success:
                    return OperationResult(
                        success=True,
                        data={
                            "file_id": existing_file_id,
                            "document_id": existing_doc_id,
                            "metadata": updated_metadata,
                            "message": "Metadata updated for existing file"
                        },
                        code="METADATA_UPDATED"
                    )
                else:
                    logger.warning(f"Failed to update metadata: {update_result.error}")
                    # Fall through to normal upload

        # CRITICAL: If file exists by NAME but different hash, DELETE the old one
        if existing_metadata.success and existing_metadata.code == "FOUND_BY_NAME":
            logger.info(f"File '{filename}' exists with different hash, deleting old version")
            old_file_id = existing_metadata.data.get("fileId")
            old_doc_id = existing_metadata.data.get("$id")

            if old_file_id:
                # Delete the old file from storage
                delete_result = self.delete_file(bucket_id, old_file_id)
                if not delete_result.success:
                    logger.warning(f"Failed to delete old file from storage: {delete_result.error}")
                    # Continue anyway - the upload might still work

            if old_doc_id:
                # Delete the old metadata document
                try:
                    self.databases.delete_document(
                        database_id=self.config.database_id,
                        collection_id=collection_id,
                        document_id=old_doc_id
                    )
                    logger.info(f"Deleted old metadata document: {old_doc_id}")
                except Exception as e:
                    logger.warning(f"Failed to delete old metadata: {str(e)}")

        # Ensure metadata infrastructure exists
        collection_result = self.metadata_manager.create_metadata_collection_if_not_exists(
            metadata, collection_name, collection_id, self.config.database_id
        )
        if not collection_result.success:
            return OperationResult(
                success=False,
                error=collection_result.error,
                code=collection_result.code
            )

        # Add file_hash to metadata
        metadata["file_hash"] = file_hash

        # Upload file - DISABLE duplicate checking since we already handled it above
        upload_result = self.upload_file(
            bucket_id, 
            file_path, 
            file_id, 
            permissions, 
            check_duplicates=False,  # Don't check again - we already handled it
            overwrite=False
        )

        if not upload_result.success:
            return OperationResult(
                success=False,
                error=upload_result.error,
                code=upload_result.code
            )

        # Get the uploaded file ID
        uploaded_file_id = upload_result.data.get("$id")
        
        # Get database and collection IDs from the collection result
        database_id = collection_result.data.get("database_id") or self.config.database_id
        coll_id = collection_result.data.get("collection_id") or collection_id

        # Prepare metadata with file reference
        metadata_with_file_ref = {
            **metadata,
            "fileId": uploaded_file_id,
            "filename": filename,
            "bucketId": bucket_id,
            "uploaded_at": datetime.now().isoformat()
        }

        # Store metadata in database using _store_metadata_document
        metadata_result = self._store_metadata_document(
            database_id=database_id,
            collection_id=coll_id,
            file_id=uploaded_file_id,
            metadata_document=metadata_with_file_ref
        )

        if not metadata_result.success:
            # Metadata storage failed, but file was uploaded
            logger.error(f"File uploaded but metadata storage failed: {metadata_result.error}")
            return OperationResult(
                success=False,
                error=f"File uploaded but metadata storage failed: {metadata_result.error}",
                data={
                    "file_id": uploaded_file_id,
                    "file_data": upload_result.data
                },
                code="PARTIAL_SUCCESS"
            )

        # Success - both file and metadata stored
        return OperationResult(
            success=True,
            data={
                "file_id": uploaded_file_id,
                "document_id": metadata_result.data.get("$id"),
                "file_data": upload_result.data,
                "metadata": metadata_with_file_ref
            },
            code="UPLOAD_SUCCESS"
        )

    def upload_file_from_bytes_with_metadata(
        self,
        bucket_id: str,
        file_bytes: bytes,
        filename: str,
        metadata: Dict[str, Any],
        file_id: str = None,
        permissions: List[str] = None,
        collection_name: str = None,
        collection_id: str = None,
    ) -> OperationResult:
        # Use defaults from config if not provided
        if collection_name is None:
            collection_name = self.config.collection_name
        if collection_id is None:
            collection_id = self.config.collection_id

        # Calculate file hash for metadata
        file_hash = self._calculate_file_hash(file_bytes=file_bytes)

        # Check if file already exists by hash
        existing_metadata = self.metadata_manager.check_file_exists_by_hash(
            file_hash, collection_name, collection_id, self.config.database_id
        )

        # Use same logic as upload_file_with_metadata for consistency
        should_update_metadata_only = (
            existing_metadata.success
            and not file_id
            and self.config.allow_metadata_only_updates
        )

        if should_update_metadata_only:
            logger.info(
                f"File with hash {file_hash} already exists, updating metadata only"
            )

            existing_file_id = existing_metadata.data.get("fileId")

            # Ensure collection exists with new metadata fields
            collection_result = (
                self.metadata_manager.create_metadata_collection_if_not_exists(
                    metadata, collection_name, collection_id, self.config.database_id
                )
            )
            if not collection_result.success:
                return OperationResult(
                    success=False,
                    error=collection_result.error,
                    code=collection_result.code,
                )

            # Update the metadata
            metadata_update = metadata.copy()
            metadata_update["file_hash"] = file_hash
            metadata_update["filename"] = filename
            metadata_update["uploaded_at"] = datetime.now().isoformat()

            update_result = self.metadata_manager.update_file_metadata(
                file_id=existing_file_id,
                metadata_updates=metadata_update,
                collection_name=collection_name,
                collection_id=collection_id,
                database_id=self.config.database_id,
            )

            if update_result.success:
                # Get the full file info to return
                file_info = self.get_file(bucket_id, existing_file_id)
                return OperationResult(
                    success=True,
                    data={
                        **(file_info.data if file_info.success else {}),
                        "metadata": update_result.data,
                        "metadata_action": "UPDATED",
                    },
                    code="EXISTS_METADATA_UPDATED",
                )
            else:
                return OperationResult(
                    success=False,
                    error=f"Failed to update metadata: {update_result.error}",
                    code="METADATA_UPDATE_FAILED",
                )

        # Ensure metadata infrastructure exists
        collection_result = (
            self.metadata_manager.create_metadata_collection_if_not_exists(
                metadata, collection_name, collection_id, self.config.database_id
            )
        )
        if not collection_result.success:
            return OperationResult(
                success=False,
                error=collection_result.error,
                code=collection_result.code,
            )

        # Add file_hash to metadata
        metadata["file_hash"] = file_hash

        # Upload file (this will handle duplicates based on check_duplicates parameter)
        upload_result = self.upload_file_from_bytes(
            bucket_id,
            file_bytes,
            filename,
            file_id,
            permissions,
            check_duplicates=True,  # Let the base method handle duplicates
            overwrite=False,  # Don't overwrite by default in metadata flow
        )

        if not upload_result.success:
            return upload_result

        file_id = upload_result.data["$id"]
        database_id = collection_result.data["database_id"]
        coll_id = collection_result.data["collection_id"]

        # Create and store metadata
        try:
            metadata_document = self._build_metadata_document(
                file_id,
                bucket_id,
                filename,
                {"data": upload_result.data},
                metadata,
                file_hash,
            )

            metadata_result = self._store_metadata_document(
                database_id, coll_id, file_id, metadata_document
            )

            if metadata_result.success:
                upload_result.data["metadata"] = metadata_result.data
                upload_result.data["metadata_action"] = metadata_result.code

            return OperationResult(
                success=True, data=upload_result.data, code="CREATED_WITH_METADATA"
            )

        except AppwriteException as e:
            logger.error(f"Metadata handling failed: {e.message}")
            # Rollback: delete the uploaded file if metadata fails
            try:
                self.delete_file(bucket_id, file_id)
            except Exception as delete_error:
                logger.error(
                    f"Failed to rollback file upload after metadata error: {delete_error}"
                )

            return OperationResult(
                success=False,
                error=f"Metadata handling failed: {e.message}",
                code="METADATA_ERROR",
            )

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

            # Determine filename for caching
            filename = file_metadata.get("name", file_id) if file_metadata else file_id
            cache_path = self.cache_manager._get_cache_path(
                bucket_id, file_id, filename
            )

            # Save to cache
            with open(cache_path, "wb") as f:
                f.write(file_bytes)

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
                success=False, error=f"Download failed: {e.message}", code=e.type
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

    def delete_file(self, bucket_id: str, file_id: str) -> OperationResult:
        try:
            result = self.storage.delete_file(bucket_id, file_id)

            # Also remove from cache
            self.cache_manager.remove_from_cache(bucket_id, file_id)

            return OperationResult(success=True, data=_as_dict(result), code="DELETED")

        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"Delete failed: {e.message}", code=e.type
            )
        except Exception as e:
            return OperationResult(
                success=False, error=f"Delete failed: {str(e)}", code="UNEXPECTED_ERROR"
            )

    def get_file(self, bucket_id: str, file_id: str) -> OperationResult:
        try:
            result = self.storage.get_file(bucket_id, file_id)
            return OperationResult(success=True, data=_as_dict(result))

        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"Get file failed: {e.message}", code=e.type
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
                success=False, error=f"List buckets failed: {e.message}", code=e.type
            )
        except Exception as e:
            return OperationResult(
                success=False, error=f"List buckets failed: {str(e)}", code="UNEXPECTED_ERROR"
            )

    def create_bucket(
        self,
        bucket_id: str,
        name: str = None,
        permissions: List[str] = None,
        file_security: bool = True,
        enabled: bool = True,
        maximum_file_size: int = None,
        allowed_file_extensions: List[str] = None,
        encryption: bool = False,
        compression: str = "none",
        antivirus: bool = True,
        create_metadata_db: bool = True,
    ) -> OperationResult:
        # Use default name from config if not provided
        if name is None:
            name = self.config.bucket_name

        _require_provisioning(f"bucket {bucket_id!r}")  # þing-01 #276: opt-in, default OFF
        try:
            if permissions is None:
                permissions = []

            if allowed_file_extensions is None:
                allowed_file_extensions = []

            result = _as_dict(self.storage.create_bucket(
                bucket_id=bucket_id,
                name=name,
                permissions=permissions,
                file_security=file_security,
                enabled=enabled,
                maximum_file_size=maximum_file_size,
                allowed_file_extensions=allowed_file_extensions,
                encryption=encryption,
                compression=compression,
                antivirus=antivirus,
            ))

            # Automatically create metadata database if requested
            if create_metadata_db:
                db_result = self.metadata_manager.create_database_if_not_exists(
                    name, self.config.database_id
                )
                # All-or-raise (þing-01 #276): a bucket created but its metadata DB not is the
                # stranded-shelf bug — do NOT return partial success; make the half state visible.
                if not db_result.success:
                    raise ProvisioningError(
                        f"stranded provisioning: bucket {bucket_id!r} was created but its metadata "
                        f"database was not ({db_result.error}) — half-success must raise, not return."
                    )
                result["metadata_database"] = db_result.data

            return OperationResult(success=True, data=result, code="CREATED")

        except AppwriteException as e:
            return OperationResult(success=False, error=e.message, code=e.type)

    # get_current_user() was removed with session auth (þing-01 #274): it required a live
    # session and no serving path used it. The API-key admin path below is retained.

    def get_user_preferences(self, user_id: Optional[str] = None) -> OperationResult:
        """Admin (API-key) read of a user's preferences by id. (Session variant retired, #274.)"""
        if not user_id:
            return OperationResult(
                success=False,
                error="user_id parameter required for API key authentication",
                code="MISSING_USER_ID",
            )
        try:
            user_prefs = self.users.get_prefs(user_id)
            return OperationResult(success=True, data=_as_dict(user_prefs), code="API_KEY")
        except AppwriteException as e:
            return OperationResult(success=False, error=e.message, code=e.type)

    def clear_cache(
        self, bucket_id: str = None, older_than_hours: int = None
    ) -> OperationResult:
        return self.cache_manager.clear_cache(bucket_id, older_than_hours)

    def get_cache_stats(self) -> Dict[str, Any]:
        return self.cache_manager.get_stats()

    def debug_collection_attributes(
        self, collection_id: str = None, database_id: str = None
    ) -> OperationResult:
        db_id = database_id or self.config.database_id
        coll_id = collection_id or self.config.collection_id

        if not db_id or not coll_id:
            return OperationResult(
                success=False,
                error="Database ID and collection ID must be provided in config or as parameters",
                code="MISSING_CONFIG",
            )
        try:
            attributes = self.databases.list_attributes(db_id, coll_id)
            logger.info("Existing attributes:")
            for attr in (_get(attributes, "attributes") or []):
                attr_dict = _as_dict(attr)
                logger.info(f"  - {attr_dict.get('key')} ({attr_dict.get('type')})")
            return OperationResult(success=True, data=_as_dict(attributes))

        except AppwriteException as e:
            logger.error(f"Error listing attributes: {e.message}")
            return OperationResult(success=False, error=e.message, code=e.type)
