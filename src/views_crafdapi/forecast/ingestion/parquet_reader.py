"""Decode the upstream forecast artifact into array-valued (object-dtype) columns.

Extracted from `_ViewsDataset._convert_to_arrays` (Phase 1 of #87). This is the seam where a
future native views-frames arrow frame-load path (#100) drops in alongside the current
object-dtype parquet read — isolating "how the artifact is decoded" as one reason to change.
"""

import numpy as np
import pandas as pd


def to_array_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of `df` with list-valued columns converted to numpy arrays."""
    converted = df.copy()
    for col in converted.columns:
        if isinstance(converted[col].iloc[0], list):
            converted[col] = converted[col].apply(np.array)
    return converted
