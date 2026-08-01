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


def dataframe_to_dict(df):
    """Convert a pandas DataFrame to a JSON-serializable dictionary."""
    # Reset index to include index columns in the output
    df_reset = df.reset_index()
    df_reset = flatten_numeric_list_columns(df_reset)
    # Convert to records format
    records = df_reset.to_dict(orient="records")

    # Convert any numpy types to native Python types
    return convert_numpy_types(records)
