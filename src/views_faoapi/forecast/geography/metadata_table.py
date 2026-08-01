"""The admin-level vocabulary and cell→level resolution for faoapi's geo-metadata table.

Single source of truth for which metadata column identifies each served level and which
extra columns each level carries into output. Extracted from `FAO_PGMDataset` (Phase 3 of
#87 / #90).
"""

import pandas as pd

# Served level -> the geo-metadata column that identifies a unit at that level.
LEVELS = {
    "country": "country_iso_a3",
    "gaul0": "admin1_gaul0_code",
    "gaul1": "admin1_gaul1_code",
    "gaul2": "admin2_gaul2_code",
}

# Served level -> the extra metadata columns to carry through aggregation to output
# (the identifying column rides as the group key; "country" uses its ISO3 as the unit id).
LEVEL_METADATA_COLUMNS = {
    "country": [],
    "gaul0": ["country_iso_a3", "admin1_gaul0_name"],
    "gaul1": ["country_iso_a3", "admin1_gaul1_name", "admin1_gaul0_name"],
    "gaul2": ["country_iso_a3", "admin2_gaul2_name", "admin1_gaul1_name", "admin1_gaul0_name"],
}


def resolve_level_cells(geo_metadata: pd.DataFrame, level_col: str, code, entity_id: str) -> list:
    """Return the unique entity (PRIO-GRID cell) IDs whose `level_col` equals `code`."""
    return (
        geo_metadata[geo_metadata[level_col] == code]
        .index.get_level_values(entity_id)
        .unique()
        .to_list()
    )
