import copy
import logging
import pandas as pd
import numpy as np
from typing import List, Union, Tuple, Optional
from views_crafdapi.forecast.frames.builder import build_prediction_frame, build_target_frame
from views_crafdapi.forecast.ingestion.dense_grid import fill_dense_grid
from views_crafdapi.forecast.ingestion.parquet_reader import to_array_columns
from views_crafdapi.forecast.ingestion.plausibility import (
    assert_prediction_samples_plausible,
)
from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.serialize.json_contract import (
    series_value_dataframe,
)
from views_crafdapi.forecast.summarize.draws import summarize_draws

from pathlib import Path

logger = logging.getLogger(__name__)



# The one refusal for summarising a dataset without draws, shared by `ForecastDataset`.
NEEDS_DRAWS = "summaries are computed from draws; this dataset holds recorded values"

class _GridDataset:
    """Generic `(time, entity)`-grid sample dataset — the frame-native compute core.

    Carries the canonical `(N, S)` sample store (`_sample_store`) over a sorted 2-level
    MultiIndex and provides the representation-agnostic machinery: tensor build, subset,
    the summaries and frame export. It is **not** FAO-specific
    — it requires no geography — which is exactly why it is retained as a separate base
    rather than merged into `ForecastDataset`: it is the geo-less unit under test (the
    parity/validation suites instantiate it directly).

    `ForecastDataset` composes the `forecast/*` modules ON TOP of this base, adding the FAO
    geo-metadata table, aggregation, and the served `summarize`. The object-dtype
    spine that motivated epic #154 is gone (S4); this two-level structure (generic base +
    FAO facade) is the rationalized end-state — full single-class merge was rejected as
    complecting two responsibilities and relocating a fail-loud (register D-21). The
    historical/scalar feature path still keeps pandas cells here (ADR-030 §1, out of scope).
    """

    _BASE_YEAR = 1980

    def __init__(
        self,
        source: Union[pd.DataFrame, str, Path],
        targets: Optional[List[str]] = None,
        broadcast_features=False,
        fill_value: float = 0,
    ):
        """
        Initialize the ViewsDataset with a source.

        Parameters:
        source (Union[pd.DataFrame, str, Path]): The source can be a pandas DataFrame,
                                                 a string representing a file path,
                                                 or a Path object.
        targets (Optional[List[str]]): List of target variable names.
        broadcast_features (bool): If True, broadcast scalar features to match sample size.
                                   If False, treat features as scalars stored in size-1 arrays
                                   and disable tensor operations.
        fill_value (float): Value used to fill missing grid cells in _preprocess_dataframe.
                            Default 0, matching views-datafactory dense grid convention (ADR-021).

        Raises:
        ValueError: If the source is not a pandas DataFrame, string, or Path object.
        """
        self._preprocess_input_dataframe = True
        self.broadcast_features = broadcast_features
        self._fill_value = fill_value
        if isinstance(source, pd.DataFrame):
            self._init_dataframe(source, targets)
        else:
            raise ValueError("Invalid input type for ViewsDataset")


    def _preprocess_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Fill the dense grid (every last-step entity present in every time step).

        Delegates to `forecast.ingestion.dense_grid.fill_dense_grid` (extracted in Phase 1
        of #87) — see that module for the C-87/ADR-021 fill semantics.
        """
        return fill_dense_grid(
            df, self._time_values, self._time_id, self._entity_id, self._fill_value
        )

    def _init_dataframe(
        self, dataframe: pd.DataFrame, targets: Optional[List[str]] = None
    ) -> None:
        if not isinstance(dataframe, pd.DataFrame) or dataframe.empty:
            raise ValueError("Dataframe is empty or not a valid DataFrame")
        # Permanent input-normalization shim (register C-61): upstream still carries a mixed
        # `priogrid_gid` / `priogrid_id` vocabulary — 774 platform parquet files bake `priogrid_gid`
        # into their Arrow schema (C-62/C-63) — so crafdapi normalizes to `priogrid_id` at the boundary.
        # This is a stable input contract, NOT a temporary hack to be removed.
        if dataframe.index.names[1] == "priogrid_gid":
            logger.warning(
                "index level 1 is 'priogrid_gid', renaming to 'priogrid_id'"
            )
            dataframe.index = dataframe.index.rename(
                [dataframe.index.names[0], "priogrid_id"]
            )
        self.original_columns = dataframe.columns.tolist()

        # Convert and sort FIRST before saving original index.
        # C-263: `sort_index()` on an already-sorted index is an identity that still allocates a
        # full copy. The producer's historical artifact arrives month-sorted, and the copy was
        # ~0.8 GB of the cold-start peak at 28.4M rows. Skipping it when the index is already
        # monotonic yields the same values in the same order; `_preprocess_dataframe` below sorts
        # again on the way out, so the invariant "self.dataframe is sorted" is unaffected either way.
        converted = self._convert_to_arrays(dataframe)
        self.dataframe = (
            converted if converted.index.is_monotonic_increasing else converted.sort_index()
        )  # Sort early


        self._time_id, self._entity_id = self.dataframe.index.names
        self._rebuild_index_mappings()

        if self._preprocess_input_dataframe:
            self.dataframe = self._preprocess_dataframe(self.dataframe)
            self._rebuild_index_mappings()

        self.validate_indices()

        # Handle situation where you only want specific cols. Too much work. Future problem.
        self.pred_vars = self.get_pred_vars()
        self.is_prediction = len(self.pred_vars) > 0
        if self.is_prediction:
            self.targets = self.pred_vars
            self.features = self.get_features()
            if targets is not None:
                logger.warning(
                    f"Ignoring specified dependent variables in prediction mode. Make sure all columns follow pred_* naming scheme. ({self.original_columns})"
                )
            self.sample_size = self._validate_prediction_structure()
        else:
            self.targets = targets
            self.features = self.get_features()
            if self.targets is not None:
                missing_vars = set(self.targets) - set(self.dataframe.columns)
                if missing_vars:
                    raise ValueError(f"Missing targets: {missing_vars}")
            else:
                raise ValueError(
                    "Targets must be specified for non-prediction dataframes. Example usage: ViewsDataset(dataframe, targets=['ln_sb_best'])"
                )

            if self.broadcast_features:
                self._validate_feature_samples()
            else:
                # S4e (Fix A): the historical/scalar feature path no longer materializes a
                # per-cell object array for each scalar column — that ~15x blow-up (≈10 GB at
                # global-historical scale) OOM-killed the worker. Scalar columns are moved into a
                # contiguous float64 `_feature_store` below (the feature analogue of the S4d
                # `_sample_store`) and dropped from `.dataframe`. Only genuinely list/array-valued
                # columns (rare on this path) are materialized as object cells here as a fallback.
                for col in self.dataframe.columns:
                    first_val = (
                        self.dataframe[col].iloc[0]
                        if not self.dataframe.empty
                        else None
                    )
                    if isinstance(first_val, list):
                        # Convert lists to numpy arrays
                        self.dataframe[col] = self.dataframe[col].apply(np.array)
                # Disable tensor operations by not setting sample_size
                self.sample_size = None
        self._split_tensor_cache = {}
        self._entity_metadata_cache = None

        # S4d (#154, the keystone / declared stop-point): the contiguous (N,S) float32
        # frame is now the CANONICAL sample store — the object-dtype pred_* cells are
        # dropped from `.dataframe` so they are no longer persisted (frame fully
        # canonical, byte-identical). Every consumer reads samples via `_sample_array`,
        # which now serves them from `_sample_store`; clone shares these buffers (C-137)
        # and the contiguous block is far more compact than per-cell Python objects at
        # grid scale (C-148). The historical/scalar (feature) path keeps its pandas cells
        # — out of migration scope (ADR-030 §1).
        self._sample_store = {}
        # S4e (Fix A): the feature/historical analogue of `_sample_store` — a contiguous float64
        # `(N,1)` block per scalar column, so values are stored once as raw floats instead of one
        # Python object array per cell (~15x smaller at grid scale). Object cells are dropped from
        # `.dataframe`; subsets rebuild from here. Populated for the non-broadcast feature path
        # only (broadcast keeps its pandas cells / tensor path).
        self._feature_store = {}
        if self.is_prediction:
            self._sample_store = {
                var: self._stack_cells(self.dataframe[var]) for var in self.targets
            }
            # Guard the load-bearing invariant (register C-155): each stored `(N,S)` block
            # is positionally aligned to `.dataframe.index`. If these ever desync (a future
            # reorder of one but not the other), samples would be silently mislabeled —
            # so assert it here, where the store is built, not discover it downstream.
            n_rows = len(self.dataframe)
            for var, block in self._sample_store.items():
                if len(block) != n_rows:
                    raise ValueError(
                        f"sample store for {var!r} has {len(block)} rows but the index has "
                        f"{n_rows} — frame/index misalignment (C-155)"
                    )
            self.dataframe = self.dataframe.drop(columns=self.targets)
        elif not self.broadcast_features:
            # Move every scalar column (targets ∪ features) into the contiguous float64 store,
            # then drop them — `.dataframe` becomes index-only. float64 preserves the historical
            # dtype exactly (the served/persisted value); `_sample_array` downcasts to float32
            # only for the callers that expect it.
            scalar_cols = [
                col
                for col in self.dataframe.columns
                if not self.dataframe.empty
                and isinstance(self.dataframe[col].iloc[0], (int, float, np.number))
            ]
            for col in scalar_cols:
                block = self.dataframe[col].to_numpy(dtype=np.float64).reshape(-1, 1)
                self._feature_store[col] = np.ascontiguousarray(block)
            # Same C-155 alignment guard as the sample store.
            n_rows = len(self.dataframe)
            for col, block in self._feature_store.items():
                if len(block) != n_rows:
                    raise ValueError(
                        f"feature store for {col!r} has {len(block)} rows but the index has "
                        f"{n_rows} — frame/index misalignment (C-155)"
                    )
            self.dataframe = self.dataframe.drop(columns=scalar_cols)

    def __deepcopy__(self, memo):
        """Shallow-copy DataFrames (shares underlying numpy arrays) but copy structure.

        Safe as long as consumers do not mutate cell contents in-place.
        At global scale (780K rows), this is ~1,700x faster than default deepcopy.
        """
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if isinstance(v, pd.DataFrame):
                setattr(result, k, v.copy(deep=False))
            elif k == "_split_tensor_cache":
                setattr(result, k, {})
            elif isinstance(v, pd.Index):
                setattr(result, k, v.copy())
            else:
                setattr(result, k, copy.copy(v))
        return result

    def copy(self) -> "_GridDataset":
        """Return an independent copy via the optimised ``__deepcopy__``.

        Shares the underlying numpy sample buffers (≈1,700× faster than a true deepcopy at
        global scale; safe as long as cell contents are not mutated in place). This is the
        public clone API the serving layer uses instead of ``copy.deepcopy`` (Phase 4a, #112 —
        register C-137).
        """
        return copy.deepcopy(self)

    def _rebuild_index_mappings(self) -> None:
        """Create sorted index mappings for tensor alignment using pandas Index."""
        self._time_values = (
            self.dataframe.index.get_level_values(self._time_id).unique().sort_values()
        )
        self._entity_values = (
            self.dataframe.index.get_level_values(self._entity_id)
            .unique()
            .sort_values()
        )

        # Convert to pandas Index for efficient lookups
        self._time_values = pd.Index(self._time_values)
        self._entity_values = pd.Index(self._entity_values)

    def _convert_to_arrays(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Convert list columns in a DataFrame to numpy arrays.

        Parameters:
        df (pd.DataFrame): The input DataFrame with columns that may contain lists.

        Returns:
        pd.DataFrame: A new DataFrame with list columns converted to numpy arrays.
        """
        return to_array_columns(df)

    def _validate_prediction_structure(self) -> int:
        """Validate and normalize prediction structure for both scalar and distributional predictions."""
        if self.is_prediction:
            # Convert scalar predictions to single-element arrays
            for var in self.targets:
                first_val = self.dataframe[var].iloc[0]

                # Handle different data types
                if isinstance(first_val, (int, float, np.number)) or np.isscalar(
                    first_val
                ):
                    # Convert scalar to single-element array
                    self.dataframe[var] = self.dataframe[var].apply(
                        lambda x: np.array([x], dtype=np.float32)
                    )
                elif isinstance(first_val, list):
                    # Convert lists to numpy arrays
                    self.dataframe[var] = self.dataframe[var].apply(
                        lambda x: np.array(x, dtype=np.float32)
                    )
                elif isinstance(first_val, np.ndarray):
                    # Ensure consistent dtype
                    self.dataframe[var] = self.dataframe[var].apply(
                        lambda x: x.astype(np.float32)
                    )
                else:
                    raise TypeError(
                        f"Invalid type {type(first_val)} for prediction column {var}"
                    )

            # Verify all prediction columns now contain arrays
            if not all(
                self.dataframe[var].apply(lambda x: isinstance(x, np.ndarray)).all()
                for var in self.targets
            ):
                raise ValueError(
                    "Prediction columns must contain array-like values after conversion"
                )

            # Check consistent sample sizes
            sample_sizes = [len(self.dataframe[var].iloc[0]) for var in self.targets]
            if len(set(sample_sizes)) > 1:
                raise ValueError(
                    f"Inconsistent sample sizes in prediction columns: {sample_sizes}"
                )

            # Ensure no independent variables present
            if len(self.features) > 0:
                raise ValueError(
                    f"Prediction dataframe should only contain pred_* columns. Found {self.features}"
                )

            return sample_sizes[0]
        return 0

    def _validate_feature_samples(self) -> None:
        sample_sizes = []
        for col in self.dataframe.columns:
            first_val = self.dataframe[col].iloc[0]

            # Convert scalars to arrays to prevent TypeError: object of type 'numpy.float64' has no len()
            if isinstance(first_val, (int, float, np.number)):
                self.dataframe[col] = self.dataframe[col].apply(lambda x: np.array([x]))
                first_val = self.dataframe[col].iloc[0]

            sample_sizes.append(len(first_val))

        if len(set(sample_sizes)) > 1:
            max_samples = max(sample_sizes)
            for col in self.dataframe.columns:
                col_vals = self.dataframe[col]
                if isinstance(col_vals.iloc[0], np.ndarray):
                    if len(col_vals.iloc[0]) != max_samples:
                        self.dataframe[col] = col_vals.apply(
                            lambda x: np.resize(x, max_samples)
                        )
                else:
                    self.dataframe[col] = col_vals.apply(
                        lambda x: np.full(max_samples, x)
                    )
            self.sample_size = max_samples
        elif sample_sizes:
            self.sample_size = sample_sizes[0]
        else:
            self.sample_size = 1

    def validate_indices(self) -> None:
        """
        Validate the structure of the DataFrame's MultiIndex.

        This method checks if the DataFrame's index is a MultiIndex and ensures
        that it has exactly two levels. If these conditions are not met, a
        ValueError is raised.

        Raises:
            ValueError: If the DataFrame's index is not a MultiIndex.
            ValueError: If the MultiIndex does not have exactly two levels.
        """
        if not isinstance(self.dataframe.index, pd.MultiIndex):
            raise ValueError("DataFrame must have a MultiIndex")
        if len(self.dataframe.index.names) != 2:
            raise ValueError("Must have exactly two index levels")

    def get_pred_vars(self) -> List[str]:
        """
        Identify prediction variables starting with 'pred_'.

        Returns:
            List[str]: A list of column names from the dataframe that start with 'pred_'.
        """
        # if self.targets:
        #     raise ValueError("Cannot identify prediction variables when dependent variables are specified")
        return [col for col in self.dataframe.columns if col.startswith("pred_")]

    def get_features(self) -> List[str]:
        """
        Get independent variables.

        This method returns a list of column names from the dataframe that are
        considered independent variables. Independent variables are those that
        are not present in the list of dependent variables (`targets`).

        Returns:
            List[str]: A list of column names representing the independent variables.
        """
        if self.is_prediction:
            return [col for col in self.dataframe.columns if col not in self.pred_vars]

        try:
            return [col for col in self.dataframe.columns if col not in self.targets]
        except TypeError as e:
            raise TypeError(
                f"{e}: Probable cause: Invalid type for targets: {type(self.targets)}. Expected list of strings like ['ln_sb_best']"
            )

    def to_tensor(
        self, include_targets: bool = True
    ) -> Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
        """
        Converts the data to a tensor format.

        Parameters:
        include_targets (bool): If True, include dependent variables in the tensor. Default is True.

        Returns:
        Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]:
            - If `self.is_prediction` is True, returns the prediction tensor as a numpy array.
            - Otherwise, returns the features tensor, optionally including dependent variables, as a numpy array or a tuple of numpy arrays.
        """
        if self.is_prediction:
            if not hasattr(self, "_prediction_tensor_cache"):
                self._prediction_tensor_cache = self._prediction_to_tensor()
            return self._prediction_tensor_cache
        else:
            if not self.broadcast_features:
                raise ValueError(
                    "Tensor operations are disabled when broadcast_features=False"
                )
            if not hasattr(self, "_features_tensor_cache"):
                self._features_tensor_cache = self._features_to_tensor(
                    include_targets=True
                )
            if include_targets:
                return self._features_tensor_cache
            else:
                # Extract indices of independent variables
                feature_indices = [
                    self.dataframe.columns.get_loc(var) for var in self.features
                ]
                return self._features_tensor_cache[:, :, :, feature_indices]

    def _features_to_tensor(self, include_targets: bool = True) -> np.ndarray:
        """
        Converts the dataframe features into a 3D tensor.
        Parameters:
        -----------
        include_targets : bool, optional
            If True, includes dependent variables in the tensor. Defaults to True.
        Returns:
        --------
        np.ndarray
            A 4D tensor with dimensions (time_steps, entities, samples, features).
        Notes:
        ------
        - The tensor is filled with NaN values initially.
        - The tensor is constructed by reindexing the dataframe to ensure all time steps and entities are included.
        - The resulting tensor has the shape (number of time steps, number of entities, number of features).
        """

        if self.dataframe.empty:
            return np.empty((0, 0, 0, 0))

        current_columns = self.dataframe.columns if include_targets else self.features

        # Get aligned index
        full_idx = pd.MultiIndex.from_product(
            [self._time_values, self._entity_values],
            names=[self._time_id, self._entity_id],
        )

        # Stack all columns simultaneously
        tensor = np.stack(
            [
                np.stack(
                    self.dataframe[col]
                    .reindex(full_idx)
                    .apply(lambda x: x if isinstance(x, np.ndarray) else np.array([x]))
                    .values
                ).reshape(
                    len(self._time_values), len(self._entity_values), self.sample_size
                )
                for col in current_columns
            ],
            axis=-1,
        )
        return tensor

    def _prediction_to_tensor(self) -> np.ndarray:
        """
        Convert predictions to a 4D tensor.
        This method converts the predictions stored in the dataframe to a 4D tensor
        with dimensions (time × entity × samples × targets).

        Returns:
            np.ndarray: A 4D tensor with dimensions (time × entity × samples × targets),
                        where each element is a prediction value or NaN if the prediction
                        is not available.
        """

        n_time, n_entity = len(self._time_values), len(self._entity_values)

        # Pre-allocate tensor with correct NaN structure
        tensor = np.full(
            (n_time, n_entity, self.sample_size, len(self.targets)),
            np.nan,
            dtype=np.float64,  # Match original data type
        )

        # Positional map current rows -> the dense (time x entity) grid. Pandas is used
        # ONLY to align indices here; the sample VALUES are sourced from the frame
        # chokepoint (`_sample_array`), not the object-dtype cells (epic #154 / S4a).
        # Absent grid cells (sparse input) stay NaN — preserving the legacy reindex
        # behaviour byte-identically (proven in test_representation_parity.py).
        full_idx = pd.MultiIndex.from_product(
            [self._time_values, self._entity_values],
            names=[self._time_id, self._entity_id],
        )
        row_pos = (
            pd.Series(np.arange(len(self.dataframe)), index=self.dataframe.index)
            .reindex(full_idx)
            .to_numpy()
        )
        present = ~np.isnan(row_pos)
        src_rows = row_pos[present].astype(np.intp)

        for var_idx, var in enumerate(self.targets):
            samples = self._sample_array(var).astype(np.float64)  # (N, S) from the frame
            flat = tensor[:, :, :, var_idx].reshape(n_time * n_entity, self.sample_size)
            flat[present] = samples[src_rows]
            tensor[:, :, :, var_idx] = flat.reshape(
                n_time, n_entity, self.sample_size
            )

        return tensor

    def _prediction_month_block(
        self,
        month_id,
        var: str,
        entity_indices: Optional[List[int]],
        sample_indices: Optional[List[int]],
    ) -> np.ndarray:
        """S6b-1 (#208): the `(n_entity, n_samples)` float64 block for ONE month + one target,
        NaN-filling absent `(month, cell)` grid cells — the per-month equivalent of a single
        `(1, n_entity, S)` slab of `_prediction_to_tensor`, then subset by entity/sample exactly
        as `get_subset_tensor` does. Streaming this per month is what keeps serving off the full
        `(n_time, n_entity, S, targets)` grid (~57 GB at full scale); the values are byte-identical
        (float32→float64 is exact, and indexing before the upcast pages only the month under mmap).
        """
        full_idx = pd.MultiIndex.from_product(
            [[month_id], self._entity_values], names=[self._time_id, self._entity_id]
        )
        row_pos = (
            pd.Series(np.arange(len(self.dataframe)), index=self.dataframe.index)
            .reindex(full_idx)
            .to_numpy()
        )
        present = ~np.isnan(row_pos)
        src_rows = row_pos[present].astype(np.intp)

        block = np.full((len(self._entity_values), self.sample_size), np.nan, dtype=np.float64)
        block[present] = self._sample_array(var)[src_rows].astype(np.float64)

        if entity_indices is not None:
            block = block[entity_indices]
        if sample_indices is not None:
            block = block[:, sample_indices]
        return block

    @staticmethod
    def _stack_cells(column: pd.Series) -> np.ndarray:
        """Materialize an object-dtype sample column into a contiguous ``(N, S)`` float32
        array, in row order. The one place per-cell arrays are stacked into a frame."""
        return np.stack([np.asarray(v, dtype=np.float32) for v in column.to_numpy()])

    def _sample_array(self, var) -> np.ndarray:
        """Return a prediction var's samples as a contiguous ``(N, S)`` float32 array,
        over the current (sorted) rows.

        The single chokepoint for the pandas-to-edges store flip (#154): every consumer
        that needs the samples as an array reads them *here*. As of S4d the canonical
        store is the contiguous frame (`_sample_store`), built once at construction; the
        object-dtype pred_* cells are no longer persisted. Falls back to stacking from
        ``.dataframe`` for the historical/scalar (feature) path, which keeps its cells.
        Returns float32 (the storage dtype); callers upcast as needed.
        """
        store = getattr(self, "_sample_store", None)
        if store is not None and var in store:
            return store[var]
        feat = getattr(self, "_feature_store", None)
        if feat is not None and var in feat:
            # Feature/historical (S4e): the store is float64; return float32 to preserve the
            # exact contract of the legacy `_stack_cells` path (to_frames reads here).
            return feat[var].astype(np.float32)
        return self._stack_cells(self.dataframe[var])

    def samples(self, var: str) -> np.ndarray:
        """Public accessor for a prediction var's canonical `(N,S)` float32 samples.

        Post-S4d the `pred_*` columns are no longer in `.dataframe` (they live in the frame
        store); this is the named, discoverable path to the draws — use it instead of the
        removed ``dataset.dataframe[var]`` (register C-153). Rows follow ``.dataframe.index``.
        """
        if self.is_prediction and var not in self.targets:
            raise KeyError(
                f"{var!r} is not a prediction target; available: {list(self.targets)}"
            )
        return self._sample_array(var)

    def validate_value_plausibility(self) -> None:
        """C-72: reject schema-valid-but-implausible prediction values before they reach
        aggregation/serving.

        Conflict fatality predictions are counts / log-counts: every posterior sample must
        be **finite** and **non-negative**. A schema-valid file can still carry NaN/Inf or
        negative values (e.g. an upstream defect) that would otherwise flow straight into the
        HDI/MAP and the UN-facing API response with no signal. Raises ``ValueError`` on
        violation (the API ingestion path surfaces it as HTTP 500). No-op for non-prediction
        (feature/historical) datasets.
        """
        if not getattr(self, "is_prediction", False):
            return
        for var in self.pred_vars:
            assert_prediction_samples_plausible(var, self._sample_array(var))

    def to_frames(self) -> dict:
        """Build a views-frames value object per target variable from the ingested grid (#88).

        Prediction mode -> ``{var: PredictionFrame (N, S)}``; feature/historical mode ->
        ``{var: TargetFrame (N, 1)}``, both at ``SpatialLevel.PGM``. Rows follow the sorted
        ``(time, entity)`` index. Geography is **not** embedded (views-frames ADR-013/014) —
        it lives in crafdapi's separate geo-metadata table and is injected at aggregation.
        """
        time = self.dataframe.index.get_level_values(self._time_id).to_numpy()
        unit = self.dataframe.index.get_level_values(self._entity_id).to_numpy()
        frames = {}
        for var in self.targets:
            stacked = self._sample_array(var)
            if self.is_prediction:
                frames[var] = build_prediction_frame(stacked, time, unit)
            else:
                frames[var] = build_target_frame(stacked[:, 0], time, unit)
        return frames

    def get_subset_tensor(
        self,
        time_ids: Optional[Union[int, List[int]]] = None,
        features: Optional[Union[str, List[str]]] = None,
        sample_idx: Optional[Union[int, List[int]]] = None,
        entity_ids: Optional[Union[int, List[int]]] = None,
    ) -> np.ndarray:
        """
        Get subset of tensor for specified time, feature, sample, and/or entity IDs

        Parameters:
        time_ids: Single or list of time IDs (None for all)
        features: Single or list of feature names (None for all)
        sample_idx: Single or list of sample indices (None for all)
        entity_ids: Single or list of entity IDs (None for all)

        Returns:
        np.ndarray: Subset tensor with dimensions [time, entity, sample, feature]
        """

        tensor = self.to_tensor()

        # Convert scalar inputs to lists
        if time_ids is not None and not isinstance(time_ids, list):
            time_ids = [time_ids]
        if entity_ids is not None and not isinstance(entity_ids, list):
            entity_ids = [entity_ids]
        if sample_idx is not None and not isinstance(sample_idx, list):
            sample_idx = [sample_idx]
        if features is not None and not isinstance(features, list):
            features = [features]

        # Get indices using pandas Index for vectorized lookup
        time_indices = None
        if time_ids is not None:
            time_indices = self._time_values.get_indexer(time_ids)
            if (time_indices == -1).any():
                invalid = [tid for tid, idx in zip(time_ids, time_indices) if idx == -1]
                raise KeyError(f"Invalid time IDs: {invalid}")
            time_indices = time_indices.tolist()

        entity_indices = None
        if entity_ids is not None:
            entity_indices = self._entity_values.get_indexer(entity_ids)
            if (entity_indices == -1).any():
                invalid = [
                    eid for eid, idx in zip(entity_ids, entity_indices) if idx == -1
                ]
                raise KeyError(f"Invalid entity IDs: {invalid}")
            entity_indices = entity_indices.tolist()

        # Get feature indices
        feature_indices = None
        if features is not None:
            if self.is_prediction:
                # For prediction datasets, features refers to target variables
                invalid = set(features) - set(self.targets)
                if invalid:
                    raise ValueError(f"Invalid features specified: {invalid}")
                feature_indices = [self.targets.index(f) for f in features]
            else:
                # For regular datasets, features refers to feature columns
                invalid = set(features) - set(self.features)
                if invalid:
                    raise ValueError(f"Invalid features specified: {invalid}")
                feature_indices = [self.dataframe.columns.get_loc(f) for f in features]

        # Get sample indices
        sample_indices = None
        if sample_idx is not None:
            sample_indices = sample_idx
            # Validate sample indices
            max_sample = tensor.shape[2] - 1
            if any(idx < 0 or idx > max_sample for idx in sample_indices):
                raise ValueError(f"Sample indices must be between 0 and {max_sample}")

        # Perform subsetting using numpy advanced indexing
        result = tensor
        if time_indices is not None:
            result = result[time_indices]
        if entity_indices is not None:
            result = result[:, entity_indices]
        if sample_indices is not None:
            result = result[:, :, sample_indices]
        if feature_indices is not None:
            result = result[..., feature_indices]

        return result

    def _subset_mask(
        self,
        time_ids: Optional[Union[int, List[int]]] = None,
        entity_ids: Optional[Union[int, List[int]]] = None,
    ) -> np.ndarray:
        """Boolean mask over `.dataframe` rows for a (time, entity) selection.

        The selection step of `get_subset_dataframe`, without materialising any column. The
        aggregate path (ADR-030 S7) needs the same selection but reads its samples from the
        `(N, S)` store via `_sample_array`, so it takes the mask and never builds the
        object-dtype sample columns. Extracted so the two paths cannot drift on what "the
        subset" means.
        """
        mask = np.ones(len(self.dataframe), dtype=bool)
        if time_ids is not None:
            if not isinstance(time_ids, list):
                time_ids = [time_ids]
            mask &= self.dataframe.index.get_level_values(self._time_id).isin(time_ids)
        if entity_ids is not None:
            if not isinstance(entity_ids, list):
                entity_ids = [entity_ids]
            mask &= self.dataframe.index.get_level_values(self._entity_id).isin(
                entity_ids
            )
        return mask

    def get_subset_dataframe(
        self,
        time_ids: Optional[Union[int, List[int]]] = None,
        features: Optional[Union[str, List[str]]] = None,
        sample_idx: Optional[Union[int, List[int]]] = None,
        entity_ids: Optional[Union[int, List[int]]] = None,
    ) -> pd.DataFrame:
        """
        Get subset dataframe for specified time, feature, sample, and/or entity IDs

        Parameters:
        time_ids: Single or list of time IDs (None for all)
        features: Single or list of feature names (None for all)
        sample_idx: Single or list of sample indices (None for all)
        entity_ids: Single or list of entity IDs (None for all)
        """
        if entity_ids == [] or time_ids == []:
            # Return empty DataFrame if no entities or time IDs specified
            return self.dataframe.iloc[0:0]
        mask = self._subset_mask(time_ids=time_ids, entity_ids=entity_ids)

        # Validate + resolve the selected columns
        if features is not None:
            if not isinstance(features, list):
                features = [features]

            if self.is_prediction:
                invalid = set(features) - set(self.targets)
            else:
                valid_cols = set(self.features)
                if self.targets:
                    valid_cols |= set(self.targets)
                invalid = set(features) - valid_cols
            if invalid:
                raise ValueError(f"Invalid features specified: {invalid}")
            selected_cols = features
        elif self.is_prediction:
            # pred_* sample columns live in `_sample_store` (S4d), not `.dataframe`.
            selected_cols = list(self.targets)
        else:
            # Feature/historical: scalar columns live in `_feature_store` (S4e), not `.dataframe`;
            # any non-scalar leftover column is still in the frame.
            feat = getattr(self, "_feature_store", {})
            selected_cols = list(feat.keys()) + list(self.dataframe.columns)

        # Validate sample subsetting
        if sample_idx is not None:
            if not isinstance(sample_idx, list):
                sample_idx = [sample_idx]
            if self.sample_size is None:
                raise ValueError(
                    "Cannot subset by sample when sample_size is not defined"
                )
            max_sample = self.sample_size - 1
            if any(idx < 0 or idx > max_sample for idx in sample_idx):
                raise ValueError(f"Sample indices must be between 0 and {max_sample}")

        # Prediction sample columns are rebuilt from the `(N,S)` frame chokepoint
        # (`_sample_array`), not sliced off the object-dtype store — so the subset survives
        # the S4d cell-drop (#154 / S4c). Byte-identical: the stored cells are already
        # float32 (proven in test_representation_parity.py). The historical/scalar (feature)
        # path stays pandas — out of migration scope (ADR-030 §1).
        if self.is_prediction:
            positions = np.flatnonzero(mask)
            index = self.dataframe.index[mask]
            data = {}
            for col in selected_cols:
                arr = self._sample_array(col)[positions]
                if sample_idx is not None:
                    arr = arr[:, sample_idx]
                # Explicit object dtype so an empty selection stays object-typed
                # (a bare list would infer float64), matching the legacy slice.
                data[col] = pd.Series(list(arr), index=index, dtype=object)
            return pd.DataFrame(data, index=index)

        # Feature/historical path: rebuild the requested columns from the contiguous
        # `_feature_store` (S4e) as float64 size-1 object cells — byte-identical to the pre-S4e
        # object-cell slice. Any non-scalar column left in `.dataframe` (rare) is sliced directly.
        positions = np.flatnonzero(mask)
        index = self.dataframe.index[mask]
        feat = getattr(self, "_feature_store", {})
        data = {}
        for col in selected_cols:
            if col in feat:
                block = feat[col][positions]  # (M, 1) float64
                if sample_idx is not None:
                    block = block[:, sample_idx]
                data[col] = pd.Series(list(block), index=index, dtype=object)
            else:
                s = self.dataframe.loc[mask, col]
                if sample_idx is not None:
                    s = s.apply(lambda x: x[sample_idx] if isinstance(x, np.ndarray) else x)
                data[col] = s
        return pd.DataFrame(data, index=index)

    @property
    def num_entities(self) -> int:
        return len(self.dataframe.index.get_level_values(self._entity_id).unique())

    @property
    def num_time_steps(self) -> int:
        return len(self.dataframe.index.get_level_values(self._time_id).unique())

    @property
    def num_features(self) -> int:
        return len(self.features)

    def __repr__(self) -> str:
        return (
            f"_GridDataset(time_steps={self.num_time_steps}, "
            f"entities={self.num_entities}, "
            f"features={self.num_features}, "
            f"prediction_mode={self.is_prediction})"
        )

    def _create_series_value_dataframe(self, var, summary, time_ids=None, entity_ids=None) -> pd.DataFrame:
        """Lay one target's summary matrices onto this dataset's `(time, entity)` axes
        (`json_contract.series_value_dataframe`). Columns are named by the target, not renamed."""
        return series_value_dataframe(
            var,
            summary,
            self._time_id,
            self._entity_id,
            self.dataframe.index.get_level_values(self._time_id).unique(),
            self.dataframe.index.get_level_values(self._entity_id).unique(),
            time_ids,
            entity_ids,
        )

    def summarize(
        self,
        features: Optional[Union[str, List[str]]] = None,
        sample_idx: Optional[Union[int, List[int]]] = None,
        time_ids: Optional[Union[int, List[int]]] = None,
        entity_ids: Optional[Union[int, List[int]]] = None,
    ) -> pd.DataFrame:
        """The served summaries of each target's draws, per grid cell and month (#204).

        Returns a frame indexed (time, entity) with, per target, the columns
        `json_contract.series_value_column_names(var)`: `{var}_p_gt0` … `{var}_n_draws`
        (`forecast.summarize.draws`). Raises ValueError on non-prediction data.
        """
        if not self.is_prediction:
            raise ValueError(NEEDS_DRAWS)

        # Row-count, not `.empty`: post-S4d a prediction `.dataframe` carries the index
        # but zero columns (sample cells live in `_sample_store`), and a 0-column frame
        # is `.empty` even with rows — which would wrongly short-circuit serving.
        if len(self.dataframe) == 0:
            return pd.DataFrame()

        # Validate features parameter
        if features is not None:
            if not isinstance(features, list):
                features = [features]
            invalid = set(features) - set(self.targets)
            if invalid:
                raise ValueError(f"Invalid features specified: {invalid}")
            selected_vars = features
        else:
            selected_vars = self.targets

        # S6b-1 (#208): stream the reduction ONE MONTH at a time instead of materializing the
        # full `(n_time, n_entity, S, targets)` float64 grid (~57 GB at full scale). The reduction
        # is per-row, so per-month blocks reduced and
        # stacked in month order are byte-identical to the whole-grid path — guarded by the golden
        # output test. Axis resolution mirrors `get_subset_tensor` exactly (order preserved).
        wrap = lambda v: [v] if (v is not None and not isinstance(v, list)) else v  # noqa: E731
        time_ids_l, entity_ids_l, sample_idx_l = wrap(time_ids), wrap(entity_ids), wrap(sample_idx)

        if time_ids_l is not None:
            time_indices = self._time_values.get_indexer(time_ids_l)
            if (time_indices == -1).any():
                invalid = [t for t, i in zip(time_ids_l, time_indices) if i == -1]
                raise KeyError(f"Invalid time IDs: {invalid}")
            ordered_months = [self._time_values[i] for i in time_indices]
        else:
            ordered_months = list(self._time_values)

        entity_indices = None
        if entity_ids_l is not None:
            entity_indices = self._entity_values.get_indexer(entity_ids_l)
            if (entity_indices == -1).any():
                invalid = [e for e, i in zip(entity_ids_l, entity_indices) if i == -1]
                raise KeyError(f"Invalid entity IDs: {invalid}")
            entity_indices = entity_indices.tolist()

        sample_indices = None
        if sample_idx_l is not None:
            max_sample = self.sample_size - 1
            if any(i < 0 or i > max_sample for i in sample_idx_l):
                raise ValueError(f"Sample indices must be between 0 and {max_sample}")
            sample_indices = sample_idx_l

        # Per target, each month's rows are summarised in one call (`summarize_draws`), streamed
        # a month at a time so one month of cells is resident, not the whole grid. Columns are
        # named by the target here; the rename to the outcome code happens where it is served.
        acc = {v: {q: [] for q in schema.SUMMARY_QUANTITIES} for v in selected_vars}
        for month_id in ordered_months:
            for var_name in selected_vars:
                block = self._prediction_month_block(
                    month_id, var_name, entity_indices, sample_indices
                )  # (n_entity, n_samples) float64
                for q, values in summarize_draws(block).items():
                    acc[var_name][q].append(values)

        # An empty month selection (e.g. `time_ids=[]`) yields empty `(0, n_entity)` stats, not a
        # 500; `np.stack([])` would raise, so build the empty blocks explicitly.
        n_entity_sub = len(entity_indices) if entity_indices is not None else len(self._entity_values)

        def _stack(rows):
            return (np.stack(rows, axis=0) if ordered_months
                    else np.empty((0, n_entity_sub), dtype=np.float64))

        results = [
            self._create_series_value_dataframe(
                var_name, {q: _stack(acc[var_name][q]) for q in schema.SUMMARY_QUANTITIES},
                time_ids, entity_ids,
            )
            for var_name in selected_vars
        ]
        return pd.concat(results, axis=1)


