"""Appwrite metadata-table manager: the search this service reads deliveries with.

Extracted from the appwrite god-module (epic #325 S9).
"""
from typing import Dict, Any
import logging

from appwrite.services.databases import Databases
from appwrite.services.tables_db import TablesDB
from appwrite.exception import AppwriteException
from appwrite.query import Query

from .constants import (
    DEFAULT_PAGE_LIMIT,
)
from .sdk_compat import _as_dict, _get
from .results import OperationResult
from .config import AppwriteConfig

logger = logging.getLogger(__name__)


class MetadataManager:
    def __init__(self, databases: Databases, config: AppwriteConfig, tables: TablesDB):
        self.databases = databases
        # Reads go through TablesDB, Appwrite 1.8's name for the Databases API (#200): its
        # `list_rows` accepts a key with `rows.read`, the only scope the console now offers.
        self.tables = tables
        self.config = config

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

            # #287: Appwrite's list_rows caps a page at its default (25) unless a
            # Query.limit is supplied — and a single limit(100) is still short of a 108-shard
            # wire run. Page through with limit+offset so EVERY matching document is enumerated;
            # a silently-truncated result drops shards and refuses an otherwise-complete run
            # (25/108 → "83 missing" → ingest_failed). Sibling methods (list_files, list_buckets)
            # already paginate this way; this one was missed.
            documents = []
            offset = 0
            while True:
                page = self.tables.list_rows(
                    db_id,
                    coll_id,
                    queries=queries + [Query.limit(DEFAULT_PAGE_LIMIT), Query.offset(offset)],
                )
                batch = _get(page, "rows") or []
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
                success=False, error=f"Search failed: {e.message}", code=e.type, status=e.code
            )
