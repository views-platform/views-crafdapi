"""S6b-2 (#208): the per-month WireRunAssembler produces a value-dir byte-identical (at the
served-array level) to the whole-run `build_dataset` → to_value path — the gate that must be
green before the ingest path is rewired to stream-assemble to disk."""
from collections import defaultdict

import numpy as np
import pytest

from views_faoapi.data.handlers import ForecastDataset
from views_faoapi.forecast.ingestion import wire_reader

from ._wire_fixtures import make_multi_shard_run

pytestmark = pytest.mark.layer2_data


def _states_for(mrun, shards):
    return [wire_reader.load_shard_state(mrun.shard_bytes[s["name"]]) for s in shards]


def _whole_run_ref(mrun, tmp_path):
    """The oracle: whole-run build_dataset → to_value → from_value(mmap)."""
    manifest = wire_reader.parse_run_manifest(mrun.manifest)
    sidecar = wire_reader.read_sidecar(mrun.sidecar_bytes)
    states = _states_for(mrun, manifest.shards)
    ref = wire_reader.build_dataset(states, sidecar, manifest)
    ref.to_value(tmp_path / "ref")
    return ForecastDataset.from_value(tmp_path / "ref", mmap=True)


def _assembled(mrun, tmp_path):
    """The candidate: per-month WireRunAssembler → from_value(mmap)."""
    manifest = wire_reader.parse_run_manifest(mrun.manifest)
    sidecar = wire_reader.read_sidecar(mrun.sidecar_bytes)
    asm = wire_reader.WireRunAssembler(tmp_path / "asm", manifest, sidecar)
    by_month = defaultdict(list)
    for s in manifest.shards:
        by_month[s["time_id"]].append(s)
    for month in sorted(by_month):
        mshards = by_month[month]
        asm.append_month(_states_for(mrun, mshards), mshards, month)
    n = asm.finalize()
    assert n == len(manifest.expected_months) * manifest.expected_cell_count
    return ForecastDataset.from_value(tmp_path / "asm", mmap=True)


@pytest.mark.parametrize("with_nan_geo", [False, True])
def test_assembler_byte_identical_to_whole_run(with_nan_geo, tmp_path):
    mrun = make_multi_shard_run(run_id="run_a", with_nan_geo=with_nan_geo)
    ref = _whole_run_ref(mrun, tmp_path)
    cand = _assembled(mrun, tmp_path)

    assert list(cand.targets) == list(ref.targets)
    assert cand.dataframe.index.equals(ref.dataframe.index)
    for var in ref.targets:
        assert isinstance(cand._sample_store[var], np.memmap)  # paged, as S6b-1 serves it
        assert np.array_equal(cand._sample_store[var], ref._sample_store[var], equal_nan=True), var
    assert cand.geo_metadata.equals(ref.geo_metadata)  # incl. NaN GAUL rows
    # The real contract: served HDI/MAP identical across cell-level + aggregate levels.
    for kw in ({}, {"aggregate": True, "level": "country"}, {"aggregate": True, "level": "gaul2"}):
        assert ref.calculate_hdi_map(**kw).equals(cand.calculate_hdi_map(**kw)), kw


def test_assembler_refuses_non_rectangular_run(tmp_path):
    """A run whose months don't share a cell set must be refused (preserves C-87), not silently
    assembled into a torn store."""
    mrun = make_multi_shard_run(run_id="run_r")
    manifest = wire_reader.parse_run_manifest(mrun.manifest)
    sidecar = wire_reader.read_sidecar(mrun.sidecar_bytes)
    by_month = defaultdict(list)
    for s in manifest.shards:
        by_month[s["time_id"]].append(s)
    months = sorted(by_month)
    assert len(months) >= 2

    asm = wire_reader.WireRunAssembler(tmp_path / "asm", manifest, sidecar)
    asm.append_month(_states_for(mrun, by_month[months[0]]), by_month[months[0]], months[0])
    # Corrupt the second month's states to a different cell set (shift the unit ids).
    bad_states = _states_for(mrun, by_month[months[1]])
    for st in bad_states:
        st["unit"] = np.asarray(st["unit"]) + 10_000
    with pytest.raises(ValueError, match="non-rectangular|cell set|does not cover"):
        asm.append_month(bad_states, by_month[months[1]], months[1])


def test_assembler_refuses_target_absent_from_a_month(tmp_path):
    """S6b-2 review #1: a target present in the run but MISSING from some month must be refused
    (fail-safe legacy), not silently dropped from served output."""
    mrun = make_multi_shard_run(run_id="run_t")
    manifest = wire_reader.parse_run_manifest(mrun.manifest)
    sidecar = wire_reader.read_sidecar(mrun.sidecar_bytes)
    assert len(manifest.targets) >= 2
    by_month = defaultdict(list)
    for s in manifest.shards:
        by_month[s["time_id"]].append(s)
    m0 = sorted(by_month)[0]
    dropped = by_month[m0][0]["target"]
    kept = [s for s in by_month[m0] if s["target"] != dropped]  # m0 now missing `dropped`
    assert kept and len(kept) < len(by_month[m0])

    asm = wire_reader.WireRunAssembler(tmp_path / "asm", manifest, sidecar)
    with pytest.raises(ValueError, match="every target must appear|targets"):
        asm.append_month(_states_for(mrun, kept), kept, m0)


def test_assembler_column_order_tracks_whole_run_not_month_one(tmp_path):
    """S6b-2 review #2: target column order follows the whole-run global first-appearance (as
    build_dataset does), NOT month 1's — so a shuffled shard list stays byte-identical."""
    import copy

    mrun = make_multi_shard_run(run_id="run_o")
    shards = list(mrun.manifest["shards"])
    # Move a later-month shard to the front so global first-appearance != month-1 order.
    crafted = [shards[-1]] + shards[:-1]
    mrun2 = copy.copy(mrun)
    mrun2.manifest = {**mrun.manifest, "shards": crafted}

    ref = _whole_run_ref(mrun2, tmp_path)
    cand = _assembled(mrun2, tmp_path)
    assert list(cand.targets) == list(ref.targets)  # same column order as the whole-run oracle
    assert list(cand.targets) == ["pred_lr_ged_ns", "pred_lr_ged_sb"]  # the crafted global order
    for var in ref.targets:
        assert np.array_equal(cand._sample_store[var], ref._sample_store[var], equal_nan=True), var
    assert ref.calculate_hdi_map().equals(cand.calculate_hdi_map())


def test_staging_dir_unique_and_discardable(tmp_path):
    """S6b-2 review (resource): staging dirs are unique per call (no collision with write()'s tmp
    or a concurrent ingest) and fully removable."""
    from views_faoapi.managers.disk_cache import FAODiskCacheManager

    dc = FAODiskCacheManager(tmp_path)
    a = dc.staging_dir("h", "forecast")
    b = dc.staging_dir("h", "forecast")
    assert a != b and a.exists() and b.exists()
    assert a.name.endswith(".tmp") and b.name.endswith(".tmp")
    dc.discard_staging(a)
    dc.discard_staging(b)
    dc.discard_staging(a)  # idempotent (no error if already gone)
    assert not a.exists() and not b.exists()
