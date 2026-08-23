"""PredictionStoreManager — the Appwrite-backed forecast artifact store.

Fuses the read (serving) surface with the producer write/delete surface. The
producer methods (upload_predictions / update_prediction_metadata /
delete_prediction) are dead on the crafdapi serving path (locked by
tests/test_serving_isolation.py) and activate only in a write clone. The
reader/writer *class* severance is deferred to the writer extraction (S12)."""
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import pandas as pd

from views_crafdapi.managers.appwrite import AppwriteConfig, AppWriteFileManager, OperationResult

from .constants import FORECAST_CATEGORY, LEGACY_ARTIFACT_TYPE, MANIFEST_ARTIFACT_TYPE
from .metadata import PredictionFileMetadata, PredictionMetadata, PredictionProvenance
from .quarantine import (
    APPROVED_FILE_IDS_ENV,
    QUARANTINED_FILE_IDS_ENV,
    _get_approved_file_ids,
    _get_quarantined_file_ids,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class PredictionStoreManager:
    def __init__(self, appwrite_file_manager_config: AppwriteConfig):
        self.model_path = appwrite_file_manager_config.path_manager
        self.__appwrite_file_manager_config = appwrite_file_manager_config
        self.__appwrite_file_manager = AppWriteFileManager(
            self.__appwrite_file_manager_config
        )

    def upload_predictions(
        self,
        file: Union[Path, str, pd.DataFrame],
        filename: str,
        loa: str,
        name: str,
        type: str,
        targets: List[str],
        category: str,
        description: Optional[str] = None,
    ) -> OperationResult:

        metadata = PredictionMetadata(
            loa=loa, name=name, type=type, targets=targets, description=description, category=category
        ).to_dict()
        if isinstance(file, pd.DataFrame):
            raise NotImplementedError(
                "Uploading a DataFrame directly is not implemented."
            )
        elif isinstance(file, (Path, str)):
            file_path = str(file)
            upload_result = self.__appwrite_file_manager.upload_file_with_metadata(
                bucket_id=self.__appwrite_file_manager_config.bucket_id,
                filename=filename,
                file_path=file_path,
                metadata=metadata,
                collection_name=self.__appwrite_file_manager_config.collection_name,
                collection_id=self.__appwrite_file_manager_config.collection_id,
            ).to_dict()
        else:
            raise TypeError("file must be a Path, str, or pd.DataFrame")

        if upload_result.get("code") == "storage_bucket_not_found":
            # þing-01 #276 / PLATFORM-001 D5: a missing bucket is a misconfiguration, not a signal
            # to auto-create a phantom. create_bucket now RAISES unless provisioning is explicitly
            # enabled (CRAFDAPI_ALLOW_PROVISIONING=1) on a deliberate setup entrypoint — so by default
            # this fails visible naming the coordinate rather than uploading into a phantom bucket.
            logger.warning(
                f"Bucket '{self.__appwrite_file_manager_config.bucket_id}' not found — "
                f"auto-provisioning is opt-in (off by default)."
            )
            try:
                self.__appwrite_file_manager.create_bucket(
                    bucket_id=self.__appwrite_file_manager_config.bucket_id,
                    name=self.__appwrite_file_manager_config.bucket_name,
                )
            except Exception as e:
                logger.error(f"Refusing to provision bucket (fail-visible): {e}")
                return OperationResult(success=False, error=str(e))

            upload_result = self.__appwrite_file_manager.upload_file_with_metadata(
                bucket_id=self.__appwrite_file_manager_config.bucket_id,
                file_path=file_path,
                filename=filename,
                metadata=metadata,
                collection_name=self.__appwrite_file_manager_config.collection_name,
                collection_id=self.__appwrite_file_manager_config.collection_id,
            ).to_dict()
        
        return OperationResult(**upload_result)

    def _require_consumer_name(self) -> str:
        """Return this consumer's document name, or raise.

        views-postprocessing#55 (held in #46): nothing else binds the *query* to the consumer-name
        constant. `test_seam_contract_binding.py` binds the constant to the registry row, but a
        falsy name used to be handled two different ways — `get_predictions_by_metadata` **omitted**
        the filter, silently widening the query across every consumer's documents and returning a
        non-empty result for the wrong consumer; `list_all_predictions` passed the falsy value
        through as a literal filter. Both now refuse (ADR-003 fail-loud).

        The two callers share this guard deliberately: the defect was that they disagreed, so the
        check is the thing that must not be duplicated.
        """
        name = getattr(self.model_path, "model_name", None)
        if not name:
            raise ValueError(
                "model_name is unset on the path manager, so this query cannot be bound to a "
                "consumer. Refusing rather than querying without a name filter, which would "
                "widen the selection across every consumer's documents and return another "
                "consumer's data as if it were ours (views-postprocessing#55, ADR-003)."
            )
        return name

    def get_predictions_by_metadata(
        self, filters: Dict[str, Any] = None
    ) -> List[Dict[str, Any]]:
        """
        Get predictions by metadata filters.
        
        Args:
            filters: Dictionary of metadata fields to filter by. If None, returns all predictions.
                    If provided, will be merged with model name filter.
        
        Returns:
            List of prediction metadata documents, sorted by creation date (newest first)
        """
        # Start with model name filter if available
        if filters is None:
            filters = {}
        
        filters["name"] = self._require_consumer_name()

        # ADR-013 §11.4 (Hop-B transition guard): a category selection that names no
        # explicit type is legacy selection — pin it to the legacy artifact type so it
        # can never resolve a wire-contract artifact (manifest/shard/sidecar) as "the
        # dataset". Contract-aware queries pass their own type and are not touched.
        if "category" in filters and "type" not in filters:
            filters["type"] = LEGACY_ARTIFACT_TYPE

        logger.info(f"Searching for predictions with filters: {filters}")
        
        search_result = (
            self.__appwrite_file_manager.metadata_manager.search_files_by_metadata(
                filters=filters if filters else None,
                collection_name=self.__appwrite_file_manager_config.collection_name,  
                collection_id=self.__appwrite_file_manager_config.collection_id,      
                database_id=self.__appwrite_file_manager_config.database_id,          
            ).to_dict()
        )
        if not search_result.get("success", False):
            logger.warning(f"Search failed with filters: {filters}")
            error_msg = search_result.get("error", "Unknown error")
            logger.error(f"Search error: {error_msg}")
            return []
        
        documents = search_result.get("data", {}).get("documents", [])
        logger.info(f"Found {len(documents)} prediction files")

        # C-71: drop quarantined files before selection so a bad upload can be rolled
        # back to the previous known-good one without deleting it from Appwrite.
        quarantined = _get_quarantined_file_ids()
        if quarantined:
            kept = [d for d in documents if d.get("fileId") not in quarantined]
            excluded = [d.get("fileId") for d in documents if d.get("fileId") in quarantined]
            if excluded:
                logger.warning(
                    f"C-71 quarantine: excluding {len(excluded)} file(s) from selection: "
                    f"{excluded} (via {QUARANTINED_FILE_IDS_ENV})"
                )
            documents = kept

        # C-71 (proactive gate): if an approval allowlist is configured, restrict selection
        # to approved files only — a new upload is not served until it is explicitly approved.
        # Unset/empty allowlist leaves selection unrestricted (default).
        approved = _get_approved_file_ids()
        if approved:
            before = len(documents)
            documents = [d for d in documents if d.get("fileId") in approved]
            logger.info(
                f"C-71 approval allowlist active ({len(approved)} approved): "
                f"{before} candidate(s) -> {len(documents)} eligible (via {APPROVED_FILE_IDS_ENV})"
            )

        if len(documents) == 0:
            logger.warning(f"No files found matching filters: {filters}")
            logger.info("Try calling list_all_predictions_unfiltered() to see all files in the bucket")

        return sorted(
            documents,
            key=lambda x: x.get("$createdAt", ""),
            reverse=True,
        )

    def download_prediction(
        self,
        file_id: str,
        save_path: Union[Path, str] = None,
        use_cache: bool = True,
        validate_cache: bool = True,
    ) -> OperationResult:
        download_result = self.__appwrite_file_manager.download_file(
            bucket_id=self.__appwrite_file_manager_config.bucket_id,
            file_id=file_id,
            save_path=save_path,
            use_cache=use_cache,
            validate_cache=validate_cache,
        )
        return download_result

    def get_latest_file_id(self, filters: Dict[str, Any]) -> Optional[str]:
        files_list = self.get_predictions_by_metadata(filters=filters)
        if len(files_list) == 0:
            logger.warning(f"No files found matching the given filters: {filters}")
            return None
        
        latest_file = files_list[0]
        file_id = latest_file.get("fileId", None)
        
        if file_id:
            logger.info(f"Latest file ID: {file_id} (created: {latest_file.get('$createdAt')})")
        else:
            logger.warning(f"Latest file found but missing 'fileId' field: {latest_file}")
        
        return file_id

    def get_latest_file_metadata(self, filters: Dict[str, Any]) -> Optional[PredictionFileMetadata]:
        """Like get_latest_file_id() but includes creation/update timestamps."""
        files_list = self.get_predictions_by_metadata(filters=filters)
        if not files_list:
            return None

        latest = files_list[0]
        file_id = latest.get("fileId")
        if not file_id:
            return None

        return PredictionFileMetadata(
            file_id=file_id,
            created_at=latest.get("$createdAt", ""),
            updated_at=latest.get("$updatedAt", ""),
        )

    def get_latest_provenance(self, filters: Dict[str, Any]) -> Optional[PredictionProvenance]:
        """C-86: build a lineage record for the newest matching artifact — its identity
        (file ID, hash, timestamp, filename) plus the declared upstream `source`/`pipeline`
        (`"unknown"` if the producer does not stamp one). Returns `None` when no file
        matches or the latest has no `fileId`. Honours the C-71 quarantine blocklist
        (it flows through `get_predictions_by_metadata`)."""
        files_list = self.get_predictions_by_metadata(filters=filters)
        if not files_list:
            return None
        return self._provenance_from(files_list[0])

    def get_latest_manifest_provenance(self) -> Optional[PredictionProvenance]:
        """#60: the lineage record of the newest *manifested wire run*, or ``None``.

        `get_latest_provenance({"category": "forecast"})` cannot answer this. That query names
        no `type`, so the §11.4 transition guard pins it to the legacy type — correctly, since
        it exists to stop a legacy selection resolving a wire artifact. `crafd_bucket` holds no
        legacy documents, so the pinned query matches nothing and a caller that stops there
        concludes "no forecast" about a bucket that is serving one.

        This goes through `get_latest_manifest`, whose `type` is explicit, so the guard leaves
        it alone. Store-side and therefore worker-independent — unlike
        `DatasetService.served_forecast_provenance()`, which is `None` until *this* worker has
        served a forecast."""
        doc = self.get_latest_manifest()
        return self._provenance_from(doc) if doc else None

    @staticmethod
    def _provenance_from(doc: Dict[str, Any]) -> Optional[PredictionProvenance]:
        """Build the lineage record for one store document. The record shape is owned here and
        nowhere else, so a lineage query added later cannot drift from the one beside it."""
        file_id = doc.get("fileId")
        if not file_id:
            return None

        source = doc.get("source") or doc.get("pipeline") or "unknown"
        return PredictionProvenance(
            file_id=file_id,
            source=source,
            created_at=doc.get("$createdAt", ""),
            filename=doc.get("filename"),
            name=doc.get("name"),
            category=doc.get("category"),
            targets=doc.get("targets"),
            description=doc.get("description"),
            file_hash=doc.get("file_hash"),
        )

    def get_latest_manifest(self) -> Optional[Dict[str, Any]]:
        """ADR-013 §4.3 (S2/#204): resolve the newest Sampled-Forecast Wire Contract *run
        manifest* (``type="sampled_forecast_manifest"``, ``category="forecast"``). Returns
        the manifest store document (``fileId`` + ``filename`` + metadata) or ``None`` when
        no manifested run exists — in which case the caller falls back to the legacy path.

        The explicit ``type`` means the §11.4 transition guard leaves this query untouched
        (it only pins type-*less* category selections). Quarantine/approval and newest-wins
        selection flow through ``get_predictions_by_metadata`` unchanged, so an operator can
        still quarantine a bad run by its manifest file-id (C-71)."""
        docs = self.get_predictions_by_metadata(
            filters={"category": FORECAST_CATEGORY, "type": MANIFEST_ARTIFACT_TYPE}
        )
        return docs[0] if docs else None

    def resolve_artifact_file_ids(
        self, filenames: Iterable[str], artifact_type: str
    ) -> Dict[str, str]:
        """Map each requested wire-contract artifact *filename* to its Appwrite ``fileId``.

        The run manifest lists shards and the sidecar by ``{name (filename), sha256}`` — not
        by store file-id — so we resolve here via a type-scoped metadata query and match on
        each document's ``filename`` field. Newest-first; the first match per filename wins."""
        wanted = set(filenames)
        docs = self.get_predictions_by_metadata(
            filters={"category": FORECAST_CATEGORY, "type": artifact_type}
        )
        resolved: Dict[str, str] = {}
        for doc in docs:
            filename = doc.get("filename")
            if filename in wanted and filename not in resolved:
                file_id = doc.get("fileId")
                if file_id:
                    resolved[filename] = file_id
        return resolved

    def download_latest_file(
        self,
        filters: Optional[Dict[str, Any]] = None,
        save_path: Union[Path, str] = None,
        use_cache: bool = True,
        validate_cache: bool = True,
    ) -> OperationResult:
        if filters is None:
            filters = {}

        latest_file_id = self.get_latest_file_id(filters=filters)
        if latest_file_id is None:
            error_msg = f"No files found matching the given filters: {filters}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)
        
        logger.info(f"Downloading latest file: {latest_file_id}")
        return self.download_prediction(
            file_id=latest_file_id,
            save_path=save_path,
            use_cache=use_cache,
            validate_cache=validate_cache,
        )
    
    def get_file_metadata(self, file_id: str) -> OperationResult:
        """
        Get metadata for a specific file from the metadata collection.
        
        Args:
            file_id: The ID of the file to get metadata for
            
        Returns:
            OperationResult with the file metadata
        """
        try:
            search_result = self.__appwrite_file_manager.metadata_manager.search_files_by_metadata(
                filters={"fileId": file_id},
                collection_name=self.__appwrite_file_manager_config.collection_name,
                collection_id=self.__appwrite_file_manager_config.collection_id,
                database_id=self.__appwrite_file_manager_config.database_id,
            )
            
            if not search_result.success:
                return OperationResult(
                    success=False,
                    error=f"Failed to get metadata for file {file_id}: {search_result.error}",
                    code=search_result.code
                )
            
            documents = search_result.data.get("documents", [])
            if not documents:
                return OperationResult(
                    success=False,
                    error=f"No metadata found for file {file_id}",
                    code="NOT_FOUND"
                )
            
            return OperationResult(
                success=True,
                data=documents[0],
                code="FOUND"
            )
        
        except Exception as e:
            logger.error(f"Error getting file metadata: {e}")
            return OperationResult(
                success=False,
                error=str(e),
                code="UNKNOWN_ERROR"
            )
    
    def update_prediction_metadata(
        self,
        file_id: str,
        metadata_updates: Dict[str, Any]
    ) -> OperationResult:
        """
        Update metadata for a specific prediction file.
        
        Args:
            file_id: The ID of the file to update
            metadata_updates: Dictionary of metadata fields to update
            
        Returns:
            OperationResult with the update status
        """
        return self.__appwrite_file_manager.metadata_manager.update_file_metadata(
            file_id=file_id,
            metadata_updates=metadata_updates,
            collection_name=self.__appwrite_file_manager_config.collection_name,
            collection_id=self.__appwrite_file_manager_config.collection_id,
            database_id=self.__appwrite_file_manager_config.database_id,
        )
    
    def delete_prediction(self, file_id: str) -> OperationResult:
        """
        Delete a prediction file and its metadata.

        Args:
            file_id: The ID of the file to delete

        Returns:
            OperationResult with the deletion status
        """
        delete_result = self.__appwrite_file_manager.delete_file(
            bucket_id=self.__appwrite_file_manager_config.bucket_id,
            file_id=file_id
        )

        if not delete_result.success:
            logger.error(f"Failed to delete file {file_id}: {delete_result.error}")
            return delete_result

        search = self.__appwrite_file_manager.metadata_manager.search_files_by_metadata(
            filters={"fileId": file_id},
            collection_name=self.__appwrite_file_manager_config.collection_name,
            collection_id=self.__appwrite_file_manager_config.collection_id,
            database_id=self.__appwrite_file_manager_config.database_id,
        )
        if search.success:
            for doc in search.data.get("documents", []):
                doc_id = doc.get("$id")
                if doc_id:
                    result = self.__appwrite_file_manager.metadata_manager.delete_metadata_document(
                        document_id=doc_id,
                        collection_id=self.__appwrite_file_manager_config.collection_id,
                        database_id=self.__appwrite_file_manager_config.database_id,
                    )
                    if not result.success:
                        logger.warning(f"Failed to delete metadata doc {doc_id}: {result.error}")

        return delete_result
    
    def list_all_predictions(
        self,
    ) -> List[Dict[str, Any]]:
        """
        List all predictions for the current model.
        
        Returns:
            List of prediction metadata documents
        """
        return self.get_predictions_by_metadata(filters={"name": self._require_consumer_name()})
    
    # Debug
    def list_all_predictions_unfiltered(self) -> List[Dict[str, Any]]:
        """
        List all predictions in the bucket without any filters.
        Useful for debugging when filtered searches return no results.
        
        Returns:
            List of all prediction metadata documents
        """
        logger.info("Listing all predictions without filters")
        
        search_result = (
            self.__appwrite_file_manager.metadata_manager.search_files_by_metadata(
                filters=None,  # No filters
                collection_name=self.__appwrite_file_manager_config.collection_name,
                collection_id=self.__appwrite_file_manager_config.collection_id,
                database_id=self.__appwrite_file_manager_config.database_id,
            ).to_dict()
        )

        if not search_result.get("success", False):
            logger.error(f"Search error: {search_result.get('error', 'Unknown error')}")
            return []
        
        documents = search_result.get("data", {}).get("documents", [])
        logger.info(f"Found {len(documents)} total prediction files")
        
        return sorted(
            documents,
            key=lambda x: x.get("$createdAt", ""),
            reverse=True,
        )