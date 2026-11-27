"""PredictionStoreManager — the Appwrite-backed forecast artifact store.

Read only: finds the newest delivery and downloads its files."""
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union


from views_crafdapi.managers.appwrite import AppwriteConfig, AppWriteFileManager, OperationResult

from .constants import FORECAST_CATEGORY, LEGACY_ARTIFACT_TYPE, MANIFEST_ARTIFACT_TYPE
from .metadata import PredictionProvenance
from .quarantine import (
    APPROVED_FILE_IDS_ENV,
    QUARANTINED_FILE_IDS_ENV,
    _get_approved_file_ids,
    _get_quarantined_file_ids,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class MetadataLookupError(Exception):
    """A metadata search failed — distinct from a search that found nothing (C-233, #158).

    The pinned SDK raises the same `AppwriteException` for an HTTP error, a network error and a
    timeout. Returning `[]` for all of them made an outage, a revoked key and "no documents" look
    identical, so the serving path reported an outage as "no manifest". ``status`` is the HTTP
    status (401/403 not authorised; 0/None no response; 5xx server-side); ``appwrite_type`` is
    Appwrite's error type string.
    """

    def __init__(self, message: str, *, status: Optional[int] = None,
                 appwrite_type: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.appwrite_type = appwrite_type

    @property
    def is_unauthorized(self) -> bool:
        return self.status in (401, 403)


class PredictionStoreManager:
    def __init__(self, appwrite_file_manager_config: AppwriteConfig):
        self.model_path = appwrite_file_manager_config.path_manager
        self.__appwrite_file_manager_config = appwrite_file_manager_config
        self.__appwrite_file_manager = AppWriteFileManager(
            self.__appwrite_file_manager_config
        )

    def _require_consumer_name(self) -> str:
        """Return this consumer's document name, or raise.

        views-postprocessing#55 (held in #46): nothing else binds the *query* to the consumer-name
        constant. `test_seam_contract_binding.py` binds the constant to the registry row, but a
        falsy name used to be handled two different ways — `get_predictions_by_metadata` **omitted**
        the filter, silently widening the query across every consumer's documents and returning a
        non-empty result for the wrong consumer. It now refuses (ADR-003 fail-loud).

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
        # A copy: the name filter and the §11.4 pin below must not leak into the caller's dict
        # (C-359), where a second, typed query reusing it would inherit `type="model"`.
        filters = dict(filters or {})
        filters["name"] = self._require_consumer_name()

        # ADR-013 §11.4 (Hop-B transition guard): a category selection that names no
        # explicit type is legacy selection — pin it to the legacy artifact type so it
        # can never resolve a wire-contract artifact (manifest/shard/sidecar) as "the
        # dataset". Contract-aware queries pass their own type and are not touched.
        if "category" in filters and "type" not in filters:
            filters["type"] = LEGACY_ARTIFACT_TYPE

        logger.info(f"Searching for predictions with filters: {filters}")
        
        try:
            search_result = (
                self.__appwrite_file_manager.metadata_manager.search_files_by_metadata(
                    filters=filters if filters else None,
                    collection_name=self.__appwrite_file_manager_config.collection_name,
                    collection_id=self.__appwrite_file_manager_config.collection_id,
                    database_id=self.__appwrite_file_manager_config.database_id,
                ).to_dict()
            )
        except Exception as e:
            # The search catches only `AppwriteException`; an SDK edge case (a malformed error
            # body, a missing header) escapes as something else. It is still a failed lookup.
            logger.error(f"Search raised with filters {filters}: {e!r}")
            raise MetadataLookupError(f"metadata search raised {type(e).__name__}") from e
        if not search_result.get("success", False):
            error_msg = search_result.get("error", "Unknown error")
            if search_result.get("code") == "MISSING_CONFIG":
                # A deployment error, not an outage: say so, rather than send the operator
                # looking for an unreachable store.
                raise RuntimeError(f"metadata collection is not configured: {error_msg}")
            logger.error(f"Search failed with filters {filters}: {error_msg}")
            # An error, not an empty answer (C-233, #158): "the store could not be asked" and
            # "the store has nothing" must stay distinguishable to every caller.
            raise MetadataLookupError(
                error_msg, status=search_result.get("status"),
                appwrite_type=search_result.get("code"),
            )

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
        no manifested run exists — in which case the caller serves the last-good manifested run
        within the SLA, or a 503 (ADR-033 §6a); never a legacy artifact (S1/#264).

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
