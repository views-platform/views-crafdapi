"""Lay estimator output (numpy) onto the served `{var}_*` column contract.

Extracted from `_ViewsDataset._create_hdi_dataframe` / `_create_map_dataframe` (Phase 4a of
#87 / #112). Produces `{var}_hdi_lower` / `{var}_hdi_upper` (HDI) and `{var}_map` (point /
extrema — the caller renames `_map` to `_min` / `_max` for the sample-extrema columns). Pure
functions: the caller supplies the `(time, entity)` axis labels and the index level names.
"""

from typing import Dict, List, Optional, Tuple, Union

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


def map_dataframe(
    var_name: str,
    values,
    time_id: str,
    entity_id: str,
    default_time: pd.Index,
    default_entity: pd.Index,
    time_ids: Optional[Union[int, List[int]]] = None,
    entity_ids: Optional[Union[int, List[int]]] = None,
) -> pd.DataFrame:
    """Format a `(time, entity)` value matrix into a single `{var_name}_map` column."""
    time_steps, entities = _resolve_axes(time_ids, entity_ids, default_time, default_entity)
    result = (
        pd.DataFrame(values, index=time_steps, columns=entities)
        .stack()
        .to_frame(f"{var_name}_map")
    )
    result.index.names = [time_id, entity_id]
    return result


def hdi_dataframe(
    var_name: str,
    lower,
    upper,
    time_id: str,
    entity_id: str,
    default_time: pd.Index,
    default_entity: pd.Index,
    time_ids: Optional[Union[int, List[int]]] = None,
    entity_ids: Optional[Union[int, List[int]]] = None,
) -> pd.DataFrame:
    """Format flat HDI bound arrays into `{var_name}_hdi_lower` / `{var_name}_hdi_upper`."""
    time_steps, entities = _resolve_axes(time_ids, entity_ids, default_time, default_entity)
    index = pd.MultiIndex.from_product(
        [time_steps, entities], names=[time_id, entity_id]
    )
    result = pd.DataFrame(
        {
            f"{var_name}_hdi_lower": lower.flatten(),
            f"{var_name}_hdi_upper": upper.flatten(),
        },
        index=index,
    )
    result.index.names = [time_id, entity_id]
    return result


# --- ADR-025 per-series value columns (epic #222 / S4) ---------------------------------------
# The JSON serving path emits, per consumer series (sb/ns/os): `s_map`, the three nested HDIs
# `s_hdi{50,90,95}_{lower,upper}`, and `s_severe_scenario` — built from `schema` so the names
# live in exactly one place. `s_actual` (the historical join) is NOT emitted here; it is the
# bulk parquet's column (S6). All columns are raw counts (ADR-024).


def series_value_column_names(series: str) -> List[str]:
    """The value columns the JSON serializer emits for one series (ADR-025 + ADR-034 exceedance)."""
    names = [schema.map_col(series)]
    for mass in schema.MASSES:
        names += [schema.hdi_col(series, mass, "lower"), schema.hdi_col(series, mass, "upper")]
    names += [schema.severe_col(series), schema.bimodality_col(series)]
    names += [schema.exceed_col(series, c) for c in schema.EXCEEDANCE_THRESHOLDS]
    return names


def series_value_data(
    series: str,
    map_: np.ndarray,
    severe: np.ndarray,
    bimodality: np.ndarray,
    hdi: Dict[float, Tuple[np.ndarray, np.ndarray]],
    exceedance: Optional[Dict[int, np.ndarray]] = None,
) -> Dict[str, np.ndarray]:
    """Ordered ``{column_name: array}`` for one series from its reduced stats.

    ``map_``/``severe``/``bimodality`` are per-row arrays; ``hdi`` maps each mass →
    ``(lower, upper)``; ``exceedance`` maps each threshold → ``P(Y>c)`` per row (ADR-034).
    Column names come from `schema`. Order matches `series_value_column_names`. When
    ``exceedance`` is ``None`` (or missing a threshold), that column is emitted as NaN so the
    served column set is always complete — the serving paths pass real values.
    """
    data: Dict[str, np.ndarray] = {schema.map_col(series): np.asarray(map_)}
    for mass in schema.MASSES:
        lower, upper = hdi[mass]
        data[schema.hdi_col(series, mass, "lower")] = np.asarray(lower)
        data[schema.hdi_col(series, mass, "upper")] = np.asarray(upper)
    data[schema.severe_col(series)] = np.asarray(severe)
    data[schema.bimodality_col(series)] = np.asarray(bimodality)
    exceedance = exceedance or {}
    nan_like = np.full(np.asarray(map_).shape, np.nan, dtype=np.float64)
    for c in schema.EXCEEDANCE_THRESHOLDS:
        col = exceedance[c] if c in exceedance else nan_like
        data[schema.exceed_col(series, c)] = np.asarray(col)
    return data


def series_value_dataframe(
    series: str,
    map_: np.ndarray,
    severe: np.ndarray,
    bimodality: np.ndarray,
    hdi: Dict[float, Tuple[np.ndarray, np.ndarray]],
    time_id: str,
    entity_id: str,
    default_time: pd.Index,
    default_entity: pd.Index,
    time_ids: Optional[Union[int, List[int]]] = None,
    entity_ids: Optional[Union[int, List[int]]] = None,
    exceedance: Optional[Dict[int, np.ndarray]] = None,
) -> pd.DataFrame:
    """Lay per-series value stats (each a ``(time, entity)`` matrix) onto a flat MultiIndex.

    Mirrors `hdi_dataframe`'s axis handling: matrices are C-order flattened against
    ``from_product([time, entity])`` (time-major), so the flatten aligns with the index.
    ``exceedance`` maps each threshold → a ``(time, entity)`` ``P(Y>c)`` matrix (ADR-034).
    """
    time_steps, entities = _resolve_axes(time_ids, entity_ids, default_time, default_entity)
    index = pd.MultiIndex.from_product([time_steps, entities], names=[time_id, entity_id])
    data = {
        name: np.asarray(col).flatten()
        for name, col in series_value_data(
            series, map_, severe, bimodality, hdi, exceedance
        ).items()
    }
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
