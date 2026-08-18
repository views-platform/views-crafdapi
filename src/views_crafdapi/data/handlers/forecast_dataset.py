import json
import logging
import pandas as pd
import numpy as np
from typing import List, Union, Optional
from views_crafdapi.forecast.aggregate.cross_level import aggregate_via_leaf, elementwise_sum
from views_crafdapi.forecast.aggregate.reduction import joint_sum_to_level
from views_crafdapi.forecast.geography.metadata_table import (
    LEVEL_METADATA_COLUMNS,
    LEVELS,
    resolve_level_cells,
)
from views_crafdapi.forecast.ingestion.plausibility import (
    assert_geo_metadata_plausible,
)
from views_crafdapi.forecast.serialize import schema
from views_crafdapi.forecast.serialize.json_contract import (
    series_value_column_names,
    series_value_data,
)
from views_crafdapi.forecast.summarize.estimator import collapse
from views_frames import SpatialLevel
from views_frames.io import npz as frame_npz

from pathlib import Path


from views_crafdapi.data.value_format import _VALUE_SCHEMA_VERSION
from views_crafdapi.data.handlers.grid_dataset import _GridDataset

logger = logging.getLogger(__name__)


class ForecastDataset(_GridDataset):
    """The crafdapi forecast dataset facade (Phase 4a of #87 / #112).

    Composes the extracted `forecast/` modules (ingestion, frames, summarize, geography,
    aggregate, serialize) over a PRIO-GRID-month grid carrying per-cell posterior samples and
    a separate GAUL geo-metadata table. This is the single entry point the API layer uses to
    turn a loaded forecast artifact into served HDI/MAP.
    """

    # Metadata columns required for FAO dataset
    _METADATA_COLS = [
        "pg_xcoord",
        "pg_ycoord",
        "country_iso_a3",
        "admin1_gaul1_code",
        "admin1_gaul1_name",
        "admin1_gaul0_code",
        "admin1_gaul0_name",
        "admin2_gaul2_code",
        "admin2_gaul2_name"
    ]

    # The geography *name* / ISO3 columns are low-cardinality strings (a few thousand unique
    # GAUL/ISO3 labels) repeated across up to ~28M grid rows. Held as pandas `object` they cost
    # ~6-7 GB for the global historical grid; stored as `category` they are a small int codes
    # array + a tiny dictionary (~30x smaller). These are cast in ``__init__`` after the
    # geo_metadata backfill (register memory fix). The ``*_code`` columns stay numeric.
    _CATEGORICAL_METADATA_COLS = [
        "country_iso_a3",
        "admin1_gaul1_name",
        "admin1_gaul0_name",
        "admin2_gaul2_name",
    ]

    def __init__(self, source: pd.DataFrame, targets: list[str] = None, broadcast_features: bool = False, fill_value: float = 0):
        if not isinstance(source, pd.DataFrame):
            raise ValueError("Source must be a pandas DataFrame")
        missing_cols = [col for col in self._METADATA_COLS if col not in source.columns]
        if missing_cols:
            raise ValueError(f"Missing necessary metadata columns: {missing_cols}. Make sure you have run the un_crafd postprocessor first. Found in views-models/postprocessors/un_crafd")

        if targets:
            missing_targets = [t for t in targets if t not in source.columns]
            if missing_targets:
                raise ValueError(
                    f"Specified targets not found in data: {missing_targets}. "
                    f"Columns: {list(source.columns)[:20]}"
                )
        else:
            pred_cols = [col for col in source.columns if col.startswith("pred_")]
            if not pred_cols:
                raise ValueError(
                    f"No prediction columns (pred_*) found and no targets specified. "
                    f"Columns: {list(source.columns)[:20]}"
                )

        if not isinstance(source.index, pd.MultiIndex):
            id_col = "priogrid_id" if "priogrid_id" in source.columns else "priogrid_gid" if "priogrid_gid" in source.columns else None
            if "month_id" in source.columns and id_col:
                source = source.set_index(["month_id", id_col])

        if not isinstance(source.index, pd.MultiIndex) or len(source.index.names) != 2:
            raise ValueError(
                f"Source must have a 2-level MultiIndex, "
                f"got {type(source.index).__name__} with names {list(source.index.names)}"
            )

        if source.index.names[0] != "month_id" or source.index.names[1] not in ("priogrid_id", "priogrid_gid"):
            raise ValueError(
                f"MultiIndex must be (month_id, priogrid_id) or (month_id, priogrid_gid), "
                f"got {tuple(source.index.names)}"
            )

        # Store metadata temporarily - will be aligned after parent init
        _temp_geo_metadata = source[self._METADATA_COLS].copy()
        source = source.drop(columns=self._METADATA_COLS)

        super().__init__(source, targets, broadcast_features, fill_value=fill_value)

        # Parent may have renamed priogrid_gid → priogrid_id; sync temp metadata
        _temp_geo_metadata.index = _temp_geo_metadata.index.set_names(self.dataframe.index.names)

        self.levels = LEVELS
        
        # Align geo_metadata to match the processed dataframe's index
        # This handles cases where parent's _preprocess_dataframe filters/adds rows
        self.geo_metadata = _temp_geo_metadata.reindex(self.dataframe.index)
        
        # For rows that were added by preprocessing (missing combinations), 
        # we need to fill metadata. Get metadata for each unique entity from original data.
        entity_metadata = _temp_geo_metadata.groupby(level=self._entity_id).first()
        
        # Fill missing metadata by looking up the entity
        missing_mask = self.geo_metadata.isna().any(axis=1)
        if missing_mask.any():
            missing_entities = self.geo_metadata[missing_mask].index.get_level_values(self._entity_id).unique()
            for entity_id in missing_entities:
                if entity_id in entity_metadata.index:
                    entity_mask = self.geo_metadata.index.get_level_values(self._entity_id) == entity_id
                    # C-69: write into explicitly-named destination columns rather than a bare
                    # positional `.values` into geo_metadata's column order — so a reorder of
                    # `_METADATA_COLS` can never silently transpose geography labels (e.g. a
                    # country ISO3 into a GAUL-name column) into UN-facing output.
                    self.geo_metadata.loc[entity_mask, entity_metadata.columns] = (
                        entity_metadata.loc[entity_id].values
                    )

        # Downcast the low-cardinality geography name/ISO3 columns to `category` so the
        # resident geo_metadata is compact (~6-7 GB → ~0.2 GB at global-historical scale).
        # This is an idempotent safety net that guarantees the invariant for datasets built by
        # ANY caller; the production path casts these upstream in dataset_service (before this
        # constructor) so the *peak* string transient is freed before the per-cell target
        # arrays are built — see that comment. Cast AFTER the backfill loop above: assigning
        # `entity_metadata` values into a categorical column with previously-unseen categories
        # would raise. Transparent to every geo_metadata consumer — plausibility
        # (`pd.to_numeric`/`.astype(str)`/`.str.fullmatch`), `groupby(level=...).first()`,
        # `.reindex()`, `.isna()`, and `.to_parquet()` (pyarrow dictionary-encodes;
        # `from_value`'s read restores the dtype).
        for _col in self._CATEGORICAL_METADATA_COLS:
            if _col in self.geo_metadata.columns and self.geo_metadata[_col].dtype == object:
                self.geo_metadata[_col] = self.geo_metadata[_col].astype("category")

    def to_value(self, directory: Union[str, Path]) -> None:
        """Serialize the dataset's VALUE (not a pickled object) under ``directory`` (S5, #154).

        Layout: per-target ``frames/<var>/`` via ``views_frames.io.npz`` (the contiguous
        ``(N,S)`` float32 store, mmap-capable) for prediction; ``features/<col>.npy``
        (float64, byte-identical to the object cells) for the historical/scalar path;
        ``geo.parquet`` for the geography table; ``index.npz`` for the row index; and a
        scalar ``manifest.json``. No pickle. The inverse is :meth:`from_value`.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        manifest = {
            "value_schema_version": _VALUE_SCHEMA_VERSION,
            "is_prediction": self.is_prediction,
            "preprocess_input": self._preprocess_input_dataframe,
            "time_id": self._time_id,
            "entity_id": self._entity_id,
            "targets": list(self.targets),
            "pred_vars": list(self.pred_vars),
            "features": list(self.features),
            "sample_size": self.sample_size,
            "broadcast_features": self.broadcast_features,
            "fill_value": self._fill_value,
            "original_columns": list(self.original_columns),
            "level": SpatialLevel.PGM.value,
        }

        time = self.dataframe.index.get_level_values(self._time_id).to_numpy()
        unit = self.dataframe.index.get_level_values(self._entity_id).to_numpy()
        np.savez(directory / "index.npz", time=time, unit=unit)
        self.geo_metadata.to_parquet(directory / "geo.parquet")

        if self.is_prediction:
            frames = self.to_frames()
            for var, frame in frames.items():
                frame.save(directory / "frames" / var)
        else:
            feat_dir = directory / "features"
            feat_dir.mkdir(parents=True, exist_ok=True)
            feat = getattr(self, "_feature_store", {})
            for col, block in feat.items():
                # (N,1) float64 — preserve the historical/scalar dtype exactly (NOT float32).
                np.save(feat_dir / f"{col}.npy", np.asarray(block, dtype=np.float64))
            # Any non-scalar column left in `.dataframe` (rare) is stacked from its object cells.
            for col in self.dataframe.columns:
                arr = np.stack(
                    [np.asarray(v, dtype=np.float64) for v in self.dataframe[col].to_numpy()]
                )
                np.save(feat_dir / f"{col}.npy", arr)
            manifest["feature_columns"] = list(feat.keys()) + list(self.dataframe.columns)

        (directory / "manifest.json").write_text(json.dumps(manifest, default=str))

    @classmethod
    def from_value(cls, directory: Union[str, Path], mmap: bool = False) -> "ForecastDataset":
        """Reconstruct a dataset from a :meth:`to_value` directory WITHOUT re-running the
        object-cell construction path (so prediction cells are never resurrected — C-148).

        Sets `_sample_store` (prediction) or the float64 object-cell `.dataframe` (feature)
        directly, restores the scalar state from the manifest, re-derives index mappings,
        and re-asserts the C-155 store/index alignment. ``mmap`` propagates to the frame
        load (read-only memmap of the `(N,S)` block — keeps peak RAM at the working set).
        """
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        with np.load(directory / "index.npz") as idx:
            time, unit = idx["time"], idx["unit"]
        index = pd.MultiIndex.from_arrays(
            [time, unit], names=[manifest["time_id"], manifest["entity_id"]]
        )

        ds = cls.__new__(cls)
        ds.is_prediction = manifest["is_prediction"]
        ds._time_id = manifest["time_id"]
        ds._entity_id = manifest["entity_id"]
        ds.targets = list(manifest["targets"])
        ds.pred_vars = list(manifest["pred_vars"])
        ds.features = list(manifest["features"])
        ds.sample_size = manifest["sample_size"]
        ds.broadcast_features = manifest["broadcast_features"]
        ds._fill_value = manifest["fill_value"]
        ds.original_columns = list(manifest["original_columns"])
        ds._preprocess_input_dataframe = manifest["preprocess_input"]
        ds.levels = LEVELS
        ds._entity_metadata_cache = None
        ds._split_tensor_cache = {}
        ds._max_tensor_cache_size = 128

        ds._feature_store = {}
        if ds.is_prediction:
            ds._sample_store = {
                var: frame_npz.load(directory / "frames" / var, mmap=mmap)["values"]
                for var in ds.targets
            }
            ds.dataframe = pd.DataFrame(index=index)
        else:
            ds._sample_store = {}
            # Feature/historical (S4e): load the contiguous float64 `(N,1)` blocks straight into
            # `_feature_store` (mmap-backed when requested) instead of resurrecting per-cell object
            # cells — the reconstructed dataset is as lean as the freshly-built one. `.dataframe`
            # is index-only.
            for col in manifest["feature_columns"]:
                arr = np.load(
                    directory / "features" / f"{col}.npy",
                    mmap_mode="r" if mmap else None,
                )
                if arr.ndim == 1:
                    arr = arr.reshape(-1, 1)
                ds._feature_store[col] = arr
            ds.dataframe = pd.DataFrame(index=index)

        ds.original_index = ds.dataframe.index.copy()
        ds._rebuild_index_mappings()
        ds.geo_metadata = pd.read_parquet(directory / "geo.parquet")

        n_rows = len(ds.dataframe)
        for label, store in (("sample", ds._sample_store), ("feature", ds._feature_store)):
            for var, block in store.items():
                if len(block) != n_rows:
                    raise ValueError(
                        f"reconstructed {label} store for {var!r} has {len(block)} rows but the "
                        f"index has {n_rows} — frame/index misalignment (C-155)"
                    )
        return ds

    def validate_indices(self) -> None:
        super().validate_indices()
        if self.dataframe.index.names[0] != "month_id":
            raise ValueError(
                f"ForecastDataset requires index 0 to be 'month_id', found {self.dataframe.index.names}"
            )
        # PRIO-GRID level check (folded in from the retired _PGDataset, S8).
        if self.dataframe.index.names[1] != "priogrid_id":
            raise ValueError(
                f"ForecastDataset requires index 1 to be 'priogrid_id', found {self.dataframe.index.names}"
            )

    def validate_metadata_plausibility(self) -> None:
        """C-72 (metadata facet): reject schema-valid-but-implausible *geographic metadata*
        before it is joined into UN-facing output. Complements
        ``validate_value_plausibility`` (which guards prediction values). Checks PRIO-GRID
        coordinate ranges and ISO3 shape (GAUL codes may be negative for disputed areas —
        #287 follow-up). Operates on non-null values only (missing metadata is a separate
        concern). Raises ``ValueError`` on violation (the API ingestion path surfaces it as
        HTTP 500).
        """
        assert_geo_metadata_plausible(self.geo_metadata)

    def _get_pg_cells(self, level: str, code: Union[str, int]) -> list[int]:
        if level not in self.levels.keys():
            raise ValueError(f"Level must be one of {list(self.levels.keys())}, got {level}")
        return resolve_level_cells(
            self.geo_metadata, self.levels[level], code, self._entity_id
        )
    
    def _elementwise_sum(self, arrays: pd.Series) -> np.ndarray:
        """Sum a group of sample arrays element-wise (joint sampling).

        Delegates to `forecast.aggregate.cross_level.elementwise_sum` (Phase 3 of #87 / #90):
        sample *i* of the result is the sum of sample *i* across all constituent cells,
        preserving cross-cell correlation (register C-70).
        """
        return elementwise_sum(arrays)

    def _frame_native_joint_sum(self, df_to_agg, var, time_col, level_col) -> dict:
        """Joint-sum a target's samples to ``level_col`` via the views-frames leaf (#154 / S4b).

        Materializes the constituent cells once into a contiguous ``(N, S)`` float32 frame
        and sums the aligned draws per ``(time, target_unit)`` group through
        ``aggregate_via_leaf`` — the conservation-correct joint-sum (register C-70) — instead
        of the per-cell pandas object-dtype groupby. An *identity* unit map (each row mapped to
        its own target-unit id) is used, so no cell-id is needed; string level codes (e.g.
        ISO3) are factorized to the leaf's integer unit space and mapped back to the original
        label. Float32 in, float32 out — byte-identical to the legacy ``elementwise_sum``
        groupby (proven in test_representation_parity.py; the float32-vs-float64 sum delta is
        the gated #112, untouched here). Returns ``{(time, label): (S,) float32}``.
        """
        values = self._stack_cells(df_to_agg[var])
        time = df_to_agg[time_col].to_numpy()
        codes = df_to_agg[level_col].to_numpy()
        if np.issubdtype(codes.dtype, np.integer):
            unit_ids = codes.astype(np.int64)
            code_lookup = None
        else:
            ints, uniques = pd.factorize(codes)
            # pd.factorize maps a missing level code (NaN) to -1. Drop those cells so the
            # joint-sum matches the legacy groupby(dropna=True): a cell with no code for
            # this level is excluded from the aggregate, not summed into a phantom -1 unit
            # (register C-146 — ~1.1% of real cells lack a GAUL code; -1 KeyError'd the leaf).
            if (ints < 0).any():
                keep = ints >= 0
                values, time, ints = values[keep], time[keep], ints[keep]
            unit_ids = ints.astype(np.int64)
            code_lookup = dict(enumerate(uniques))
        map_keys = np.column_stack([time.astype(np.int64), unit_ids])
        agg = aggregate_via_leaf(values, time, unit_ids, map_keys, unit_ids, SpatialLevel.PGM)
        sums = {}
        for i, (t, u) in enumerate(zip(agg.index.time, agg.index.unit)):
            label = code_lookup[int(u)] if code_lookup is not None else int(u)
            sums[(int(t), label)] = np.asarray(agg.values[i])
        return sums

    def _aggregate_distributions(self, df: pd.DataFrame, level: str) -> pd.DataFrame:
        """
        Aggregate raw prediction distributions to a higher geographic level.
        
        This method performs element-wise summation of sample distributions,
        which is statistically correct for aggregating probabilistic forecasts.
        The key insight is that if X_i ~ P_i for each cell i, then the aggregate
        sum(X_i) has a distribution that is properly captured by summing
        corresponding samples.
        
        Time dimension is ALWAYS preserved - aggregation happens within each time period.
        
        Args:
            df: DataFrame with array-valued target columns (raw distributions)
            level: Geographic level to aggregate to ('country', 'gaul0', 'gaul1', 'gaul2')
            
        Returns:
            DataFrame with MultiIndex (time_id, geo_unit) and distributions summed element-wise
            within each time period, along with relevant metadata for that level.
        """
        target_level_col = self.levels[level]
        time_col = self._time_id
        
        # Which metadata columns each level carries through aggregation (single source of
        # truth in forecast.geography.metadata_table).
        metadata_cols_to_keep = LEVEL_METADATA_COLUMNS.get(level, [])
        
        # Reset index to make time_id available as a column for grouping
        df_reset = df.reset_index()
        
        # Drop metadata columns except the grouping column and columns we want to keep
        metadata_cols = list(self.geo_metadata.columns)
        cols_to_drop = [
            col for col in metadata_cols 
            if col != target_level_col 
            and col not in metadata_cols_to_keep 
            and col in df_reset.columns
        ]
        df_to_agg = df_reset.drop(columns=cols_to_drop)
        
        # Also drop the entity_id column since we're aggregating to a higher level
        if self._entity_id in df_to_agg.columns:
            df_to_agg = df_to_agg.drop(columns=[self._entity_id])
        
        # Handle empty dataframe case
        if df_to_agg.empty:
            return df_to_agg
        
        # Group by BOTH time and geographic unit to preserve time dimension
        groupby_cols = [time_col, target_level_col]

        # Partition the columns: PREDICTION sample targets are joint-summed frame-natively
        # in float32 (S4b, #154); the historical/scalar (feature) path keeps the legacy
        # float64 `elementwise_sum` — it is out of the frame migration's scope (ADR-030 §1)
        # and must stay byte-identical (the frame path's float32 stack would re-baseline it).
        # Iteration order is df_to_agg.columns, so the assembled column order matches the
        # legacy groupby.
        array_targets = []
        scalar_agg_dict = {}
        for feature in df_to_agg.columns:
            if feature in groupby_cols:
                continue
            if feature in self.targets:
                # Check if values are arrays (distributions) or scalars
                sample_val = df_to_agg[feature].iloc[0]
                if isinstance(sample_val, np.ndarray):
                    if self.is_prediction:
                        array_targets.append(feature)               # frame-native float32
                    else:
                        scalar_agg_dict[feature] = self._elementwise_sum  # legacy float64
                else:
                    scalar_agg_dict[feature] = "sum"
            elif feature in metadata_cols_to_keep:
                # Keep the first value for metadata columns (they should be the same within a group)
                scalar_agg_dict[feature] = "first"
            else:
                col_vals = df_to_agg[feature].values
                if len(col_vals) > 0 and all(np.array_equal(col_vals[0], arr) for arr in col_vals):
                    scalar_agg_dict[feature] = "first"

        # The grouped object fixes the (sorted) (time, target_unit) index — identical to the
        # legacy ``.agg`` index — for both the scalar reductions and the frame-summed targets.
        # `observed=True` is load-bearing: the country level groups by `country_iso_a3`, which
        # is now `category` dtype (memory fix). With the pandas default `observed=False` a
        # categorical group key emits a row for EVERY category — all ~250 countries — even for
        # a single-country request, which would inject phantom NaN rows and make the
        # frame-native-sum lookup at `result[var] = [sums[idx] ...]` KeyError on the unobserved
        # keys. `observed=True` restricts to combinations actually present, exactly reproducing
        # the pre-categorical object-dtype groupby (where `observed` was ignored).
        grouped = df_to_agg.groupby(groupby_cols, observed=True)
        if scalar_agg_dict:
            result = grouped.agg(scalar_agg_dict)
        else:
            result = pd.DataFrame(index=grouped.size().index)

        for var in array_targets:
            sums = self._frame_native_joint_sum(df_to_agg, var, time_col, target_level_col)
            result[var] = [sums[idx] for idx in result.index]

        # Preserve the legacy column order (df_to_agg order, grouping cols excluded).
        ordered_cols = [
            c for c in df_to_agg.columns
            if c not in groupby_cols and (c in array_targets or c in scalar_agg_dict)
        ]
        return result[ordered_cols]
    def calculate_hdi_map(
        self,
        alpha: float = 0.9,
        features: Optional[Union[str, List[str]]] = None,
        sample_idx: Optional[Union[int, List[int]]] = None,
        time_ids: Optional[Union[int, List[int]]] = None,
        entity_ids: Optional[Union[str, List[str]]] = None,
        enforce_non_negative: bool = False,
        with_metadata: bool = True,
        level: Optional[str] = None,
        aggregate: bool = False,
    ) -> pd.DataFrame:
        """
        Calculate HDI and MAP estimates, with optional geographic aggregation.
        
        When `aggregate=True`, this method performs aggregation:
        1. Retrieves raw sample distributions for each PRIO-GRID cell
        2. Sums distributions element-wise across cells within each geographic unit
        3. Computes HDI and MAP on the aggregated distributions
        
        This ensures proper uncertainty quantification at aggregated levels.
        HDI([sum of samples]) ≠ sum of HDI bounds.
        
        Args:
            alpha: Credibility level for HDI (default 0.9 = 90% interval)
            features: Target features to analyze (None for all)
            sample_idx: Sample indices to include (None for all)
            time_ids: Time periods to include (None for all)
            entity_ids: Entity codes (ISO3 for country, GAUL codes for admin levels)
            enforce_non_negative: Clip MAP estimates to >= 0
            with_metadata: Include geographic metadata columns in output
            level: Geographic level ('country', 'gaul0', 'gaul1', 'gaul2')
            aggregate: If True, aggregate to the specified level
            
        Returns:
            DataFrame with MultiIndex (time_id, geo_unit) and columns for each target variable:
            - {var}_lower: Lower bound of the HDI
            - {var}_upper: Upper bound of the HDI  
            - {var}_map: Maximum A Posteriori estimate
            - {var}_min: Minimum sample value (when aggregate=True)
            - {var}_max: Maximum sample value (when aggregate=True)
        """
        # Resolve entity_ids to PRIO-GRID cells if a level is specified
        if level is not None and entity_ids is not None:
            if isinstance(entity_ids, (str, int)):
                entity_ids = [entity_ids]
            pg_cells = []
            for id in entity_ids:
                pg_cells.extend(self._get_pg_cells(level=level, code=id))
            entity_ids = pg_cells
        
        if not aggregate:
            # Standard path: compute HDI at cell level
            result = super().calculate_hdi_map(
                alpha=alpha,
                features=features,
                sample_idx=sample_idx,
                time_ids=time_ids,
                entity_ids=entity_ids,
                enforce_non_negative=enforce_non_negative,
            )
            if with_metadata:
                result = result.join(self.geo_metadata, how="left")
            return result
        
        # Aggregation path (ADR-030 S7): joint-sum the cell samples to `level` as arrays, then
        # reduce. Pandas is asked only for the group index and the metadata columns; the samples
        # are read straight from the contiguous `(N, S)` store and never enter a DataFrame.
        if level is None:
            raise ValueError("Must specify 'level' when aggregate=True")

        target_level_col = self.levels[level]
        time_col = self._time_id
        selected_vars = features if features else self.targets
        if not isinstance(selected_vars, list):
            selected_vars = [selected_vars]
        # Which metadata columns each level carries through aggregation (single source of truth
        # in forecast.geography.metadata_table).
        metadata_cols_to_include = LEVEL_METADATA_COLUMNS.get(level, [])

        # Step 1: the group index and the metadata, from a frame carrying NO sample column.
        # This is the memory story. The path this replaces materialised one ndarray object per
        # (cell, month, target) here — ~7M of them for the delivered run, ~10.9 GB — purely to
        # stack them back into `(N, S)` for the joint-sum two steps later.
        mask = self.subset_mask(time_ids=time_ids, entity_ids=entity_ids)
        positions = np.flatnonzero(mask)
        keyframe = (
            pd.DataFrame(index=self.dataframe.index[mask])
            .join(self.geo_metadata, how="left")
            .reset_index()
        )

        # `observed=True` is load-bearing: the country level groups by `country_iso_a3`, which is
        # `category` dtype. With the pandas default a categorical key emits a row for EVERY
        # category — all ~250 countries — even for a single-country request. This groupby fixes
        # the output index and its order, exactly as the pre-S7 `_aggregate_distributions` did.
        grouped = keyframe.groupby([time_col, target_level_col], observed=True)
        group_meta = (
            grouped.agg({c: "first" for c in metadata_cols_to_include})
            if metadata_cols_to_include
            else None
        )
        out_index = group_meta.index if group_meta is not None else grouped.size().index
        if len(out_index) == 0:
            return pd.DataFrame()

        row_of_key = {key: i for i, key in enumerate(out_index)}
        stats = {
            var: {
                name: np.full(len(out_index), np.nan, dtype=np.float64)
                for name in series_value_column_names(var)
            }
            for var in selected_vars
        }

        # Step 2: per month, per target — take that month's rows as a contiguous `(N, S)` block,
        # joint-sum it to the level, and `collapse` the result in ONE call. Streaming per month
        # holds one month of cells resident instead of the whole request (the cell path's
        # pattern, S6b-1 / #208). Groups are `(time, unit)` and a month is one time value, so no
        # group spans two months: the draws summed, their order, and the reduction are all
        # identical to doing every month at once — which is why the goldens must not move.
        times = keyframe[time_col].to_numpy()
        codes = keyframe[target_level_col].to_numpy()
        sample_index = None
        if sample_idx is not None:
            sample_index = sample_idx if isinstance(sample_idx, list) else [sample_idx]

        for month in pd.unique(times):
            in_month = times == month
            month_positions = positions[in_month]
            month_times = times[in_month]
            month_codes = codes[in_month]
            for var_name in selected_vars:
                values = self._sample_array(var_name)[month_positions]
                if sample_index is not None:
                    values = values[:, sample_index]
                keys, block = joint_sum_to_level(values, month_times, month_codes)
                if not keys:
                    continue

                # Same ADR-025 reduction as the cell path (#222/S4): MAP + fixed 50/90/95 HDIs +
                # severe_scenario, through the shared `schema`/`json_contract` builder — so the
                # aggregate and cell schemas can never diverge. One `collapse` call per (month,
                # target), matching the cell path's call count (C-235).
                cr = collapse(
                    block,
                    masses=schema.MASSES,
                    enforce_non_negative=enforce_non_negative,
                    thresholds=schema.EXCEEDANCE_THRESHOLDS,
                )
                data = series_value_data(
                    var_name,
                    cr.map,
                    cr.severe,
                    cr.bimodality,
                    {m: (cr.lower(m), cr.upper(m)) for m in schema.MASSES},
                    exceedance=cr.exceedance,
                )
                rows = np.fromiter(
                    (row_of_key[k] for k in keys), dtype=np.intp, count=len(keys)
                )
                for name, column in data.items():
                    target = stats[var_name].setdefault(
                        name, np.full(len(out_index), np.nan, dtype=np.float64)
                    )
                    target[rows] = column

        # Step 3: build the DataFrame once, from stacked arrays — the serialize seam ADR-030 §1
        # allows. Column order matches the pre-S7 path: each target's value columns, then the
        # level's metadata columns once (duplicates across targets dropped, as before).
        results = []
        for var_name in selected_vars:
            var_df = pd.DataFrame(stats[var_name], index=out_index)
            if group_meta is not None:
                for meta_col in metadata_cols_to_include:
                    var_df[meta_col] = group_meta[meta_col].astype(object)
            results.append(var_df)

        if not results:
            return pd.DataFrame()

        final_result = pd.concat(results, axis=1)
        final_result = final_result.loc[:, ~final_result.columns.duplicated()]
        return final_result

    def get_subset_dataframe(
        self,
        time_ids: Optional[Union[int, List[int]]] = None,
        features: Optional[Union[str, List[str]]] = None,
        sample_idx: Optional[Union[int, List[int]]] = None,
        entity_ids: Optional[Union[int, str, List[int], List[str]]] = None,
        with_metadata: bool = True,
        level: Optional[str] = None,
        aggregate: bool = False,
    ) -> pd.DataFrame:
        """
        Get a subset of the dataset, with optional geographic aggregation.
        
        When `aggregate=True`, distributions are summed element-wise:
        - Cell A has samples [1, 2, 3]
        - Cell B has samples [4, 5, 6]
        - Aggregated result: [5, 7, 9] (element-wise sum)
        
        This preserves the joint distribution for downstream HDI calculations.
        
        Args:
            time_ids: Time periods to include
            features: Features to include
            sample_idx: Sample indices to include
            entity_ids: Entity codes for filtering (ISO3 strings for country, integers for GAUL)
            with_metadata: Include geographic metadata
            level: Geographic level for filtering/aggregation
            aggregate: If True, aggregate to the specified level
            
        Returns:
            DataFrame with distributions, optionally aggregated
            
        Raises:
            ValueError: If aggregate=True but level is not specified
        """
        if aggregate and level is None:
            raise ValueError("Must specify 'level' when aggregate=True")
        
        if level is not None and entity_ids is not None:
            if isinstance(entity_ids, (str, int)):
                entity_ids = [entity_ids]
            pg_cells = []
            for id in entity_ids:
                pg_cells.extend(self._get_pg_cells(level=level, code=id))
            entity_ids = pg_cells
        result = super().get_subset_dataframe(
            time_ids=time_ids,
            features=features,
            sample_idx=sample_idx,
            entity_ids=entity_ids,
        )
        if with_metadata:
            result = result.join(self.geo_metadata, how="left")
        if aggregate and level is not None:
            result = self._aggregate_distributions(df=result, level=level)
        return result

    def _subset_for_integrity(self, time_ids, features, sample_idx, entity_ids):
        """Exclude the geo-metadata columns from the integrity subset (C-68).

        The base ``check_integrity`` re-wraps the subset in a bare ``_GridDataset``,
        which would classify the 9 GAUL metadata columns (added by this class's
        ``get_subset_dataframe`` when ``with_metadata=True``, the default) as feature
        columns and raise. Restrict the round-trip to the ``pred_*`` columns.
        """
        return self.get_subset_dataframe(
            time_ids, features, sample_idx, entity_ids, with_metadata=False
        )
