"""Prediction store package — the Appwrite-backed forecast artifact store.

Split from a single ``prediction.py`` (epic #325, story S10) into one concept
per module, mirroring the ``appwrite/`` package (S9):

* :mod:`.constants`  — wire-contract artifact types + metadata field schemas
* :mod:`.quarantine` — the C-71 operator rollback gate (blocklist/allowlist)
* :mod:`.metadata`   — the value objects (PredictionMetadata / -FileMetadata / -Provenance)
* :mod:`.manager`    — ``PredictionStoreManager`` (fused reader + dead-producer writer)

The public surface is re-exported here so ``from views_crafdapi.managers.prediction
import X`` keeps working unchanged.
"""
from .constants import (
    FORECAST_CATEGORY,
    LEGACY_ARTIFACT_TYPE,
    MANIFEST_ARTIFACT_TYPE,
    PREDICTION_METADATA_FIELDS,
    SHARD_ARTIFACT_TYPE,
    SIDECAR_ARTIFACT_TYPE,
    SYSTEM_METADATA_FIELDS,
)
from .manager import PredictionStoreManager
from .metadata import (
    PredictionFileMetadata,
    PredictionMetadata,
    PredictionProvenance,
)
from .quarantine import (
    APPROVED_FILE_IDS_ENV,
    QUARANTINED_FILE_IDS_ENV,
    _get_approved_file_ids,
    _get_quarantined_file_ids,
)

__all__ = [
    "PredictionStoreManager",
    "PredictionMetadata",
    "PredictionFileMetadata",
    "PredictionProvenance",
    "PREDICTION_METADATA_FIELDS",
    "SYSTEM_METADATA_FIELDS",
    "LEGACY_ARTIFACT_TYPE",
    "FORECAST_CATEGORY",
    "MANIFEST_ARTIFACT_TYPE",
    "SHARD_ARTIFACT_TYPE",
    "SIDECAR_ARTIFACT_TYPE",
    "QUARANTINED_FILE_IDS_ENV",
    "APPROVED_FILE_IDS_ENV",
    "_get_quarantined_file_ids",
    "_get_approved_file_ids",
]
