"""Appwrite metadata-collection manager (search + the provisioning/DDL create_* surface).

Extracted from the appwrite god-module (epic #325 S9).
"""
from typing import Dict, Any, Tuple
from datetime import datetime
import time
import logging

from appwrite.services.databases import Databases
from appwrite.exception import AppwriteException
from appwrite.query import Query

from .constants import (
    DEFAULT_PAGE_LIMIT, MAX_ATTRIBUTE_CREATION_RETRIES, INITIAL_RETRY_DELAY,
)
from .sdk_compat import _as_dict, _get
from .results import OperationResult
from .provisioning import _require_provisioning
from .config import AppwriteConfig

logger = logging.getLogger(__name__)


class MetadataManager:
    def __init__(self, databases: Databases, config: AppwriteConfig):
        self.databases = databases
        self.config = config

    def create_database_if_not_exists(
        self, database_id: str = None, database_name: str = None
    ) -> OperationResult:
        # Use config values as defaults
        db_id = database_id or self.config.database_id
        db_name = database_name or self.config.database_name

        if not db_id or not db_name:
            return OperationResult(
                success=False,
                error="Database ID and name must be provided in config or as parameters",
                code="MISSING_CONFIG",
            )

        try:
            existing_databases = self.databases.list()

            for db in _get(existing_databases, "databases") or []:
                db_dict = _as_dict(db)
                if db_dict.get("name") == db_name or db_dict.get("$id") == db_id:
                    logger.info(f"Database '{db_name}' already exists")
                    return OperationResult(success=True, data=db_dict, code="EXISTS")

            # Only try to create if it doesn't exist
            _require_provisioning(f"database {db_id!r}")  # þing-01 #276: opt-in, default OFF
            try:
                result = self.databases.create(
                    database_id=db_id, name=db_name, enabled=True
                )

                logger.info(f"Created new database: {db_name}")
                return OperationResult(success=True, data=_as_dict(result))
            except AppwriteException as create_error:
                # If we hit the database limit but the database exists, that's okay
                if "maximum number of databases" in create_error.message.lower():
                    # Try to find the database again
                    existing_databases = self.databases.list()
                    for db in _get(existing_databases, "databases") or []:
                        db_dict = _as_dict(db)
                        if db_dict.get("$id") == db_id:
                            logger.warning(
                                f"Database limit reached, but database '{db_id}' exists"
                            )
                            return OperationResult(success=True, data=db_dict, code="EXISTS")
                raise create_error

        except AppwriteException as e:
            logger.error(f"Database operation failed: {e.message}")
            return OperationResult(
                success=False,
                error=f"Database operation failed: {e.message}",
                code=e.type,
            )

    def _infer_attribute_type(self, value: Any) -> Tuple[str, bool]:
        is_array = isinstance(value, list)
        base_value = value[0] if is_array and value else value

        if isinstance(base_value, bool):
            return "boolean", is_array
        elif isinstance(base_value, int):
            return "integer", is_array
        elif isinstance(base_value, float):
            return "double", is_array
        elif isinstance(base_value, datetime):
            return "datetime", is_array
        elif isinstance(base_value, str):
            try:
                datetime.fromisoformat(base_value.replace("Z", "+00:00"))
                return "datetime", is_array
            except (ValueError, AttributeError):
                return "string", is_array
        else:
            return "string", is_array

    def _create_dynamic_attributes(
        self,
        database_id: str,
        collection_id: str,
        metadata: Dict[str, Any],
        max_retries: int = MAX_ATTRIBUTE_CREATION_RETRIES,
        initial_delay: float = INITIAL_RETRY_DELAY,
    ) -> OperationResult:
        fixed_attributes = [
            {"key": "fileId", "type": "string", "size": 255, "required": True},
            {"key": "bucketId", "type": "string", "size": 255, "required": True},
            {"key": "filename", "type": "string", "size": 500, "required": True},
            {"key": "file_size", "type": "integer", "required": False},
            {"key": "mime_type", "type": "string", "size": 100, "required": False},
            {"key": "uploaded_at", "type": "datetime", "required": False},
            {"key": "file_hash", "type": "string", "size": 64, "required": False},
        ]

        delay = initial_delay
        existing_attribute_keys = set()

        for attempt in range(1, max_retries + 1):
            try:
                existing_attributes = self.databases.list_attributes(
                    database_id, collection_id
                )
                existing_attribute_keys = {
                    _as_dict(attr).get("key")
                    for attr in _get(existing_attributes, "attributes") or []
                }
                logger.debug(
                    f"Found {len(existing_attribute_keys)} existing attributes"
                )
                break
            except AppwriteException as e:
                if "collection_not_found" in e.message and attempt < max_retries:
                    logger.warning(
                        f"Collection not ready (attempt {attempt}/{max_retries}), retrying in {delay}s"
                    )
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.error(f"Failed to list attributes: {e.message}")
                    return OperationResult(
                        success=False,
                        error=f"Failed to list attributes: {e.message}",
                        code=e.type,
                    )

        for attr in fixed_attributes:
            if attr["key"] in existing_attribute_keys:
                continue

            try:
                self._create_single_attribute(database_id, collection_id, attr)
            except AppwriteException as e:
                if "attribute already exists" not in e.message.lower():
                    logger.warning(
                        f"Failed to create fixed attribute {attr['key']}: {e.message}"
                    )

        for key, value in metadata.items():
            if key in existing_attribute_keys:
                continue

            try:
                attr_type, is_array = self._infer_attribute_type(value)
                self._create_attribute_by_type(
                    database_id, collection_id, key, attr_type, is_array
                )
            except AppwriteException as e:
                logger.error(f"Failed to create attribute '{key}': {e.message}")

        return OperationResult(success=True)

    def _create_single_attribute(
        self, database_id: str, collection_id: str, attr: Dict[str, Any]
    ):
        # þing-01 #276: opt-in, default OFF. This writes through the same
        # `databases.create_*_attribute` SDK calls as the gated `_create_attribute_by_type`,
        # and is reached when `create_metadata_collection_if_not_exists` backfills the fixed
        # schema onto an *existing* collection — a path that previously bypassed the gate.
        _require_provisioning(f"attribute {attr['key']!r}")
        attr_creators = {
            "string": lambda: self.databases.create_string_attribute(
                database_id, collection_id, attr["key"], attr["size"], attr["required"]
            ),
            "integer": lambda: self.databases.create_integer_attribute(
                database_id, collection_id, attr["key"], attr["required"]
            ),
            "datetime": lambda: self.databases.create_datetime_attribute(
                database_id, collection_id, attr["key"], attr["required"]
            ),
        }

        if attr["type"] in attr_creators:
            attr_creators[attr["type"]]()
            logger.debug(f"Created {attr['type']} attribute: {attr['key']}")

    def _create_attribute_by_type(
        self,
        database_id: str,
        collection_id: str,
        key: str,
        attr_type: str,
        is_array: bool,
    ):
        _require_provisioning(f"attribute {key!r}")  # þing-01 #276: opt-in, default OFF
        common_args = [database_id, collection_id, key]

        try:
            if attr_type == "string":
                result = self.databases.create_string_attribute(
                    *common_args, size=255, required=False, array=is_array
                )
            elif attr_type == "integer":
                result = self.databases.create_integer_attribute(
                    *common_args, required=False, array=is_array
                )
            elif attr_type == "boolean" and not is_array:
                result = self.databases.create_boolean_attribute(
                    *common_args, required=False
                )
            else:
                result = self.databases.create_string_attribute(
                    *common_args, size=255, required=False, array=is_array
                )
                logger.warning(
                    f"Unsupported type {attr_type} for {key}, defaulted to string"
                )

            logger.debug(f"Created {attr_type} attribute '{key}' (array: {is_array})")
            return result
        except AppwriteException as e:
            # If the attribute already exists, that's fine - just return
            if "already exists" in e.message.lower():
                logger.debug(f"Attribute '{key}' already exists")
                return None
            # Otherwise, re-raise the exception
            raise e

    def create_metadata_collection_if_not_exists(
        self,
        metadata: Dict[str, Any] = None,
        collection_name: str = None,
        collection_id: str = None,
        database_id: str = None,
    ) -> OperationResult:
        # Use config values as defaults
        db_id = database_id or self.config.database_id
        coll_name = collection_name or self.config.collection_name
        coll_id = collection_id or self.config.collection_id

        if not db_id or not coll_name or not coll_id:
            return OperationResult(
                success=False,
                error="Database ID, collection name, and collection ID must be provided in config or as parameters",
                code="MISSING_CONFIG",
            )

        db_result = self.create_database_if_not_exists(db_id, self.config.database_name)
        if not db_result.success:
            return db_result

        try:
            existing_collections = self.databases.list_collections(db_id)

            for collection in _get(existing_collections, "collections") or []:
                coll_dict = _as_dict(collection)
                if coll_dict.get("$id") == coll_id or coll_dict.get("name") == coll_name:
                    if metadata:
                        self._create_dynamic_attributes(
                            db_id, coll_dict["$id"], metadata
                        )

                    return OperationResult(
                        success=True,
                        data={
                            "collection": coll_dict,
                            "database_id": db_id,
                            "collection_id": coll_dict["$id"],
                        },
                        code="EXISTS",
                    )

            _require_provisioning(f"collection {coll_id!r}")  # þing-01 #276: opt-in, default OFF
            result = self.databases.create_collection(
                database_id=db_id,
                collection_id=coll_id,
                name=coll_name,
                # views-crafdapi#91 / #123: this previously granted read/create/update/delete
                # to `Role.any()` — anyone holding the project id, which is not a secret. The
                # identical shape left two production collections (`unfao`, 111 rows;
                # `production_forecasts`, 461) anonymously readable AND writable until
                # 2026-08-14. An empty permission list is the correct scoping here: this
                # collection is only ever reached by a server-side API key, and an Appwrite API
                # key bypasses collection permissions, so no role grant is required to serve.
                # NOTE: Appwrite applies permissions at CREATION ONLY. This fixes what future
                # provisioning creates; it does not remediate any collection already created
                # with the open grant. See #91/#123 for that, which needs Appwrite access.
                permissions=[],
                document_security=False,
                enabled=True,
            )

            self._create_dynamic_attributes(db_id, coll_id, metadata or {})

            return OperationResult(
                success=True,
                data={
                    "collection": result,
                    "database_id": db_id,
                    "collection_id": _get(result, "$id") or _get(result, "id"),
                },
            )

        except AppwriteException as e:
            logger.error(f"Collection creation failed: {e.message}")
            return OperationResult(
                success=False,
                error=f"Collection creation failed: {e.message}",
                code=e.type,
            )

    def check_file_exists_by_hash(
    self,
    file_hash: str,
    collection_name: str = None,
    collection_id: str = None,
    database_id: str = None,
) -> OperationResult:
        # Use config values as defaults
        db_id = database_id or self.config.database_id
        coll_id = collection_id or self.config.collection_id

        if not db_id or not coll_id:
            return OperationResult(
                success=False,
                error="Database ID and collection ID must be provided in config or as parameters",
                code="MISSING_CONFIG",
            )
        try:
            # First ensure the collection exists
            collection_result = self.create_metadata_collection_if_not_exists(
                {}, collection_name, collection_id, database_id
            )

            if not collection_result.success:
                return collection_result

            # Now search for the file by hash
            search_result = self.databases.list_documents(
                db_id, coll_id, queries=[Query.equal("file_hash", file_hash)]
            )

            docs = _get(search_result, "documents") or []
            if (_get(search_result, "total") or 0) > 0:
                return OperationResult(
                    success=True,
                    data=_as_dict(docs[0]),
                    code="FOUND_BY_HASH"  # <-- CHANGED from "FOUND" to "FOUND_BY_HASH"
                )

            return OperationResult(success=False, code="NOT_FOUND")

        except AppwriteException as e:
            # If the file_hash attribute doesn't exist, create it and try again
            if "Attribute not found in schema: file_hash" in e.message:
                logger.info("file_hash attribute not found, creating it...")
                try:
                    self._create_attribute_by_type(
                        db_id, coll_id, "file_hash", "string", False
                    )

                    # Try the search again
                    try:
                        search_result = self.databases.list_documents(
                            db_id,
                            coll_id,
                            queries=[Query.equal("file_hash", file_hash)],
                        )

                        docs = _get(search_result, "documents") or []
                        if (_get(search_result, "total") or 0) > 0:
                            return OperationResult(
                                success=True,
                                data=_as_dict(docs[0]),
                                code="FOUND_BY_HASH"  # <-- CHANGED here too
                            )

                        return OperationResult(success=False, code="NOT_FOUND")
                    except AppwriteException as retry_e:
                        logger.error(
                            f"Search failed after creating attribute: {retry_e.message}"
                        )
                        return OperationResult(
                            success=False,
                            error=f"Search failed: {retry_e.message}",
                            code=retry_e.type,
                        )
                except AppwriteException as create_e:
                    logger.error(
                        f"Failed to create file_hash attribute: {create_e.message}"
                    )
                    return OperationResult(
                        success=False,
                        error=f"Attribute creation failed: {create_e.message}",
                        code=create_e.type,
                    )

            logger.error(f"Search failed: {e.message}")
            return OperationResult(
                success=False, error=f"Search failed: {e.message}", code=e.type
            )

    def search_files_by_metadata(
        self,
        filters: Dict[str, Any] = None,
        array_filters: Dict[str, Any] = None,
        collection_name: str = None,
        collection_id: str = None,
        database_id: str = None,
    ) -> OperationResult:
        # Use config values as defaults
        db_id = database_id or self.config.database_id
        coll_id = collection_id or self.config.collection_id

        if not db_id or not coll_id:
            return OperationResult(
                success=False,
                error="Database ID and collection ID must be provided in config or as parameters",
                code="MISSING_CONFIG",
            )

        try:
            queries = []

            if filters:
                for attribute, value in filters.items():
                    if value is not None:
                        queries.append(Query.equal(attribute, value))

            if array_filters:
                for attribute, value in array_filters.items():
                    if value is not None:
                        queries.append(Query.contains(attribute, value))

            # #287: Appwrite's list_documents caps a page at its default (25) unless a
            # Query.limit is supplied — and a single limit(100) is still short of a 108-shard
            # wire run. Page through with limit+offset so EVERY matching document is enumerated;
            # a silently-truncated result drops shards and refuses an otherwise-complete run
            # (25/108 → "83 missing" → ingest_failed). Sibling methods (list_files, list_buckets)
            # already paginate this way; this one was missed.
            documents = []
            offset = 0
            while True:
                page = self.databases.list_documents(
                    db_id,
                    coll_id,
                    queries=queries + [Query.limit(DEFAULT_PAGE_LIMIT), Query.offset(offset)],
                )
                batch = _get(page, "documents") or []
                documents.extend(batch)
                if len(batch) < DEFAULT_PAGE_LIMIT:
                    break
                offset += DEFAULT_PAGE_LIMIT
            total = _get(page, "total") or len(documents)

            return OperationResult(
                success=True,
                data={"documents": [_as_dict(d) for d in documents], "total": total},
            )

        except AppwriteException as e:
            logger.error(f"Search failed: {e.message}")
            return OperationResult(
                success=False, error=f"Search failed: {e.message}", code=e.type
            )

    def update_file_metadata(
        self,
        file_id: str,
        metadata_updates: Dict[str, Any],
        collection_name: str = None,
        collection_id: str = None,
        database_id: str = None,
    ) -> OperationResult:
        # Use config values as defaults
        db_id = database_id or self.config.database_id
        coll_id = collection_id or self.config.collection_id

        if not db_id or not coll_id:
            return OperationResult(
                success=False,
                error="Database ID and collection ID must be provided in config or as parameters",
                code="MISSING_CONFIG",
            )
        try:
            search_result = self.databases.list_documents(
                database_id=db_id,
                collection_id=coll_id,
                queries=[Query.equal("fileId", file_id)],
            )

            if not (_get(search_result, "documents") or []):
                return OperationResult(
                    success=False,
                    error=f"No metadata found for file ID: {file_id}",
                    code="METADATA_NOT_FOUND",
                )

            document_id = _get(_get(search_result, "documents")[0], "$id")

            result = self.databases.update_document(
                database_id=db_id,
                collection_id=coll_id,
                document_id=document_id,
                data=metadata_updates,
            )

            return OperationResult(success=True, data=_as_dict(result), code="UPDATED")

        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"Metadata update failed: {e.message}", code=e.type
            )

    def delete_metadata_document(
        self,
        document_id: str,
        collection_id: str = None,
        database_id: str = None,
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
            self.databases.delete_document(
                database_id=db_id,
                collection_id=coll_id,
                document_id=document_id,
            )
            return OperationResult(success=True, data={"document_id": document_id}, code="DELETED")
        except AppwriteException as e:
            return OperationResult(
                success=False, error=f"Metadata deletion failed: {e.message}", code=e.type
            )
