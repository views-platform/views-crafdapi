"""Lay the summaries (numpy) onto the served `{var}_*` columns, and rename them for serving.

`series_value_*` build the summary columns per target (`schema.SUMMARY_QUANTITIES`, #204);
`to_consumer_columns` renames them to the outcome code. Pure
functions: the caller supplies the `(time, entity)` axis labels and the index level names.
"""

from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd

from views_crafdapi.forecast.serialize import schema


def _resolve_axes(time_ids, entity_ids, default_time, default_entity):
    """Use the subsetted time/entity ids when given, else the dataset's full axes."""
    if time_ids is not None:
        if not isinstance(time_ids, list):
            time_ids = [time_ids]
        time_steps = pd.Index(time_ids)
    else:
        time_steps = default_time

    if entity_ids is not None:
        if not isinstance(entity_ids, list):
            entity_ids = [entity_ids]
        entities = pd.Index(entity_ids)
    else:
        entities = default_entity
    return time_steps, entities


# --- The summary columns per target (#204) ----------------------------------------------------
# Named by the internal target here (`{var}_mean`, …); `to_consumer_columns` renames them to the
# outcome code where the answer is served. Names come from `schema`.


def series_value_column_names(var: str) -> List[str]:
    """The summary columns for one target, in served order."""
    return [f"{var}_{q}" for q in schema.SUMMARY_QUANTITIES]


def series_value_data(var: str, summary: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
    """``{column: array}`` for one target from ``summarize_draws``' output, in served order."""
    return {f"{var}_{q}": np.asarray(summary[q]) for q in schema.SUMMARY_QUANTITIES}


def series_value_dataframe(
    var: str,
    summary: Dict[str, np.ndarray],
    time_id: str,
    entity_id: str,
    default_time: pd.Index,
    default_entity: pd.Index,
    time_ids: Optional[Union[int, List[int]]] = None,
    entity_ids: Optional[Union[int, List[int]]] = None,
) -> pd.DataFrame:
    """Lay one target's summaries (each a ``(time, entity)`` matrix) onto a flat MultiIndex,
    time-major, matching ``from_product([time, entity])``."""
    time_steps, entities = _resolve_axes(time_ids, entity_ids, default_time, default_entity)
    index = pd.MultiIndex.from_product([time_steps, entities], names=[time_id, entity_id])
    data = {name: np.asarray(col).flatten() for name, col in series_value_data(var, summary).items()}
    return pd.DataFrame(data, index=index)


def to_consumer_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Structured var→series rename of the served forecast columns (epic #222 / S4).

    The single boundary transform: applied once, on the forecast response, it swaps each
    internal-var quantity column `{var}_{quantity}` for its consumer `{series}_{quantity}`
    (via `schema.consumer_column`). Identity, geo, and historical `actual` columns carry no
    quantity suffix and pass through untouched — so the fail-loud series gate only sees columns
    that legitimately have a series. Not applied inside the reduction (which runs on arbitrary
    vars, incl. synthetic fixtures); applied where real forecast vars are guaranteed to conform.
    """
    return df.rename(columns={col: schema.consumer_column(col) for col in df.columns})
