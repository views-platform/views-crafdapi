"""Decode the upstream forecast artifact into array-valued (object-dtype) columns.

Extracted from `_ViewsDataset._convert_to_arrays` (Phase 1 of #87). This is the seam where a
future native views-frames arrow frame-load path (#100) drops in alongside the current
object-dtype parquet read — isolating "how the artifact is decoded" as one reason to change.
"""

import numpy as np
import pandas as pd


def to_array_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return `df` with any list-valued columns converted to numpy arrays.

    C-263: the copy is taken only when there is something to convert. The unconditional
    `df.copy()` this replaces cost a full duplicate of the frame on every construction —
    ~0.8 GB on the 28.4M-row global historical, whose columns are all scalar, so the copy
    was discarded unchanged. The historical and wire artifacts both arrive scalar-valued;
    list columns come from the legacy object-cell forecast frames, which still copy.

    Returning the input unchanged is safe for the one caller (`_GridDataset._init_dataframe`):
    every path that follows rebinds `self.dataframe` to a fresh object — `sort_index()` when
    the index is unsorted, `fill_dense_grid` (which sorts) when preprocessing runs, and
    `.drop(columns=...)` when the stores are built — so no caller frame is mutated in place.
    """
    # `df.empty` FIRST: the generator dereferences `.iloc[0]`, so on a 0-row frame that
    # still has columns the `or df.empty` clause is never reached and this raises IndexError.
    if df.empty or not any(isinstance(df[col].iloc[0], list) for col in df.columns):
        return df
    converted = df.copy()
    for col in converted.columns:
        if isinstance(converted[col].iloc[0], list):
            converted[col] = converted[col].apply(np.array)
    return converted
