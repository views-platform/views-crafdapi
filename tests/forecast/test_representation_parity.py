"""Representation-parity audit for the pandas-to-edges migration (epic #154, S1).

This is the tracer + permanent parity net. It proves the central scope claim of
the epic — that making the `(N,S)` `PredictionFrame` the canonical store, while
keeping the legacy float64 collapse, is **byte-identical** (so the store flip S4
is a behaviour-preserving refactor) — and it characterizes the *only* numeric
change (the float32-native collapse, the gated #112), so the ADR-023 diff is
recorded, not guessed.

Synthetic data only (no real artifact is in the repo; see the un_fao postmortems).
The corpus deliberately includes even/odd sample counts (the median tie-break),
varied sizes, and NaN, because those are the corners a representation flip hits.

Findings recorded in ADR-030:
- Store flip (object-dtype store vs frame store, both under float64 collapse): **byte-identical**.
- Per-cell MAP/HDI float32-vs-float64 delta: **0** (the tower is order-statistic based).
- Aggregation (joint-sum) float32-vs-float64 delta: **~1e-5** — real, sub-count, but
  invisible to the `approx(1e-4)` golden (register C-136). This is the #112 surface.
- Ragged-S: rejected fail-loud (rectangular `(N,S)` required).
"""
import numpy as np
import pandas as pd
import pytest

from tests.conftest import make_fao_df
from views_faoapi.data.handlers import ForecastDataset
from views_faoapi.forecast.serialize import schema
from views_faoapi.forecast.summarize.estimator import collapse, tower_collapse

pytestmark = pytest.mark.layer2_data

# Corner corpus: (n_cells, n_months, n_samples, seed). Even AND odd S on purpose
# (the MAP median tie-break is the one place float32/float64 could diverge).
# Note: make_fao_df caps at n_cells <= 4 (hardcoded coordinate lists).
_CORPUS = [
    (4, 2, 64, 1),    # even S
    (4, 2, 65, 2),    # odd S
    (3, 3, 100, 3),   # even, larger grid
    (4, 1, 257, 4),   # odd, large S
    (2, 2, 2, 5),     # tiny S (degenerate tower)
]


def _input_samples(df: pd.DataFrame, var: str) -> np.ndarray:
    """Independent oracle for the canonical `(N,S)` a dataset must hold for `var`: the
    test's OWN synthetic input, sorted into construction's `(time, entity)` order and
    stacked to float32. Reads neither the object cells nor `_sample_store`, so it stays a
    true cross-check after S4d drops the cells (the test made the data — it knows the
    answer). `make_fao_df` emits a dense grid, so construction only sorts (no dense-fill).
    """
    s = df.sort_index()[var]
    return np.stack([np.asarray(v, dtype=np.float32) for v in s.to_numpy()])


@pytest.mark.parametrize("n_cells,n_months,n_samples,seed", _CORPUS)
class TestStoreFlipIsByteIdentical:
    """The canonical frame store carries the test's input samples in construction order,
    and the float64 collapse is identical from the frame and from a hand-stacked input."""

    def test_frame_store_equals_input_samples(self, n_cells, n_months, n_samples, seed):
        df = make_fao_df(n_cells=n_cells, n_months=n_months, n_samples=n_samples, seed=seed)
        ds = ForecastDataset(df)
        for var in ds.targets:
            expected = _input_samples(df, var)         # independent of the impl
            frame = ds.to_frames()[var].values         # frame store (float32)
            assert frame.dtype == np.float32
            # Same samples, same row order -> the store carries the input faithfully.
            assert np.array_equal(frame, expected), f"{var}: frame store diverges from input"

    def test_float64_collapse_identical_from_frame_and_input(self, n_cells, n_months, n_samples, seed):
        df = make_fao_df(n_cells=n_cells, n_months=n_months, n_samples=n_samples, seed=seed)
        ds = ForecastDataset(df)
        for var in ds.targets:
            input_f64 = _input_samples(df, var).astype(np.float64)
            frame_f64 = ds.to_frames()[var].values.astype(np.float64)
            a_lo, a_hi, a_mp = tower_collapse(input_f64, 0.9)
            b_lo, b_hi, b_mp = tower_collapse(frame_f64, 0.9)
            assert np.array_equal(np.asarray(a_mp), np.asarray(b_mp))
            assert np.array_equal(np.asarray(a_lo), np.asarray(b_lo))
            assert np.array_equal(np.asarray(a_hi), np.asarray(b_hi))


@pytest.mark.parametrize("n_cells,n_months,n_samples,seed", _CORPUS)
class TestFloat32CollapseDelta:
    """Characterize the ONLY numeric change (the gated #112): float32-native collapse."""

    # Recorded in ADR-030. Per-cell MAP/HDI is order-statistic -> exactly 0 here; the
    # bound is generous to remain robust across the corpus, and the test asserts the
    # measured delta stays sub-count (far below a single fatality).
    PER_CELL_BOUND = 1e-3

    def test_per_cell_map_hdi_float32_delta_is_negligible(self, n_cells, n_months, n_samples, seed):
        ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=n_months,
                                         n_samples=n_samples, seed=seed))
        for var in ds.targets:
            f32 = ds.to_frames()[var].values
            f64 = f32.astype(np.float64)
            lo32, hi32, mp32 = (np.asarray(x) for x in tower_collapse(f32, 0.9))
            lo64, hi64, mp64 = (np.asarray(x) for x in tower_collapse(f64, 0.9))
            assert np.max(np.abs(mp32 - mp64)) <= self.PER_CELL_BOUND
            assert np.max(np.abs(lo32 - lo64)) <= self.PER_CELL_BOUND
            assert np.max(np.abs(hi32 - hi64)) <= self.PER_CELL_BOUND


def test_aggregation_joint_sum_float32_delta_is_subcount_but_golden_invisible():
    """The #112 aggregation surface (register C-136): the cross-cell joint-sum is
    arithmetic, so float32 vs float64 differ — by ~1e-5 here. Real, sub-count, and
    BELOW the `approx(1e-4)` golden tolerance, which is why a flip would pass CI
    silently. Recorded so the ADR-023 diff for #112 is evidence-based."""
    rng = np.random.default_rng(0)
    cells = rng.gamma(2.0, 3.0, size=(20, 1000)).astype(np.float32)  # 20 cells x 1000 draws
    summed_f32 = cells.sum(axis=0)
    summed_f64 = cells.astype(np.float64).sum(axis=0)
    _, hi32, mp32 = (np.asarray(x) for x in tower_collapse(summed_f32, 0.9))
    _, hi64, mp64 = (np.asarray(x) for x in tower_collapse(summed_f64, 0.9))
    map_delta = float(abs(mp32[0] - mp64[0]))
    # The drift exists (arithmetic), is sub-count, and would be invisible at 1e-4.
    assert 0.0 < float(np.max(np.abs(summed_f32 - summed_f64)))   # the sum genuinely differs
    assert map_delta < 1e-3                                        # but is sub-count


def test_ragged_S_is_rejected_fail_loud():
    """Ragged sample counts cannot fit a rectangular (N,S) frame. The pipeline must
    reject them loudly, not pad or silently mangle. (ADR-030 ragged-S policy.)"""
    df = make_fao_df(n_cells=3, n_months=1, n_samples=10)
    col = list(df["pred_test"].to_numpy())
    col[0] = np.arange(7, dtype=np.float32)  # one cell with 7 draws, not 10
    df["pred_test"] = pd.Series(col, index=df.index, dtype=object)
    with pytest.raises((ValueError, TypeError)):
        ForecastDataset(df).to_frames()


def _grid_tensor_oracle(ds: ForecastDataset) -> np.ndarray:
    """Independent reference for the dense `(T,E,S,V)` prediction tensor: place each
    present row's canonical samples into the `(time x entity)` grid by its index,
    NaN-filling absent grid cells. Sources samples via `_sample_array` (the canonical
    accessor — object cells pre-S4d, `_sample_store` after) but recomputes the
    reindex/NaN-fill **placement** independently (a dict+loop, vs `_prediction_to_tensor`'s
    pandas positional reindex), so it still catches a placement bug. float64 to match the
    served collapse upcast. (Densification correctness surface — register C-146.)
    """
    full_idx = pd.MultiIndex.from_product(
        [ds._time_values, ds._entity_values], names=[ds._time_id, ds._entity_id]
    )
    T, E, S, V = len(ds._time_values), len(ds._entity_values), ds.sample_size, len(ds.targets)
    tensor = np.full((T, E, S, V), np.nan, dtype=np.float64)
    pos = {idx: i for i, idx in enumerate(ds.dataframe.index)}
    for vi, var in enumerate(ds.targets):
        samples = ds._sample_array(var).astype(np.float64)
        flat = tensor[:, :, :, vi].reshape(T * E, S)
        for g, idx in enumerate(full_idx):
            j = pos.get(idx)
            if j is not None:
                flat[g] = samples[j]
        tensor[:, :, :, vi] = flat.reshape(T, E, S)
    return tensor


def _drop_grid_cells(ds: ForecastDataset, n: int = 1) -> ForecastDataset:
    """Force a NON-dense frame to reach the tensor path: drop `n` interior grid cells
    from the (already-sorted, construction-densified) dataframe. Exercises the
    `_prediction_to_tensor` reindex + NaN-fill branch (register C-146) the dense happy
    path never hits. Drops the SAME rows from `_sample_store` when present (post-S4d), so
    `.dataframe.index` and the sample store stay aligned (the C-155 invariant)."""
    if len(ds.dataframe) > 2 * n:
        keep = np.r_[0:n, 2 * n:len(ds.dataframe)]
        if getattr(ds, "_sample_store", None):
            ds._sample_store = {v: a[keep] for v, a in ds._sample_store.items()}
        ds.dataframe = ds.dataframe.iloc[keep]
    return ds


@pytest.mark.parametrize("n_cells,n_months,n_samples,seed", _CORPUS)
class TestDenseTensorReroute:
    """S4a parity net: the served dense tensor (`_prediction_to_tensor`, the float64
    collapse input) matches an independent grid-placement oracle — under both the dense
    happy path and the sparse reindex/NaN-fill branch (the densification surface, C-146)."""

    def test_dense_tensor_matches_grid_oracle(self, n_cells, n_months, n_samples, seed):
        ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=n_months,
                                         n_samples=n_samples, seed=seed))
        served = np.asarray(ds.to_tensor())
        oracle = _grid_tensor_oracle(ds)
        assert served.shape == oracle.shape
        assert served.dtype == np.float64  # the collapse upcast is preserved (byte-identical)
        assert np.array_equal(served, oracle, equal_nan=True)

    def test_sparse_grid_tensor_matches_grid_oracle(self, n_cells, n_months, n_samples, seed):
        """The reindex/NaN-fill branch: absent grid cells become all-NaN sample vectors,
        carried identically by the served path and the independent placement oracle."""
        ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=n_months,
                                         n_samples=n_samples, seed=seed))
        _drop_grid_cells(ds, n=1)
        # tensor caches on first build; this dataset is freshly mutated, so build clean.
        served = np.asarray(ds._prediction_to_tensor())
        oracle = _grid_tensor_oracle(ds)
        nan_gridcells = int(np.isnan(oracle).all(axis=2).sum())
        assert nan_gridcells > 0, "fixture did not actually create an absent grid cell"
        assert np.array_equal(served, oracle, equal_nan=True)


def test_dense_tensor_carries_cell_level_nan_identically():
    """NaN *within* a present cell (not an absent grid cell) flows through the tensor
    path bit-for-bit — the corner a representation flip must not perturb (C-146)."""
    rows = [[0.0, 1.0, np.nan, 3.0]] * 4
    rows[0] = [np.nan, np.nan, 2.0, 2.0]
    ds = ForecastDataset(_controlled_df(rows))
    served = np.asarray(ds._prediction_to_tensor())
    oracle = _grid_tensor_oracle(ds)
    assert np.array_equal(served, oracle, equal_nan=True)


def _controlled_df(sample_rows):
    """A minimal forecast df whose `pred_test` cells are the given `(S,)` arrays."""
    df = make_fao_df(n_cells=4, n_months=1, n_samples=len(sample_rows[0]))
    df["pred_test"] = pd.Series([np.asarray(r, dtype=np.float32) for r in sample_rows],
                                index=df.index, dtype=object)
    return df


def test_frame_store_carries_nan_identically():
    """NaN cells (a corner a representation flip must not perturb) are carried
    bit-for-bit from the input into the canonical frame store."""
    rows = [[0.0, 1.0, np.nan, 3.0]] * 4
    rows[0] = [np.nan, np.nan, 2.0, 2.0]
    df = _controlled_df(rows)
    ds = ForecastDataset(df)
    expected = _input_samples(df, "pred_test")
    frame = ds.to_frames()["pred_test"].values
    assert np.array_equal(expected, frame, equal_nan=True)


def _object_oracle_aggregated_sums(ds, var, level):
    """Independent oracle for the cross-level joint-sum, sourced from the **object-dtype
    cells** (NOT the frame): for each (time, target_unit), stack the constituent cells'
    `(S,)` arrays in row order and sum in float32 — exactly what the legacy
    `groupby.agg(elementwise_sum)` did. Returns ``{(time, label): (S,) float32}``.

    Independent of the impl, so after S4b reroutes the served sum onto the frame leaf it
    stays a real check (frame-native sum vs object-cell oracle), not a tautology (C-70)."""
    level_col = ds.levels[level]
    raw = ds.get_subset_dataframe(with_metadata=False).join(ds.geo_metadata, how="left")
    time = raw.index.get_level_values(ds._time_id).to_numpy()
    labels = raw[level_col].to_numpy()
    groups = {}
    for i, (t, lab) in enumerate(zip(time, labels)):
        groups.setdefault((int(t), lab), []).append(np.asarray(raw[var].iloc[i], np.float32))
    return {k: np.stack(v).sum(axis=0) for k, v in groups.items()}


@pytest.mark.parametrize("level", ["country", "gaul0", "gaul1", "gaul2"])
def test_served_aggregation_joint_sum_matches_object_oracle(level):
    """S4b parity net: the served aggregated distributions (`get_subset_dataframe`
    `aggregate=True`) are byte-identical to the object-cell joint-sum oracle, at every
    geographic level. Pins the C-70 conservation-correct sum across the representation
    flip — float32 in, float32 out (no #112 re-baseline)."""
    ds = ForecastDataset(make_fao_df(seed=2))
    for var in ds.targets:
        oracle = _object_oracle_aggregated_sums(ds, var, level)
        agg = ds.get_subset_dataframe(aggregate=True, level=level)  # aggregation needs metadata
        served = {(int(t), u): np.asarray(agg.loc[(t, u), var]) for (t, u) in agg.index}
        assert set(served) == set(oracle), f"{level}/{var}: group keys diverge"
        for k in oracle:
            assert served[k].dtype == np.float32
            assert np.array_equal(served[k], oracle[k]), f"{level}/{var}/{k}: sum diverges"


@pytest.mark.parametrize("level", ["country", "gaul0", "gaul1", "gaul2"])
def test_served_aggregated_hdi_map_byte_identical_to_oracle(level):
    """The end-to-end served aggregate collapse (`calculate_hdi_map` aggregate=True) is
    byte-identical to collapsing the object-cell oracle sums — MAP, the three fixed HDIs
    (50/90/95), and severe_scenario — at every level. This is the FAO-facing aggregated number
    (raw sample min/max were dropped in #222/S4)."""
    ds = ForecastDataset(make_fao_df(seed=2))
    served = ds.calculate_hdi_map(aggregate=True, level=level, with_metadata=False)
    for var in ds.targets:
        if f"{var}_map" not in served.columns:
            continue
        assert f"{var}_min" not in served.columns and f"{var}_max" not in served.columns
        oracle = _object_oracle_aggregated_sums(ds, var, level)
        for (t, u), summed in oracle.items():
            cr = collapse(summed, masses=schema.MASSES)
            row = served.loc[(t, u)]
            assert row[f"{var}_map"] == float(cr.map[0])
            assert row[f"{var}_severe_scenario"] == float(cr.severe[0])
            for m in schema.MASSES:
                assert row[schema.hdi_col(var, m, "lower")] == float(cr.lower(m)[0])
                assert row[schema.hdi_col(var, m, "upper")] == float(cr.upper(m)[0])


@pytest.mark.parametrize("n_cells,n_months,n_samples,seed", _CORPUS)
class TestSubsetIsFrameSourced:
    """S4c parity net: the served forecast subset (`get_subset_dataframe`) returns the
    canonical samples sliced by row mask + sample_idx, matching an independent slice of the
    `(N,S)` frame. Pins the reroute that lets S4d drop the object cells."""

    def _oracle(self, ds, var, positions, sample_idx):
        """Independent slice oracle: take the canonical `(N,S)` samples (via the chokepoint
        `_sample_array`) and slice them by row positions + sample_idx by hand."""
        cells = ds._sample_array(var)[positions]
        out = []
        for c in cells:
            a = np.asarray(c)
            out.append(a[sample_idx] if sample_idx is not None else a)
        return out

    @pytest.mark.parametrize("kind", ["all", "sample_list", "sample_scalar"])
    def test_subset_columns_match_object_oracle(self, n_cells, n_months, n_samples, seed, kind):
        ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=n_months,
                                         n_samples=n_samples, seed=seed))
        # sample indices adapt to S so they stay in range across the corpus.
        if kind == "all":
            case, sidx = {}, None
        elif kind == "sample_list":
            picks = sorted({0, n_samples // 2, n_samples - 1})
            case, sidx = {"sample_idx": picks}, picks
        else:
            case, sidx = {"sample_idx": n_samples - 1}, [n_samples - 1]
        sub = ds.get_subset_dataframe(**case)
        positions = np.arange(len(ds.dataframe))  # no row filter in these cases
        assert sub.index.equals(ds.dataframe.index)
        for var in ds.targets:
            oracle = self._oracle(ds, var, positions, sidx)
            served = list(sub[var].to_numpy())
            assert len(served) == len(oracle)
            for s, o in zip(served, oracle):
                assert np.array_equal(np.asarray(s), np.asarray(o), equal_nan=True)

    def test_row_filtered_subset_matches_object_oracle(self, n_cells, n_months, n_samples, seed):
        ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=n_months,
                                         n_samples=n_samples, seed=seed))
        t0 = ds._time_values.tolist()[0]
        sub = ds.get_subset_dataframe(time_ids=t0)
        mask = ds.dataframe.index.get_level_values(ds._time_id) == t0
        positions = np.flatnonzero(mask)
        assert sub.index.equals(ds.dataframe.index[mask])
        for var in ds.targets:
            oracle = self._oracle(ds, var, positions, None)
            for s, o in zip(list(sub[var].to_numpy()), oracle):
                assert np.array_equal(np.asarray(s), np.asarray(o), equal_nan=True)


def test_frame_representation_is_compact_float32():
    """The scalability claim, made deterministic: the frame store is a contiguous
    float32 `(N,S)` block (N*S*4 bytes, no per-cell Python objects) — unlike the
    object-dtype column whose cells are individual numpy arrays (C-148 motivation)."""
    n_cells, n_samples = 4, 256
    ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=2, n_samples=n_samples))
    frame = ds.to_frames()["pred_test"].values
    n_rows = n_cells * 2
    assert frame.dtype == np.float32
    assert frame.flags["C_CONTIGUOUS"]
    assert frame.nbytes == n_rows * n_samples * 4  # exact, no object overhead
    # S4d end-state: the object-dtype sample column is GONE from `.dataframe` (the
    # non-scalable representation is no longer persisted) — samples live in the frame store.
    assert "pred_test" not in ds.dataframe.columns
    assert ds._sample_store["pred_test"].flags["C_CONTIGUOUS"]


@pytest.mark.parametrize("n_cells,n_months,n_samples,seed", _CORPUS)
def test_s4d_end_state_no_object_cells_store_aligned(n_cells, n_months, n_samples, seed):
    """S4d acceptance: a prediction `.dataframe` persists NO object-dtype sample columns
    (no dual store), the `_sample_store` is row-aligned to the index (C-155), and the
    public `samples()` accessor (C-153) returns the canonical `(N,S)` float32 block."""
    ds = ForecastDataset(make_fao_df(n_cells=n_cells, n_months=n_months,
                                     n_samples=n_samples, seed=seed))
    # No sample columns left in .dataframe (the object-dtype representation is dropped).
    assert all(t not in ds.dataframe.columns for t in ds.targets)
    assert not any(ds.dataframe[c].dtype == object for c in ds.dataframe.columns)
    for var in ds.targets:
        block = ds._sample_store[var]
        assert block.dtype == np.float32 and block.shape[0] == len(ds.dataframe)  # C-155
        s = ds.samples(var)                                  # C-153 public accessor
        assert s.shape == (len(ds.dataframe), n_samples)
        assert np.shares_memory(s, block)                    # serves the store, no copy
    with pytest.raises(KeyError):
        ds.samples("not_a_target")
