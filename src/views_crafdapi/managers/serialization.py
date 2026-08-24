"""HTTP request/response (de)serialization helpers for the CRAF'd API.

Pure, stateless functions that translate between HTTP query parameters / pandas
DataFrames and JSON-serializable Python. Extracted from `managers/api.py` so the
`CrafdApiManager` is not also the home of serialization logic (SRP; epic #144 / C-36).
These have no dependency on `CrafdApiManager` state — they take their inputs as
arguments and are covered by `tests/test_api_utilities.py`.
"""
import logging
from typing import List, Optional

import numpy as np
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def parse_list_param(param: Optional[str]) -> Optional[List[int]]:
    """Parse a comma-separated string into a list of integers."""
    if param is None:
        return None
    # Handle empty list representations
    stripped = param.strip()
    if stripped in ('[]', '', 'null', 'None'):
        return None  # Treat empty as "all" rather than "none"
    try:
        # Split by comma and convert each item to int
        return [int(item.strip()) for item in param.split(',') if item.strip()]
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid parameter format: {param}. Expected comma-separated integers."
        )


def parse_string_list_param(param: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated string into a list of strings."""
    if param is None:
        return None
    # Handle empty list representations
    stripped = param.strip()
    if stripped in ('[]', '', 'null', 'None'):
        return None  # Treat empty as "all" rather than "none"
    try:
        # Split by comma and strip whitespace
        result = [item.strip() for item in param.split(',') if item.strip()]
        return result if result else None
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid parameter format: {param}. Expected comma-separated strings."
        )


def convert_numpy_types(obj):
    """Convert numpy types to native Python types for JSON serialization.

    Also handles special float values (NaN, Inf) that are not JSON compliant
    by converting them to None.
    """
    if isinstance(obj, np.ndarray):
        return [convert_numpy_types(x) for x in obj.tolist()]
    elif isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        val = float(obj)
        # Handle non-JSON-compliant float values
        if np.isnan(val) or np.isinf(val):
            return None
        return val
    elif isinstance(obj, float):
        # Handle native Python floats that may be nan/inf
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj
    elif isinstance(obj, dict):
        return {key: convert_numpy_types(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_numpy_types(item) for item in obj]
    return obj


def flatten_numeric_list_columns(df):
    """
    Flattens single-element lists/arrays in numeric columns to scalar values.

    Only flattens arrays with exactly one element (e.g., [5.2] → 5.2).
    Preserves multi-element distributions (e.g., [1, 2, 3] stays as [1, 2, 3]).
    Ignores columns with strings, None values, or non-list-like data.

    Args:
        df (pd.DataFrame): The input DataFrame.

    Returns:
        pd.DataFrame: A new DataFrame with single-element lists flattened to scalars.
    """
    df = df.copy()

    for col in df.columns:
        try:
            # Skip if column is empty
            if df[col].empty:
                continue

            # Get the first non-null value to check type
            first_non_null = df[col].dropna().iloc[0] if not df[col].dropna().empty else None

            # Skip if no valid value found
            if first_non_null is None:
                continue

            # Skip if the value is a string (not a list/array)
            if isinstance(first_non_null, str):
                continue

            # Check if it's a list-like structure containing numeric values
            if isinstance(first_non_null, (np.ndarray, list, tuple)):
                # Skip empty lists
                if len(first_non_null) == 0:
                    continue

                # Only flatten if it's a single-element array
                if len(first_non_null) != 1:
                    continue

                # Check if the first element is numeric (not string)
                first_element = first_non_null[0]
                if isinstance(first_element, str):
                    continue

                # Verify it's a numeric type
                if isinstance(first_element, (int, float, np.integer, np.floating, np.number)):
                    # Flatten single-element arrays to scalars
                    df[col] = df[col].apply(
                        lambda x: x[0] if isinstance(x, (np.ndarray, list, tuple)) and len(x) == 1 else x
                    )

        except (IndexError, TypeError, KeyError) as e:
            # Log the error but continue processing other columns
            logger.debug(f"Skipping column '{col}' due to error: {e}")
            continue

    return df


# Measured cost of `dataframe_to_dict` output, per value, on real production artifacts.
#
# Two constants because a payload has two kinds of value and they differ threefold. A KEYED
# value is one column of one row -- it costs a dict entry, with the interning and hashing that
# implies. A SAMPLE ELEMENT is one posterior draw inside a list cell, which costs only the list
# slot and the float.
#
# Calibrated against two direct `tracemalloc` measurements, each linear across its range:
#
#   historical  100,000 rows x 14 keyed, 0 elements  -> 155.0 MiB  =>  1,625 B/row => ~116 B/keyed
#   forecast     13,110 rows x 17 keyed, 192 elements ->  96.7 MiB  =>  7,731 B/row => ~30 B/element
#
# Rounded UP, because this feeds a guard: over-estimating refuses slightly early, under-estimating
# lets through the payload the guard exists to stop. (C-284.)
_BYTES_PER_KEYED_VALUE = 120
_BYTES_PER_SAMPLE_ELEMENT = 32


def estimate_records_bytes(
    n_rows: int, n_keyed_columns: int, n_sample_elements_per_row: int
) -> int:
    """Estimate the peak memory `dataframe_to_dict` will need for a frame of this shape.

    Exists so a caller can decide **before** materialising. The whole point of the bound it
    feeds is not to pay the cost it is trying to refuse, so it takes a shape rather than a
    frame.

    A row bound cannot do this job: a historical row costs ~1,625 bytes and a forecast row
    ~7,731, because the second carries a posterior sample per draw per target. One number
    calibrated on the first is wrong by nearly 5x for the second, and the sample width varies
    per delivery -- a 32-draw run and a 1000-draw run differ by another factor of thirty.

    The mechanism is shared with the sibling API this box co-hosts, which runs the same
    serializer and was calibrated against it. What does NOT transfer is the call site: there the
    guard sits on one unfiltered route, and here every bounded route takes filters.
    """
    per_row = (
        n_keyed_columns * _BYTES_PER_KEYED_VALUE
        + n_sample_elements_per_row * _BYTES_PER_SAMPLE_ELEMENT
    )
    return n_rows * per_row


def dataframe_to_dict(df):
    """Convert a pandas DataFrame to a JSON-serializable dictionary."""
    # Reset index to include index columns in the output
    df_reset = df.reset_index()
    df_reset = flatten_numeric_list_columns(df_reset)
    # Convert to records format
    records = df_reset.to_dict(orient="records")

    # Convert any numpy types to native Python types
    return convert_numpy_types(records)
