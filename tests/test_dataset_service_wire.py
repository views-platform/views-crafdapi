"""S2 (#204): DatasetService serves a manifested wire-contract run end to end,
and falls back to the legacy path (no regression) when no manifest exists."""

import io
import json
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from tests.conftest import make_fao_df
from tests.forecast._wire_fixtures import make_wire_run
from views_crafdapi.managers.dataset_service import DatasetService
from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager
from views_crafdapi.managers.prediction import SHARD_ARTIFACT_TYPE, SIDECAR_ARTIFACT_TYPE

pytestmark = pytest.mark.layer4_infra

HASH = "deadbeef00000000"


def _service(cache_dir=None):
    # S6b-2 (#208): wire ingest streams to disk and reads it back, so it needs a REAL disk cache
    # (production always has one). A per-test temp dir stands in for the mock disk used pre-S6b-2.
    disk = CrafdDiskCacheManager(Path(cache_dir) if cache_dir else Path(tempfile.mkdtemp()))
    return DatasetService(
        dataframe_cache={},
        file_cache={},
        disk_cache=disk,
        prediction_bucket_id="test-bucket",
        configs_getter=lambda: {},
        api_key_hash_fn=lambda _k: HASH,
        check_staleness_fn=lambda _ts: types.SimpleNamespace(
            is_stale=False, age_hours=0.0, threshold_hours=24.0
        ),
    )


def _wire_manager(run):
    """A store manager returning the run's manifest/shard/sidecar docs + bytes."""
    mgr = MagicMock()
    mgr.get_latest_manifest.return_value = run.manifest_doc

    def resolve(names, artifact_type):
        if artifact_type == SHARD_ARTIFACT_TYPE:
            return {run.shard_name: "shard_1"}
        if artifact_type == SIDECAR_ARTIFACT_TYPE:
            return {run.sidecar_name: "sidecar_1"}
        return {}

    mgr.resolve_artifact_file_ids.side_effect = resolve
    bytes_by_id = {
        "mani_1": json.dumps(run.manifest).encode(),
        "shard_1": run.shard_bytes,
        "sidecar_1": run.sidecar_bytes,
    }

    def download(file_id, *a, **k):
        return MagicMock(success=True, data={"file_bytes": bytes_by_id[file_id]})

    mgr.download_prediction.side_effect = download
    return mgr


def test_serves_manifested_wire_run():
    run = make_wire_run()
    svc = _service()
    mgr = _wire_manager(run)

    out = svc.get_latest_dataframe(mgr, "key", "forecast")

    assert len(out) == len(run.units)  # served frame has one row per cell
    mgr.get_latest_manifest.assert_called_once()
    # the wire path never touches the legacy provenance selector
    mgr.get_latest_provenance.assert_not_called()

    ds = svc._dataframe_cache[HASH]["forecast"]["dataset"]
    (var,) = ds.targets
    assert ds.samples(var).shape == (len(run.units), run.sample_count)
    # draws for a known non-degenerate cell match what was ingested
    import numpy as np

    idx = ds.dataframe.index.get_level_values("priogrid_id").tolist()
    row = idx.index(run.units[1])
    assert np.allclose(ds.samples(var)[row], run.values[1])


def test_wire_run_provenance_recorded():
    run = make_wire_run()
    svc = _service()
    svc.get_latest_dataframe(_wire_manager(run), "key", "forecast")
    prov = svc._dataframe_cache[HASH]["forecast"]["provenance"]
    assert prov["run_id"] == run.run_id
    assert prov["source"] == "fixture_ensemble"


def test_forecast_no_manifest_fails_visible_but_historical_still_legacy():
    """S1 (#264): a **forecast** with no manifest and no last-good manifested run fails visible
    (503) — never a loose legacy artifact. `/historical` with no manifest still serves legacy
    (loose files, unchanged)."""
    df = make_fao_df()
    buf = io.BytesIO()
    df.to_parquet(buf)

    svc = _service()
    mgr = MagicMock()
    mgr.get_latest_manifest.return_value = None  # no manifested run
    prov = MagicMock(
        file_id="legacy_1", source="datafactory", methodology_version="v1",
        file_hash="h", created_at="2026", filename="legacy.parquet",
    )
    prov.to_dict.return_value = {"file_id": "legacy_1", "source": "datafactory"}
    mgr.get_latest_provenance.return_value = prov
    mgr.download_prediction.return_value = MagicMock(success=True, data={"file_bytes": buf.getvalue()})

    # forecast: no manifest, no last-good ⇒ fail visible; the legacy selector is NOT consulted.
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()

    # historical: still falls back to legacy, unchanged.
    out = svc.get_latest_dataframe(mgr, "key", "historical")
    assert len(out) == len(df)
    mgr.get_latest_provenance.assert_called_once()


def _legacy_manager_with_manifest(run, *, manifest_bytes=None):
    """A manager that returns a manifest whose run cannot be served (e.g. a malformed/corrupt
    manifest) AND has a working legacy path — to prove fail-safe fallback."""
    df = make_fao_df()
    buf = io.BytesIO()
    df.to_parquet(buf)
    mgr = MagicMock()
    mgr.get_latest_manifest.return_value = run.manifest_doc

    manifest = dict(run.manifest)

    def resolve(names, artifact_type):
        if artifact_type == SHARD_ARTIFACT_TYPE:
            return {n: f"shard_{i}" for i, n in enumerate(names)}
        if artifact_type == SIDECAR_ARTIFACT_TYPE:
            return {run.sidecar_name: "sidecar_1"}
        return {}

    mgr.resolve_artifact_file_ids.side_effect = resolve
    bytes_by_id = {
        "mani_1": manifest_bytes if manifest_bytes is not None else json.dumps(manifest).encode(),
        "shard_0": run.shard_bytes, "shard_1": run.shard_bytes, "sidecar_1": run.sidecar_bytes,
    }
    mgr.download_prediction.side_effect = lambda fid, *a, **k: MagicMock(
        success=True, data={"file_bytes": bytes_by_id.get(fid, run.shard_bytes)}
    )
    # legacy fallback path
    prov = MagicMock(file_id="legacy_1", source="datafactory", methodology_version="v1",
                     file_hash="h", created_at="2026", filename="legacy.parquet")
    prov.to_dict.return_value = {"file_id": "legacy_1"}
    mgr.get_latest_provenance.return_value = prov
    mgr._legacy_bytes = buf.getvalue()
    # legacy download is the SAME download_prediction; route legacy file_id to the parquet
    bytes_by_id["legacy_1"] = buf.getvalue()
    return mgr, len(df)


def _multi_wire_manager(mrun):
    """A store manager serving a full multi-(target,month) run's manifest/shards/sidecar."""
    mgr = MagicMock()
    mgr.get_latest_manifest.return_value = mrun.manifest_doc
    name_to_id = {d["filename"]: d["fileId"] for d in mrun.shard_docs}

    def resolve(names, artifact_type):
        if artifact_type == SHARD_ARTIFACT_TYPE:
            return {n: name_to_id[n] for n in names if n in name_to_id}
        if artifact_type == SIDECAR_ARTIFACT_TYPE:
            return {mrun.sidecar_name: "sidecar_1"}
        return {}

    mgr.resolve_artifact_file_ids.side_effect = resolve
    bytes_by_id = {"mani_1": json.dumps(mrun.manifest).encode(), "sidecar_1": mrun.sidecar_bytes}
    for d in mrun.shard_docs:
        bytes_by_id[d["fileId"]] = mrun.shard_bytes[d["filename"]]
    mgr.download_prediction.side_effect = lambda fid, *a, **k: MagicMock(
        success=True, data={"file_bytes": bytes_by_id[fid]}
    )
    return mgr


def test_multishard_run_serves():
    """S4: a full multi-(target, month) manifested run assembles and serves (no fallback)."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    mrun = make_multi_shard_run()
    svc = _service()
    mgr = _multi_wire_manager(mrun)
    out = svc.get_latest_dataframe(mgr, "key", "forecast")

    mgr.get_latest_provenance.assert_not_called()  # served the wire run, did NOT fall back
    ds = svc._dataframe_cache[HASH]["forecast"]["dataset"]
    assert set(ds.targets) == {f"pred_{t}" for t in mrun.targets}
    assert len(out) == len(mrun.months) * len(mrun.units)


# S2 (#247, ADR-033 §2): a manifested run that is present but UNSERVABLE fails visible (503) —
# it is NOT silently served from the legacy artifact (the C-170 fix). `get_latest_provenance`
# (the legacy selector) is never touched.
def test_over_capacity_run_fails_visible_not_legacy(monkeypatch):
    """§4.6: a run over the capacity bound is Refused → 503, not a silent legacy serve."""
    monkeypatch.setenv("FAOAPI_MAX_ASSEMBLED_BYTES", "1")  # 1 byte — any real run exceeds it
    run = make_wire_run()
    mgr, _ = _legacy_manager_with_manifest(run)
    with pytest.raises(HTTPException) as e:
        _service().get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()  # did NOT fall back to legacy


def test_malformed_manifest_fails_visible_not_legacy():
    """A malformed manifest is Refused → a clean 503 (not an unhandled 500, not a legacy serve)."""
    run = make_wire_run()
    mgr, _ = _legacy_manager_with_manifest(run, manifest_bytes=b"{ this is not json")
    with pytest.raises(HTTPException) as e:
        _service().get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()


def test_bad_shard_hash_fails_visible_not_legacy():
    """§4.5(c): a shard whose bytes don't match the manifest sha256 → Refused → 503, not legacy."""
    run = make_wire_run()
    manifest = dict(run.manifest)
    manifest["shards"] = [dict(manifest["shards"][0], sha256="0" * 64)]  # wrong hash
    mgr, _ = _legacy_manager_with_manifest(run, manifest_bytes=json.dumps(manifest).encode())
    with pytest.raises(HTTPException) as e:
        _service().get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()


def test_incomplete_run_fails_visible_not_legacy():
    """§5.2: a run whose shard has fewer cells than the manifest declares → Refused → 503, not legacy."""
    run = make_wire_run()
    manifest = dict(run.manifest)
    manifest["expected_cell_count"] = len(run.units) + 3  # manifest over-declares
    mgr, _ = _legacy_manager_with_manifest(run, manifest_bytes=json.dumps(manifest).encode())
    with pytest.raises(HTTPException) as e:
        _service().get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()


def test_unsupported_contract_version_fails_visible_not_legacy():
    """S5 (#250, ADR-033 §7, C-171): a run declaring a wire-contract dialect this build cannot
    render is Refused (schema_capability_mismatch) → 503, BEFORE any shard is fetched — never a
    silent degraded serve, never a legacy fallback."""
    run = make_wire_run()
    manifest = dict(run.manifest)
    manifest["contract_version"] = "2.0"  # a future major this build does not implement
    mgr, _ = _legacy_manager_with_manifest(run, manifest_bytes=json.dumps(manifest).encode())
    with pytest.raises(HTTPException) as e:
        _service().get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()      # did NOT fall back to legacy
    mgr.resolve_artifact_file_ids.assert_not_called()  # refused before fetching shards/sidecar


def test_historical_never_takes_the_wire_path():
    """Only forecasts are manifest-first; historical is unconditionally legacy."""
    df = make_fao_df()
    buf = io.BytesIO()
    df.to_parquet(buf)

    svc = _service()
    mgr = MagicMock()
    prov = MagicMock(
        file_id="h1", source="s", methodology_version="v1", file_hash="h",
        created_at="2026", filename="h.parquet",
    )
    prov.to_dict.return_value = {"file_id": "h1"}
    mgr.get_latest_provenance.return_value = prov
    mgr.download_prediction.return_value = MagicMock(success=True, data={"file_bytes": buf.getvalue()})

    svc.get_latest_dataframe(mgr, "key", "historical")
    mgr.get_latest_manifest.assert_not_called()  # historical never checks for a manifest


# --- S5 (#207): cache re-key on the run-manifest fileId -----------------------------------------

def _rekey_manager(runs_by_fileid, *, with_legacy=False):
    """A store manager that serves whichever manifest ``state['current']`` names — a fileId key
    of ``runs_by_fileid`` (a `MultiShardRun`), ``None`` (no manifest), or the string ``"RAISE"``
    (a lookup blip). Flip ``state['current']`` to simulate a new run, a rollback to a previous
    run, a quarantine-to-nothing, or an Appwrite blip. Optionally also serves a legacy fallback."""
    from tests.conftest import make_fao_df

    state = {"current": None}
    mgr = MagicMock()

    def latest_manifest():
        cur = state["current"]
        if cur == "RAISE":
            raise RuntimeError("appwrite blip")
        if cur is None:
            return None
        run = runs_by_fileid[cur]
        return dict(run.manifest_doc, fileId=cur)  # patch the fileId to this run's identity

    mgr.get_latest_manifest.side_effect = latest_manifest

    def resolve(names, artifact_type):
        out = {}
        for fid, run in runs_by_fileid.items():
            if artifact_type == SHARD_ARTIFACT_TYPE:
                for d in run.shard_docs:
                    if d["filename"] in names:
                        out[d["filename"]] = f"{fid}::{d['fileId']}"
            elif artifact_type == SIDECAR_ARTIFACT_TYPE and run.sidecar_name in names:
                out[run.sidecar_name] = f"{fid}::sidecar"
        return out

    mgr.resolve_artifact_file_ids.side_effect = resolve

    bytes_by_id = {}
    for fid, run in runs_by_fileid.items():
        bytes_by_id[fid] = json.dumps(run.manifest).encode()
        bytes_by_id[f"{fid}::sidecar"] = run.sidecar_bytes
        for d in run.shard_docs:
            bytes_by_id[f"{fid}::{d['fileId']}"] = run.shard_bytes[d["filename"]]

    legacy_rows = 0
    if with_legacy:
        df = make_fao_df()
        buf = io.BytesIO()
        df.to_parquet(buf)
        bytes_by_id["legacy_1"] = buf.getvalue()
        legacy_rows = len(df)
        prov = MagicMock(file_id="legacy_1", source="datafactory", methodology_version="v1",
                         file_hash="h", created_at="2026", filename="legacy.parquet")
        prov.to_dict.return_value = {"file_id": "legacy_1", "source": "datafactory"}
        mgr.get_latest_provenance.return_value = prov

    mgr.download_prediction.side_effect = lambda fid, *a, **k: MagicMock(
        success=True, data={"file_bytes": bytes_by_id[fid]}
    )
    return mgr, state, legacy_rows


def _prov_run_id(svc):
    return svc._dataframe_cache[HASH]["forecast"]["provenance"]["run_id"]


def test_new_manifest_invalidates_warm_wire_and_serves_new_run():
    """S5: a newer run (different manifest fileId) invalidates the warm wire entry and serves
    the new run on the very next request — not up to a TTL late."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    run_b = make_multi_shard_run(run_id="run_b")
    mgr, state, _ = _rekey_manager({"mani_a": run_a, "mani_b": run_b})
    svc = _service()

    state["current"] = "mani_a"
    svc.get_latest_dataframe(mgr, "key", "forecast")
    assert _prov_run_id(svc) == "run_a"

    state["current"] = "mani_b"  # a new run is uploaded (newest manifest wins)
    svc.get_latest_dataframe(mgr, "key", "forecast")
    assert _prov_run_id(svc) == "run_b"  # re-keyed, not the stale warm run_a


def test_same_manifest_serves_warm_without_redownload():
    """S5: an unchanged manifest is a warm hit — the run is NOT re-ingested (no shard download,
    no legacy provenance), only the cheap manifest lookup runs."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a})
    svc = _service()

    state["current"] = "mani_a"
    svc.get_latest_dataframe(mgr, "key", "forecast")  # cold: ingests + warms
    downloads_after_first = mgr.download_prediction.call_count
    manifest_calls_after_first = mgr.get_latest_manifest.call_count

    svc.get_latest_dataframe(mgr, "key", "forecast")  # warm: same manifest
    assert mgr.download_prediction.call_count == downloads_after_first  # no re-download
    assert mgr.get_latest_manifest.call_count == manifest_calls_after_first + 1  # re-key checked
    mgr.get_latest_provenance.assert_not_called()  # never fell back to legacy


def test_manifest_quarantine_rolls_back_to_previous_run():
    """S5 / ADR-013 §4.4: quarantining the live run's manifest makes get_latest_manifest return
    the PREVIOUS run's manifest (a different fileId) — the cache re-keys to it (run-level
    rollback) on the next request. Operators act on the manifest, never the shards."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    prev = make_multi_shard_run(run_id="run_prev")
    bad = make_multi_shard_run(run_id="run_bad")
    mgr, state, _ = _rekey_manager({"mani_prev": prev, "mani_bad": bad})
    svc = _service()

    state["current"] = "mani_bad"  # the bad run is live
    svc.get_latest_dataframe(mgr, "key", "forecast")
    assert _prov_run_id(svc) == "run_bad"

    state["current"] = "mani_prev"  # operator quarantines mani_bad → previous manifest is now latest
    svc.get_latest_dataframe(mgr, "key", "forecast")
    assert _prov_run_id(svc) == "run_prev"  # rolled back to the previous run


def test_zero_manifest_forecast_fails_visible_not_legacy():
    """S5 + S1 (#264): if the manifest is quarantined to nothing (get_latest_manifest → None), a
    warm WIRE entry is invalidated (§11.4) and the forecast does NOT fall to a legacy artifact —
    with no freshness-datable last-good (the fixture stamps no `$createdAt`) it fails visible (503)."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a}, with_legacy=True)
    svc = _service()

    state["current"] = "mani_a"
    svc.get_latest_dataframe(mgr, "key", "forecast")  # warm wire entry
    mgr.get_latest_provenance.assert_not_called()

    state["current"] = None  # all manifests quarantined
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()  # never legacy


def test_manifest_lookup_blip_serves_warm_cache_not_500():
    """S5 (Edge B): a manifest-lookup exception must not 500 the warm path nor drop a good wire
    entry — it degrades to serving whatever is warm."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a}, with_legacy=True)
    svc = _service()

    state["current"] = "mani_a"
    first = svc.get_latest_dataframe(mgr, "key", "forecast")  # warm wire entry

    state["current"] = "RAISE"  # get_latest_manifest now throws
    out = svc.get_latest_dataframe(mgr, "key", "forecast")  # must not raise
    assert len(out) == len(first)
    assert _prov_run_id(svc) == "run_a"  # still the warm wire run
    mgr.get_latest_provenance.assert_not_called()  # did NOT fall through to legacy


# (S5 Edge C — "a manifest present + a mismatched disk entry → skip disk, re-ingest" — is now
# covered end-to-end with a real disk by test_new_manifest_supersedes_stale_disk_wire_entry and
# test_legacy_to_wire_transition_invalidates_warm_legacy, so the mock-poking variant was removed.)


def test_relaxed_capacity_guard_serves_run_the_whole_run_bound_would_refuse(monkeypatch):
    """S6b-2 (#208): the capacity guard is now PER-MONTH. A multi-month run whose WHOLE-run
    assembled size would exceed the cap now serves (assembled per-month to disk), where pre-S6b-2
    the whole-run bound refused it → legacy."""
    from tests.forecast._wire_fixtures import make_multi_shard_run
    from views_crafdapi.forecast.ingestion import wire_reader

    mrun = make_multi_shard_run(run_id="run_a")
    n_targets, cells, s = len(mrun.targets), len(mrun.units), mrun.sample_count
    n_months = len(mrun.months)
    assert n_months >= 2
    est_pm = wire_reader.estimate_assembled_bytes(n_targets, 1, cells, s)
    est_whole = wire_reader.estimate_assembled_bytes(n_targets, n_months, cells, s)
    cap = int(est_pm * 3 * 1.5)  # between the per-month peak (×3) and the whole-run peak (×3)
    assert est_pm * 3 <= cap < est_whole * 3  # serves now; the old whole-run bound would refuse
    monkeypatch.setenv("FAOAPI_MAX_ASSEMBLED_BYTES", str(cap))

    svc = _service()
    mgr = _multi_wire_manager(mrun)
    out = svc.get_latest_dataframe(mgr, "key", "forecast")
    mgr.get_latest_provenance.assert_not_called()  # served the wire run, NOT a legacy fallback
    assert len(out) == n_months * cells


def test_failed_wire_ingest_leaves_no_staging_dir(tmp_path):
    """S6b-2 review (resource): a failed wire ingest cleans its full-N staging dir (finally) and
    fails visible (503) — no leaked `*_value.*.tmp`, no silent legacy serve (S2)."""
    run = make_wire_run()
    manifest = dict(run.manifest)
    manifest["shards"] = [dict(manifest["shards"][0], sha256="0" * 64)]  # bad hash → wire fails
    mgr, _ = _legacy_manager_with_manifest(run, manifest_bytes=json.dumps(manifest).encode())
    svc = _service(cache_dir=tmp_path)
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()
    assert not list(tmp_path.glob("*_value.*.tmp")), "a staging dir leaked on the failure path"


def test_ingest_evicts_shard_bytes_to_hold_the_per_month_peak():
    """S6b-2 (#208): per-month ingest drops each shard's bytes from the in-memory file cache after
    loading, so the whole run's shards are never all resident at once."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    mrun = make_multi_shard_run(run_id="run_a")
    svc = _service()
    mgr = _multi_wire_manager(mrun)
    svc.get_latest_dataframe(mgr, "key", "forecast")

    shard_ids = {d["fileId"] for d in mrun.shard_docs}
    assert len(shard_ids) >= 4
    assert not (shard_ids & set(svc._file_cache)), "shard bytes were retained in _file_cache"


def test_unservable_run_fails_visible_and_reevaluates_each_request(monkeypatch):
    """ADR-033 §2, decision (i): a persistently-unservable manifested run (here: over the capacity
    bound) FAILS VISIBLE (503) and is RE-EVALUATED on each request — the prior legacy-fallback
    caching is dropped, so it is never served from legacy. (The file-byte cache avoids re-download;
    refusal-caching to skip the re-evaluation is deferred to S4.)"""
    monkeypatch.setenv("FAOAPI_MAX_ASSEMBLED_BYTES", "1")  # any real run trips the bound
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a}, with_legacy=True)
    svc = _service()

    state["current"] = "mani_a"
    with pytest.raises(HTTPException) as e1:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e1.value.status_code == 503

    with pytest.raises(HTTPException) as e2:  # SAME manifest → re-evaluated (decision i), still 503
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e2.value.status_code == 503
    assert mgr.get_latest_manifest.call_count >= 2  # re-evaluated each request, not a cached legacy serve
    mgr.get_latest_provenance.assert_not_called()   # never served legacy


def test_forecast_no_manifest_then_wire_serves_never_legacy():
    """S1 (#264): with no manifest a forecast fails visible (never a legacy artifact); when the
    first manifest appears it serves the wire run. A legacy forecast is served in neither phase."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a}, with_legacy=True)
    svc = _service()

    state["current"] = None  # no manifest yet → fail visible, NOT legacy
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()

    state["current"] = "mani_a"  # the first wire run is published
    svc.get_latest_dataframe(mgr, "key", "forecast")
    assert svc._dataframe_cache[HASH]["forecast"]["source_kind"] == "wire"
    assert _prov_run_id(svc) == "run_a"  # served the wire run
    mgr.get_latest_provenance.assert_not_called()  # never legacy


def test_force_refresh_reingests_even_with_matching_manifest():
    """S5: force_refresh still bypasses both cache tiers and re-ingests, manifest match or not."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a})
    svc = _service()

    state["current"] = "mani_a"
    svc.get_latest_dataframe(mgr, "key", "forecast")
    ds1 = svc._dataframe_cache[HASH]["forecast"]["dataset"]

    svc.get_latest_dataframe(mgr, "key", "forecast", force_refresh=True)
    ds2 = svc._dataframe_cache[HASH]["forecast"]["dataset"]
    assert ds2 is not ds1  # rebuilt from a fresh ingest, not returned from the warm/disk tier


# --- S6a (#208): wire disk-persistence + C-166 (source_kind/provenance round-trip) --------------

def _service_real_disk(tmp_path):
    """A DatasetService on a REAL disk cache at a specific dir (for cold-cache/restart tests that
    need the disk to persist across an in-memory clear). Same as `_service` post-S6b-2."""
    return _service(cache_dir=tmp_path)


def test_wire_run_persists_to_disk_and_survives_cold_cache(tmp_path):
    """S6a: a served wire run is persisted to disk keyed on the manifest fileId, so a cold worker
    (in-memory cache cleared, e.g. after a restart) serves it FROM DISK — no shard re-download —
    with its source_kind + provenance restored (C-166 / C-86)."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    mrun = make_multi_shard_run(run_id="run_a")
    svc = _service_real_disk(tmp_path)
    mgr = _multi_wire_manager(mrun)

    svc.get_latest_dataframe(mgr, "key", "forecast")  # cold: ingest + persist to disk
    downloads = mgr.download_prediction.call_count

    svc._dataframe_cache.clear()  # simulate a restart / fresh worker (disk survives)
    svc._file_cache.clear()
    out = svc.get_latest_dataframe(mgr, "key", "forecast")

    assert mgr.download_prediction.call_count == downloads  # served from disk, no re-download
    assert len(out) == len(mrun.months) * len(mrun.units)
    slot = svc._dataframe_cache[HASH]["forecast"]
    assert slot["source_kind"] == "wire"             # kind restored from disk meta (C-166)
    assert slot["provenance"]["run_id"] == "run_a"   # C-86 lineage survived the restart


def test_disk_wire_entry_manifest_vanishes_fails_visible_not_legacy(tmp_path):
    """S6a / C-166 §4.4 disk-half + S1 (#264): a wire run persisted to disk must NOT keep being
    served once its manifest is quarantined to nothing (the identity guard invalidates it), and it
    does NOT fall through to a legacy artifact — with no datable freshness it fails visible (503)."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a}, with_legacy=True)
    svc = _service_real_disk(tmp_path)

    state["current"] = "mani_a"
    svc.get_latest_dataframe(mgr, "key", "forecast")  # persist wire to disk
    svc._dataframe_cache.clear()
    svc._file_cache.clear()

    state["current"] = None  # all manifests quarantined
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()  # never legacy


def test_legacy_disk_entry_roundtrips_kind_and_provenance(tmp_path):
    """S6a regression: a legacy artifact persisted to disk reads back tagged 'legacy' with its C-86
    provenance restored (the extended write must not mistag or drop lineage). Exercised via
    `/historical` — post-S1 (#264) a *forecast* never serves a legacy artifact, but historical
    still does (loose files), so the legacy disk round-trip lives on that path."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")  # only its legacy fallback is exercised here
    mgr, state, legacy_rows = _rekey_manager({"mani_a": run_a}, with_legacy=True)
    svc = _service_real_disk(tmp_path)

    state["current"] = None  # historical never consults the manifest → legacy served + persisted
    svc.get_latest_dataframe(mgr, "key", "historical")
    svc._dataframe_cache.clear()
    svc._file_cache.clear()

    out = svc.get_latest_dataframe(mgr, "key", "historical")  # cold → disk hit
    slot = svc._dataframe_cache[HASH]["historical"]
    assert slot["source_kind"] == "legacy"
    assert slot["provenance"]["file_id"] == "legacy_1"
    assert len(out) == legacy_rows


def test_new_manifest_supersedes_stale_disk_wire_entry(tmp_path):
    """S6a: the S5 re-key holds on the DISK half too — a wire run persisted to disk is NOT served
    once a newer manifest exists; check_file_id(new fileId) misses the disk entry (keyed on the
    old fileId) and the new run is re-ingested."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    run_b = make_multi_shard_run(run_id="run_b")
    mgr, state, _ = _rekey_manager({"mani_a": run_a, "mani_b": run_b})
    svc = _service_real_disk(tmp_path)

    state["current"] = "mani_a"
    svc.get_latest_dataframe(mgr, "key", "forecast")  # persist run_a to disk
    svc._dataframe_cache.clear()
    svc._file_cache.clear()

    state["current"] = "mani_b"  # a newer run; disk still holds run_a keyed on mani_a
    svc.get_latest_dataframe(mgr, "key", "forecast")
    assert _prov_run_id(svc) == "run_b"  # re-ingested the new run, not the stale disk entry


def test_cold_worker_manifest_blip_serves_persisted_wire_from_disk(tmp_path):
    """S6a: on a manifest-lookup blip a COLD worker now serves the last-good wire run FROM DISK
    (source_kind survives) rather than dropping to legacy — a strict improvement over pre-S6a
    (where wire never lived on disk, so a cold-worker blip fell to legacy)."""
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a}, with_legacy=True)
    svc = _service_real_disk(tmp_path)

    state["current"] = "mani_a"
    svc.get_latest_dataframe(mgr, "key", "forecast")  # persist wire to disk
    svc._dataframe_cache.clear()
    svc._file_cache.clear()

    state["current"] = "RAISE"  # manifest lookup blips
    svc.get_latest_dataframe(mgr, "key", "forecast")  # must not 500
    slot = svc._dataframe_cache[HASH]["forecast"]
    assert slot["source_kind"] == "wire"          # served last-good wire from disk
    assert slot["provenance"]["run_id"] == "run_a"
    mgr.get_latest_provenance.assert_not_called()  # did NOT drop to legacy


def test_force_refresh_unservable_run_fails_visible(monkeypatch):
    """ADR-033 §2: a force_refresh of an unservable run (manifest present, over the bound) FAILS
    VISIBLE (503) — never a silent legacy fallback (S2)."""
    monkeypatch.setenv("FAOAPI_MAX_ASSEMBLED_BYTES", "1")  # manifest present but unservable
    from tests.forecast._wire_fixtures import make_multi_shard_run

    run_a = make_multi_shard_run(run_id="run_a")
    mgr, state, _ = _rekey_manager({"mani_a": run_a}, with_legacy=True)
    svc = _service()

    state["current"] = "mani_a"
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast", force_refresh=True)
    assert e.value.status_code == 503
    mgr.get_latest_provenance.assert_not_called()
