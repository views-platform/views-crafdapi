"""The on-disk VALUE format for the disk cache (S5, #154).

Extracted from the handlers god-class (S11, #336) so the cache-version derivation
(`managers/disk_cache.py`) and the wire reader depend on this **stable leaf**, not on
the volatile `ForecastDataset` module (the SDP inversion / C-138 decoupling). Bump
`_VALUE_SCHEMA_VERSION` when the persisted layout changes."""

_VALUE_SCHEMA_VERSION = "value-store-v1"
_VALUE_MANIFEST_SCALARS = (
    "is_prediction", "preprocess_input", "time_id", "entity_id", "targets", "pred_vars",
    "features", "sample_size", "broadcast_features", "fill_value", "original_columns",
)


