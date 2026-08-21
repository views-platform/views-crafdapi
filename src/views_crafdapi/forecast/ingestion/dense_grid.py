"""Dense-grid fill for the forecast grid.

RETAINED in crafdapi (views-frames has no fill primitive — sparse-index by design). Extracted
from `_ViewsDataset._preprocess_dataframe` (Phase 1 of #87). The dense grid is defined by the
entities present in the last time step; recreated cells are filled with `fill_value`
(default 0, ADR-021), and array (sample) columns get a sample-length array, not a scalar
(C-87). An entity present in the input but absent from the last time step fails loud rather
than being silently dropped (C-87).
"""

import numpy as np
import pandas as pd


def fill_dense_grid(
    df: pd.DataFrame,
    time_values: pd.Index,
    time_id: str,
    entity_id: str,
    fill_value: float,
) -> pd.DataFrame:
    """Return `df` with every `(time, entity)` combination of the last step's entities
    present, missing cells recreated and filled. See module docstring for the C-87/ADR-021
    semantics."""
    last_step_id = time_values.max()
    existing_entity_ids = df.loc[last_step_id].index.unique()

    # C-87: fail loud rather than silently drop an entity absent from the last step.
    all_entity_ids = df.index.get_level_values(entity_id).unique()
    dropped_entities = all_entity_ids.difference(existing_entity_ids)
    if len(dropped_entities) > 0:
        sample = list(dropped_entities[:10])
        raise ValueError(
            f"{len(dropped_entities)} entity/entities present in the input are absent from "
            f"the last time step ({last_step_id}) and would be dropped from the dense grid "
            f"(e.g. {sample}). Refusing to silently discard data (C-87)."
        )

    df = df[df.index.get_level_values(entity_id).isin(existing_entity_ids)]

    # Already-dense fast path (C-263). The producer's historical artifact IS the dense grid:
    # 439 months x 64,742 cells == 28,421,738 rows, exactly, with no gaps. On that input the
    # work below is performed in full and finds nothing — `MultiIndex.from_product` materialises
    # all 28.4M pairs and `.difference()` hashes them against the frame's own index, purely to
    # reach the `len(missing_combinations) == 0` early return a few lines down.
    #
    # The precondition is decidable without building either object. After the filter above,
    # every remaining row's (time, entity) pair is drawn from `time_values x existing_entity_ids`,
    # so the frame's index is a SUBSET of the product. A subset whose cardinality equals the
    # product's, and which contains no duplicate pairs, IS the product — nothing is missing.
    # Both facts are cheap: a multiply and an index uniqueness check.
    #
    # `is_unique` is load-bearing, not defensive: without it a frame with one duplicated pair
    # and one absent pair has the right length and would skip a fill it genuinely needs.
    if len(df) == len(time_values) * len(existing_entity_ids) and df.index.is_unique:
        return df.sort_index()

    all_combinations = pd.MultiIndex.from_product(
        [time_values, existing_entity_ids], names=[time_id, entity_id]
    )
    missing_combinations = all_combinations.difference(df.index)
    if len(missing_combinations) == 0:
        return df.sort_index()

    # C-87: array (sample) columns get a sample-length array of the fill value, not a scalar.
    n_missing = len(missing_combinations)
    fill_data = {}
    for col in df.columns:
        representative = df[col].iloc[0]
        if isinstance(representative, np.ndarray):
            template = np.full(
                representative.shape, fill_value, dtype=representative.dtype
            )
            fill_data[col] = [template.copy() for _ in range(n_missing)]
        elif isinstance(representative, list):
            fill_data[col] = [
                [fill_value] * len(representative) for _ in range(n_missing)
            ]
        else:
            fill_data[col] = fill_value
    missing_df = pd.DataFrame(fill_data, index=missing_combinations, columns=df.columns)
    return pd.concat([df, missing_df]).sort_index()
