"""S2 (#204): wire_reader builds a servable ForecastDataset from a manifested run."""

import os
import tempfile

import numpy as np
import pytest

from views_crafdapi.forecast.ingestion import wire_reader

from ._wire_fixtures import make_wire_run

pytestmark = pytest.mark.layer2_data


@pytest.fixture
def run():
    return make_wire_run()


def _build(run):
    manifest = wire_reader.parse_run_manifest(run.manifest)
    state = wire_reader.load_shard_state(run.shard_bytes)
    sidecar = wire_reader.read_sidecar(run.sidecar_bytes)
    return wire_reader.build_dataset([state], sidecar, manifest)


def test_parse_run_manifest(run):
    m = wire_reader.parse_run_manifest(run.manifest)
    assert m.run_id == run.run_id
    assert m.targets == [run.target]
    assert m.shard_names == [run.shard_name]
    assert m.sidecar_name == run.sidecar_name
    assert m.expected_cell_count == len(run.units)


def test_load_shard_state_roundtrips_values(run):
    state = wire_reader.load_shard_state(run.shard_bytes)
    assert state["values"].shape == (len(run.units), run.sample_count)
    assert np.allclose(state["values"], run.values)
    assert state["metadata"]["sample_count"] == run.sample_count
    assert list(np.asarray(state["unit"])) == list(run.units)


def test_build_dataset_populates_sample_store(run):
    ds = _build(run)
    assert ds.is_prediction
    # one target, pred_-prefixed; (N, S) store aligned to the index
    (var,) = ds.targets
    assert var == f"pred_{run.target}"
    block = ds._sample_store[var]
    assert block.shape == (len(run.units), run.sample_count)
    assert block.dtype == np.float32


def test_build_dataset_preserves_nan_geography(run):
    ds = _build(run)
    # the last unit has missing GAUL — the row must survive ingest (dropped only at aggregation)
    assert len(ds.geo_metadata) == len(run.units)
    last_unit = run.units[-1]
    iso = ds.geo_metadata.xs(last_unit, level="priogrid_id")["country_iso_a3"]
    assert iso.isna().all()


def test_served_draws_match_source(run):
    """The whole point: draws for a known cell/time come back matching what was ingested."""
    ds = _build(run)
    (var,) = ds.targets
    samples = ds.samples(var)  # (N, S) in index order
    idx = ds.dataframe.index.get_level_values("priogrid_id").tolist()
    row = idx.index(run.units[1])  # cell 100002 (non-degenerate)
    assert np.allclose(samples[row], run.values[1])


def _build_multi(mrun):
    manifest = wire_reader.parse_run_manifest(mrun.manifest)
    states = [wire_reader.load_shard_state(mrun.shard_bytes[s["name"]]) for s in manifest.shards]
    sidecar = wire_reader.read_sidecar(mrun.sidecar_bytes)
    return wire_reader.build_dataset(states, sidecar, manifest)


def test_multi_shard_assembly_serves_all_targets_and_months():
    """S4 (#206): all (target, month) shards assemble into one dataset; draws for a known
    (target, month, cell) come back matching what was ingested."""
    from ._wire_fixtures import make_multi_shard_run

    mrun = make_multi_shard_run()
    ds = _build_multi(mrun)

    assert set(ds.targets) == {f"pred_{t}" for t in mrun.targets}
    total_rows = len(mrun.months) * len(mrun.units)
    for var in ds.targets:
        assert ds._sample_store[var].shape == (total_rows, mrun.sample_count)

    # Verify a known draw for EVERY target at a non-first month/cell — a target/column
    # transposition (target A's draws served under pred_B) must not slip through.
    month, cell = mrun.months[1], int(mrun.units[1])
    row = list(ds.dataframe.index).index((month, cell))
    for target in mrun.targets:
        assert np.allclose(
            ds.samples(f"pred_{target}")[row], mrun.values[(target, month, cell)]
        ), f"draws for {target} at ({month}, {cell}) do not match the ingested shard"


def test_shard_count_mismatch_raises():
    """S4 defense: fewer/more loaded shards than the manifest lists must be refused, not
    silently truncated by the positional zip."""
    from ._wire_fixtures import make_multi_shard_run

    mrun = make_multi_shard_run()
    manifest = wire_reader.parse_run_manifest(mrun.manifest)
    states = [wire_reader.load_shard_state(mrun.shard_bytes[s["name"]]) for s in manifest.shards]
    sidecar = wire_reader.read_sidecar(mrun.sidecar_bytes)
    with pytest.raises(ValueError, match="manifest lists"):
        wire_reader.build_dataset(states[:-1], sidecar, manifest)  # drop one loaded shard


def test_duplicate_target_month_shard_raises():
    """S4 defense: a manifest listing the same (target, month) twice must be refused, not
    silently last-wins overwritten."""
    from ._wire_fixtures import make_multi_shard_run

    mrun = make_multi_shard_run()
    manifest = wire_reader.parse_run_manifest(mrun.manifest)
    states = [wire_reader.load_shard_state(mrun.shard_bytes[s["name"]]) for s in manifest.shards]
    # Force the second loaded shard to duplicate the first's (target, month) coordinates.
    dup = dict(states[0])
    manifest.shards[1] = dict(manifest.shards[1], target=manifest.shards[0]["target"],
                              time_id=manifest.shards[0]["time_id"])
    states[1] = dup
    sidecar = wire_reader.read_sidecar(mrun.sidecar_bytes)
    with pytest.raises(ValueError, match="duplicate"):
        wire_reader.build_dataset(states, sidecar, manifest)


def test_cross_shard_sample_count_mismatch_raises():
    """§4.5(a) cross-shard: all shards in a run must share S."""
    from ._wire_fixtures import make_multi_shard_run

    mrun = make_multi_shard_run(sample_count=4)
    manifest = wire_reader.parse_run_manifest(mrun.manifest)
    states = [wire_reader.load_shard_state(mrun.shard_bytes[s["name"]]) for s in manifest.shards]
    states[1] = dict(states[1])
    states[1]["values"] = np.ones((len(mrun.units), 5), dtype=np.float32)  # S=5, others S=4
    sidecar = wire_reader.read_sidecar(mrun.sidecar_bytes)
    with pytest.raises(ValueError, match="sample_count"):
        wire_reader.build_dataset(states, sidecar, manifest)


def test_estimate_assembled_bytes():
    # 3 targets × 36 months × 64742 cells × 1024 samples × 4 bytes ≈ 28.6 GB (the ADR figure)
    est = wire_reader.estimate_assembled_bytes(3, 36, 64742, 1024)
    assert 28e9 < est < 29e9


def test_target_raggedness_zero_filled():
    """A (month,cell) covered by one target but not another → the absent target is zero-filled
    (dense-grid fill handles missing rows; missing columns within a row are the assembler's job)."""
    from views_frames.io import arrow

    from ._wire_fixtures import _build_sidecar, _sha256

    s, month = 4, 543
    units_a = np.array([100001, 100002, 100003], dtype=np.int64)
    units_b = np.array([100004, 100005, 100006], dtype=np.int64)  # disjoint cell sets, same N
    all_units = np.arange(100001, 100007, dtype=np.int64)

    def shard(target, units_):
        vals = np.ones((len(units_), s), dtype=np.float32) * (7.0 if target == "lr_ged_sb" else 9.0)
        header = {"contract_version": "1.5", "sample_count": s, "target": target, "time_id": month,
                  "id_semantics": {"time": "views_month_id", "unit": "priogrid_id"}}
        p = tempfile.mktemp(suffix=".parquet")
        arrow.save(p, values=vals, time=np.full(len(units_), month, dtype=np.int64), unit=units_, level="pgm", metadata=header)
        b = open(p, "rb").read()
        os.unlink(p)
        return b

    b_a, b_b = shard("lr_ged_sb", units_a), shard("lr_ged_ns", units_b)
    manifest = wire_reader.parse_run_manifest({
        "run_id": "r", "targets": ["lr_ged_sb", "lr_ged_ns"], "expected_months": [month], "expected_cell_count": 3,
        "shards": [
            {"name": "a", "target": "lr_ged_sb", "time_id": month, "sha256": _sha256(b_a)},
            {"name": "b", "target": "lr_ged_ns", "time_id": month, "sha256": _sha256(b_b)},
        ],
        "sidecar": {"name": "sc", "sha256": ""},
    })
    _, sc_bytes = _build_sidecar(all_units, "r", with_nan_geo=False)
    states = [wire_reader.load_shard_state(b_a), wire_reader.load_shard_state(b_b)]
    ds = wire_reader.build_dataset(states, wire_reader.read_sidecar(sc_bytes), manifest)
    idx = list(ds.dataframe.index)
    # cell 100004 is covered by target ns but NOT sb → sb zero-filled there
    row = idx.index((month, 100004))
    assert np.allclose(ds.samples("pred_lr_ged_sb")[row], 0.0)
    assert np.allclose(ds.samples("pred_lr_ged_ns")[row], 9.0)


def test_permuted_sample_ordering_is_rejected(run):
    """§4.5(b): a shard whose `sample` column is not tile(arange(S), N) must be refused,
    since arrow.load discards it and reshapes positionally (silent mis-slotting otherwise)."""
    import io as _io

    import pyarrow as pa
    import pyarrow.parquet as pq

    # Rewrite the shard with a permuted sample column (swap draws 0 and 1 in cell 0).
    t = pq.read_table(_io.BytesIO(run.shard_bytes))
    sample = t.column("sample").to_numpy().copy()
    sample[0], sample[1] = sample[1], sample[0]
    t2 = t.set_column(t.schema.get_field_index("sample"), "sample", pa.array(sample))
    buf = _io.BytesIO()
    pq.write_table(t2.replace_schema_metadata(t.schema.metadata), buf)
    with pytest.raises(ValueError, match="sample"):
        wire_reader.load_shard_state(buf.getvalue())


def test_sidecar_not_covering_shard_raises(run):
    """§5.2 (S3): a shard cell absent from the sidecar is refused (silent-cell-loss guard)."""
    manifest = wire_reader.parse_run_manifest(run.manifest)
    state = wire_reader.load_shard_state(run.shard_bytes)
    sidecar = wire_reader.read_sidecar(run.sidecar_bytes).iloc[:-1]  # drop the last cell
    with pytest.raises(ValueError, match="does not cover"):
        wire_reader.build_dataset([state], sidecar, manifest)


def test_incomplete_cell_count_raises(run):
    """§5.2/§11.2 (S3): loaded N != manifest expected_cell_count is refused (truncated map)."""
    manifest = wire_reader.parse_run_manifest(run.manifest)
    manifest.expected_cell_count = len(run.units) + 5  # manifest expects more than the shard has
    state = wire_reader.load_shard_state(run.shard_bytes)
    sidecar = wire_reader.read_sidecar(run.sidecar_bytes)
    with pytest.raises(ValueError, match="expects"):
        wire_reader.build_dataset([state], sidecar, manifest)


def test_header_target_mismatch_raises(run):
    """§4.5 (S3): the shard header target must equal the manifest's per-shard target."""
    manifest = wire_reader.parse_run_manifest(run.manifest)
    manifest.shards[0]["target"] = "lr_ged_ns"  # manifest disagrees with the shard header (lr_ged_sb)
    state = wire_reader.load_shard_state(run.shard_bytes)
    sidecar = wire_reader.read_sidecar(run.sidecar_bytes)
    with pytest.raises(ValueError, match="target"):
        wire_reader.build_dataset([state], sidecar, manifest)


def test_sample_count_header_mismatch_raises():
    """§4.5(a): a shard whose loaded S disagrees with its header sample_count is refused."""
    from views_frames.io import arrow

    n, s = 6, 4
    values = np.ones((n, s), dtype=np.float32)
    time = np.full(n, 543, dtype=np.int64)
    unit = np.arange(100001, 100001 + n, dtype=np.int64)
    header = {"contract_version": "1.5", "sample_count": 99, "target": "lr_ged_sb", "time_id": 543,
              "id_semantics": {"time": "views_month_id", "unit": "priogrid_id"}}  # sample_count LIES
    p = tempfile.mktemp(suffix=".parquet")
    arrow.save(p, values=values, time=time, unit=unit, level="pgm", metadata=header)
    data = open(p, "rb").read()
    os.unlink(p)
    with pytest.raises(ValueError, match="sample_count"):
        wire_reader.load_shard_state(data)


def test_verify_content_hash():
    good = b"hello"
    import hashlib

    wire_reader.verify_content_hash(good, hashlib.sha256(good).hexdigest(), "x")  # ok
    with pytest.raises(ValueError, match="content hash"):
        wire_reader.verify_content_hash(good, "0" * 64, "x")
    wire_reader.verify_content_hash(good, None, "x")  # None → warn, no raise
