"""C-69 regression: geo_metadata must expose each cell's geography in the correct NAMED
columns — never a positional transposition (e.g. a country ISO3 written into a GAUL-name
column) into UN-facing output. The backfill assignment writes by explicit destination
columns (handlers.py), so a reorder of `_METADATA_COLS` can never silently transpose labels."""

import pytest

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import FAO_PGMDataset

pytestmark = pytest.mark.layer2_data


def test_geo_metadata_labels_match_source_by_column():
    """Every cell's geo_metadata matches the source metadata for that entity, column by
    column (by name) — guards against positional transposition in the metadata pipeline."""
    df = make_fao_df(n_cells=4, n_months=2, n_samples=5, seed=7)
    ds = FAO_PGMDataset(df)

    assert list(ds.geo_metadata.columns) == FAO_PGMDataset._METADATA_COLS
    for idx in ds.geo_metadata.index:
        ref, got = df.loc[idx], ds.geo_metadata.loc[idx]
        for col in FAO_PGMDataset._METADATA_COLS:
            assert got[col] == ref[col], (
                f"geo_metadata[{col!r}] at {idx} = {got[col]!r} but source has {ref[col]!r} "
                f"— positional transposition (register C-69)"
            )
