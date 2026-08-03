"""Wire-contract artifact types and the prediction/system metadata field schemas
(ADR-013). Pure literals; the single source of truth cross-checked by
tests/test_metadata_contract.py."""

# ADR-013 §11.4 (Hop-B transition guard): the `type` stamped on every legacy
# (pre-wire-contract) document — an FAO-era convention ground-truthed 2026-07-15 against live
# Appwrite (all typed docs carried type="model"). Inherited verbatim by crafd, which is
# greenfield on the wire contract (crafd_bucket has no pre-wire-contract docs), so the guard is
# inert here. Category selections that name no
# type of their own are pinned to it, so contract artifacts
# (type="sampled_forecast_shard|manifest|sidecar", uploaded under this consumer's
# own `name` and therefore visible to the name filter) can never be grabbed as
# "the forecast" by legacy selection. Golden-string-tested; changing this value is
# a wire-contract amendment, not a refactor.
LEGACY_ARTIFACT_TYPE = "model"

# ADR-013 Sampled-Forecast Wire Contract artifact types (§4.1a). These are the explicit
# `type` values a contract-aware query passes, so the §11.4 transition guard above leaves
# them untouched (it only pins type-*less* category selections). Selection is manifest-first:
# resolve the run manifest, then fetch the shards + sidecar it lists (§4.3).
FORECAST_CATEGORY = "forecast"
MANIFEST_ARTIFACT_TYPE = "sampled_forecast_manifest"
SHARD_ARTIFACT_TYPE = "sampled_forecast_shard"
SIDECAR_ARTIFACT_TYPE = "sampled_forecast_sidecar"

PREDICTION_METADATA_FIELDS = {
    "loa":         {"type": str, "required": True},
    "name":        {"type": str, "required": True},
    "type":        {"type": str, "required": True},
    "targets":     {"type": list, "required": True},
    "category":    {"type": str, "required": True, "allowed": ["forecast", "historical"]},
    "description": {"type": str, "required": False},
}

SYSTEM_METADATA_FIELDS = {
    "fileId":      {"type": "string", "size": 255, "required": True},
    "bucketId":    {"type": "string", "size": 255, "required": True},
    "filename":    {"type": "string", "size": 500, "required": True},
    "file_size":   {"type": "integer", "required": False},
    "mime_type":   {"type": "string", "size": 100, "required": False},
    "uploaded_at": {"type": "datetime", "required": False},
    "file_hash":   {"type": "string", "size": 64, "required": False},
}
