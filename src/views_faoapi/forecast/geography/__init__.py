"""Geography: the GAUL/admin metadata that views-frames deliberately does not carry.

RETAINED in faoapi (views-frames is scalar-metadata-only, ADR-013; geography is injected,
not embedded, ADR-014). One reason to change — the admin-level scheme (GAUL columns / levels).

- `metadata_table` — the level vocabulary + cell→level resolution
- `level_mapping`   — build the injected `(time, priogrid) → target_unit` mapping for the leaf
"""
