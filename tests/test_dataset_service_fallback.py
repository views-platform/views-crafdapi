"""S4 (#249, ADR-033 §6, D-24): the bounded, alarmed grace fallback.

When the newest manifested run is Refused (S1/S2), DatasetService serves the **last-good manifested
run** from disk — but only within the freshness SLA (S3), and only loudly (degraded serving-state +
WARNING alarm). Past the SLA, on unknown age, or with no manifested last-good, it fails visible (503)
rather than serve a possibly-outdated forecast or drop to the legacy artifact (the C-170 hole S2 shut).
"""

import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from tests.conftest import make_fao_df
from tests.forecast._wire_fixtures import make_wire_run
from tests.test_dataset_service_wire import HASH, _service
from views_crafdapi.managers.prediction import SHARD_ARTIFACT_TYPE, SIDECAR_ARTIFACT_TYPE

pytestmark = pytest.mark.layer4_infra


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _good_then_bad_manager(run, *, created_at):
    """A store manager with two phases, flipped via ``state['phase']``:

    - ``'good'`` — serves ``run`` normally (fileId ``mani_good``), persisting it to disk with
      ``created_at`` as its freshness stamp.
    - ``'bad'`` — a NEW manifest (fileId ``mani_bad``) whose declared shard hash is wrong, so its
      run is Refused (``ingest_failed``), while the disk still holds the good run.
    - ``'none'`` — no manifest at all (``get_latest_manifest`` → None): transition-mode legacy
      serving (a valid `make_fao_df` served from ``legacy_1``).
    """
    state = {"phase": "good"}
    base_doc = {"filename": f"{run.run_id}__manifest.json", "type": "sampled_forecast_manifest",
                "category": "forecast", "name": "un_crafd", "$createdAt": created_at}
    good_doc = dict(base_doc, fileId="mani_good")
    bad_doc = dict(base_doc, fileId="mani_bad")

    bad_manifest = dict(run.manifest)
    bad_manifest["shards"] = [dict(bad_manifest["shards"][0], sha256="0" * 64)]  # wrong hash

    legacy_df = make_fao_df()
    buf = io.BytesIO()
    legacy_df.to_parquet(buf)

    mgr = MagicMock()

    def latest_manifest():
        return {"good": good_doc, "bad": bad_doc, "none": None}[state["phase"]]

    mgr.get_latest_manifest.side_effect = latest_manifest

    def resolve(names, artifact_type):
        if artifact_type == SHARD_ARTIFACT_TYPE:
            return {run.shard_name: "shard_1"}
        if artifact_type == SIDECAR_ARTIFACT_TYPE:
            return {run.sidecar_name: "sidecar_1"}
        return {}

    mgr.resolve_artifact_file_ids.side_effect = resolve
    bytes_by_id = {
        "mani_good": json.dumps(run.manifest).encode(),
        "mani_bad": json.dumps(bad_manifest).encode(),
        "shard_1": run.shard_bytes,
        "sidecar_1": run.sidecar_bytes,
        "legacy_1": buf.getvalue(),
    }
    mgr.download_prediction.side_effect = lambda fid, *a, **k: MagicMock(
        success=True, data={"file_bytes": bytes_by_id[fid]}
    )
    legacy_prov = MagicMock(file_id="legacy_1", source="datafactory", methodology_version="v1",
                            file_hash="h", created_at="2026", filename="legacy.parquet")
    legacy_prov.to_dict.return_value = {"file_id": "legacy_1", "source": "datafactory"}
    mgr.get_latest_provenance.return_value = legacy_prov
    mgr._legacy_rows = len(legacy_df)
    return mgr, state


def test_within_sla_serves_last_good_degraded():
    """Newest run refused + a fresh last-good manifested run on disk ⇒ serve the last-good, flagged
    degraded (fallback active) — never silently, never the legacy artifact."""
    run = make_wire_run()
    svc = _service()
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))  # 5 days old, within the 45-day SLA

    good = svc.get_latest_dataframe(mgr, "key", "forecast")  # phase good → persist to disk
    assert svc.forecast_serving_state() == {"degraded": False}

    state["phase"] = "bad"  # a new, unservable run is published
    served = svc.get_latest_dataframe(mgr, "key", "forecast")

    assert len(served) == len(good)  # served the last-good run, not gone dark
    st = svc.forecast_serving_state()
    assert st["degraded"] is True and st["fallback_available"] is True
    assert st["serving"] == "last_good_manifested" and st["file_id"] == "mani_good"
    assert st["age_days"] is not None and st["age_days"] <= st["sla_days"]
    mgr.get_latest_provenance.assert_not_called()  # never fell back to the legacy artifact


def test_past_sla_fails_visible():
    """Newest run refused + the last-good manifested run is PAST the freshness SLA ⇒ fail visible
    (503), degraded state with no fallback — a bounded fallback, not an unbounded one."""
    run = make_wire_run()
    svc = _service()
    mgr, state = _good_then_bad_manager(run, created_at=_iso(200))  # 200 days old, past the SLA

    svc.get_latest_dataframe(mgr, "key", "forecast")
    state["phase"] = "bad"
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")

    assert e.value.status_code == 503
    st = svc.forecast_serving_state()
    assert st["degraded"] is True and st["fallback_available"] is False
    mgr.get_latest_provenance.assert_not_called()


def test_unknown_age_fails_visible():
    """A last-good run whose freshness cannot be computed (no ``created_at``) is treated as unknown,
    not fresh — the fallback refuses it and fails visible."""
    run = make_wire_run()
    svc = _service()
    mgr, state = _good_then_bad_manager(run, created_at=None)  # no freshness stamp

    svc.get_latest_dataframe(mgr, "key", "forecast")
    state["phase"] = "bad"
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")

    assert e.value.status_code == 503
    assert svc.forecast_serving_state()["fallback_available"] is False


def test_no_last_good_fails_visible():
    """Newest run refused with NO prior manifested run on disk ⇒ fail visible (nothing to fall back
    to). Here the very first request is already the bad run."""
    run = make_wire_run()
    svc = _service()
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))
    state["phase"] = "bad"  # bad from the outset — disk is empty

    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    st = svc.forecast_serving_state()
    assert st["degraded"] is True and st["fallback_available"] is False
    mgr.get_latest_provenance.assert_not_called()


def test_recovery_clears_degraded_state():
    """When the producer recovers (a good newest run again), the degraded/fallback state clears —
    the flag is not sticky."""
    run = make_wire_run()
    svc = _service()
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))

    svc.get_latest_dataframe(mgr, "key", "forecast")          # good
    state["phase"] = "bad"
    svc.get_latest_dataframe(mgr, "key", "forecast")          # degraded fallback
    assert svc.forecast_serving_state()["degraded"] is True

    state["phase"] = "good"                                   # producer recovers
    svc.get_latest_dataframe(mgr, "key", "forecast")
    assert svc.forecast_serving_state() == {"degraded": False}


def test_no_manifest_serves_last_good_manifested_degraded_never_legacy():
    """S1 (#264): when every manifest is quarantined-to-nothing (NoRun), a forecast serves the
    last-good MANIFESTED run within the SLA — degraded, reason ``no_manifest`` — and NEVER a legacy
    artifact. The grace window is the same one the Refused path uses (§6), extended to NoRun."""
    run = make_wire_run()
    svc = _service()
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))

    good = svc.get_latest_dataframe(mgr, "key", "forecast")   # good → persist mani_good to disk
    state["phase"] = "none"                                   # all manifests gone (NoRun)
    served = svc.get_latest_dataframe(mgr, "key", "forecast")

    assert len(served) == len(good)                          # last-good manifested, not legacy
    st = svc.forecast_serving_state()
    assert st["degraded"] is True and st["fallback_available"] is True
    assert st["reason"] == "no_manifest" and st["file_id"] == "mani_good"
    mgr.get_latest_provenance.assert_not_called()            # never legacy


def test_no_manifest_no_last_good_fails_visible():
    """S1 (#264): NoRun with no last-good manifested run on disk ⇒ fail visible (503), never legacy
    — the honest "no current forecast" state."""
    run = make_wire_run()
    svc = _service()
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))
    state["phase"] = "none"  # no manifest from the outset; disk is empty

    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503
    st = svc.forecast_serving_state()
    assert st["degraded"] is True and st["fallback_available"] is False
    mgr.get_latest_provenance.assert_not_called()


# ---------------------------------------------------------------------------------------------
# C-356 (#156): the routes call `get_latest_dataset`, not `get_latest_dataframe`. The fallback
# deliberately leaves the warm cache empty (ADR-033 decision (i)), so on a cold worker — a restart,
# or 4 h after the in-memory TTL drops the key — the dataset a route receives must come from the
# selection itself, never from re-reading a warm slot the fallback never filled.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("phase", ["bad", "none"])
def test_cold_worker_fallback_returns_last_good_dataset(tmp_path, phase):
    """Good run served, in-memory cache dropped, then the newest run is refused (``bad``) or gone
    (``none``): `get_latest_dataset` returns the last-good run, and the warm slot stays empty."""
    run = make_wire_run()
    svc = _service(tmp_path)
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))

    good = svc.get_latest_dataset(mgr, "key", "forecast")
    svc._dataframe_cache.clear()                      # the 4 h TTL lapsed
    state["phase"] = phase

    served = svc.get_latest_dataset(mgr, "key", "forecast")

    assert len(served.dataframe) == len(good.dataframe)
    assert svc.forecast_serving_state()["file_id"] == "mani_good"
    # ADR-033 decision (i): the fallback never becomes the warm entry.
    slot = next(iter(svc._dataframe_cache.values()))["forecast"]
    assert slot["dataset"] is None


@pytest.mark.parametrize("phase", ["bad", "none"])
def test_restarted_worker_fallback_returns_last_good_dataset(tmp_path, phase):
    """The same, across a restart: a second service on the same disk cache, as systemd's
    Restart=always produces on every deploy."""
    run = make_wire_run()
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))
    good = _service(tmp_path).get_latest_dataset(mgr, "key", "forecast")

    state["phase"] = phase
    restarted = _service(tmp_path)
    served = restarted.get_latest_dataset(mgr, "key", "forecast")

    assert len(served.dataframe) == len(good.dataframe)
    st = restarted.forecast_serving_state()
    assert st["degraded"] is True and st["serving"] == "last_good_manifested"


def _runs_manager(runs, *, created_at):
    """A store whose newest manifest is ``runs[state["phase"]]`` (a WireRun), or a refused run
    (``"bad"``: wrong shard hash). Each run keeps its own file ids, so two workers sharing one disk
    slot can be told apart by which run they serve."""
    state = {"phase": next(iter(runs))}
    mgr = MagicMock()

    def doc(key):
        return {"filename": f"{key}__manifest.json", "type": "sampled_forecast_manifest",
                "category": "forecast", "name": "un_crafd", "$createdAt": created_at,
                "fileId": f"mani_{key}"}

    def latest_manifest():
        return doc(state["phase"])

    def current_run():
        return runs[state["phase"] if state["phase"] != "bad" else "bad"]

    def resolve(names, artifact_type):
        run = current_run()
        if artifact_type == SHARD_ARTIFACT_TYPE:
            return {run.shard_name: f"shard_{state['phase']}"}
        if artifact_type == SIDECAR_ARTIFACT_TYPE:
            return {run.sidecar_name: f"sidecar_{state['phase']}"}
        return {}

    def download(fid, *a, **k):
        key = fid.split("_", 1)[1]
        run = runs[key]
        manifest = dict(run.manifest)
        if key == "bad":
            manifest["shards"] = [dict(manifest["shards"][0], sha256="0" * 64)]
        payload = {"mani": json.dumps(manifest).encode(), "shard": run.shard_bytes,
                   "sidecar": run.sidecar_bytes}[fid.split("_", 1)[0]]
        return MagicMock(success=True, data={"file_bytes": payload})

    mgr.get_latest_manifest.side_effect = latest_manifest
    mgr.resolve_artifact_file_ids.side_effect = resolve
    mgr.download_prediction.side_effect = download
    return mgr, state


def test_warm_worker_fallback_returns_the_selected_run_not_its_own_warm_entry(tmp_path):
    """The warm-worker half of C-356. Worker A holds run g1 warm; worker B then serves a newer run
    g2, which replaces the shared last-good disk slot; then g3 is refused. A's fallback selects g2
    from disk, and /provenance reports g2 — so A's route must return g2, not the g1 it holds warm.
    The runs differ in cell count so the returned dataset itself shows which one was served."""
    runs = {"g1": make_wire_run(run_id="g1", n_cells=6),
            "g2": make_wire_run(run_id="g2", n_cells=7),
            "bad": make_wire_run(run_id="g3", n_cells=8)}
    mgr, state = _runs_manager(runs, created_at=_iso(5))
    worker_a, worker_b = _service(tmp_path), _service(tmp_path)

    assert len(worker_a.get_latest_dataset(mgr, "key", "forecast").dataframe) == 6   # A warm on g1
    state["phase"] = "g2"
    assert len(worker_b.get_latest_dataset(mgr, "key", "forecast").dataframe) == 7   # disk now g2
    state["phase"] = "bad"

    served = worker_a.get_latest_dataset(mgr, "key", "forecast")

    assert worker_a.forecast_serving_state()["file_id"] == "mani_g2"
    assert len(served.dataframe) == 7, "served A's stale warm entry, not the run selection chose"


def test_a_caller_changing_its_dataset_does_not_change_the_next_serve(tmp_path):
    """`get_latest_dataset` promises a copy that is safe to subset. A route that changes what it
    received in place must not change what the next request is served from the warm cache."""
    run = make_wire_run()
    svc = _service(tmp_path)
    mgr, _ = _good_then_bad_manager(run, created_at=_iso(5))

    svc.get_latest_dataset(mgr, "key", "forecast")                        # ingest -> warm entry
    warm = svc.get_latest_dataset(mgr, "key", "forecast")                 # served from the warm cache
    n = len(warm.dataframe)
    warm.dataframe.drop(index=warm.dataframe.index[:2], inplace=True)     # a careless caller

    again = svc.get_latest_dataset(mgr, "key", "forecast")                # warm hit again
    assert len(again.dataframe) == n


@pytest.mark.parametrize("phase", ["bad", "none"])
def test_fallback_serves_last_good_past_disk_ttl_within_sla(tmp_path, phase):
    """C-360 (#157). The last-good run is 5 days old — inside the 45-day SLA — but its disk slot is
    past the disk TTL (set to 0 here; 24.5 days in production). ADR-033 §6 bounds the fallback by
    the SLA, so it must still serve. Before #157 the disk-tier read deleted the slot first: 503."""
    run = make_wire_run()
    svc = _service(tmp_path)
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))
    good = svc.get_latest_dataset(mgr, "key", "forecast")

    # Age only the last-good slot past the (production, 24.5-day) disk TTL; the run itself stays
    # 5 days old by its producer timestamp, so it is inside the SLA. Cold worker, so no warm entry.
    meta_path = svc._disk_cache._meta_path(HASH, "forecast")
    meta = json.loads(meta_path.read_text())
    meta["timestamp"] -= svc._disk_cache.ttl_seconds + 86400
    meta_path.write_text(json.dumps(meta))
    svc._dataframe_cache.clear()
    state["phase"] = phase

    for _ in range(2):                    # still served on a later request: the slot survived
        served = svc.get_latest_dataset(mgr, "key", "forecast")
        assert len(served.dataframe) == len(good.dataframe)
        st = svc.forecast_serving_state()
        assert st["degraded"] is True and st["serving"] == "last_good_manifested"
    assert meta_path.exists()


@pytest.mark.parametrize("gate", ["quarantined", "not_approved"])
def test_fallback_never_serves_a_last_good_run_the_operator_has_withdrawn(
    tmp_path, monkeypatch, gate
):
    """The operator's C-71 rollback controls bind the fallback too. Here the last-good run is the
    one withdrawn — quarantined by file id, or left off an active approval allowlist — and no other
    manifest exists, so selection is NoRun and the fallback reads that very run from disk. It must
    fail visible rather than serve what the operator removed (code-review on #157)."""
    from views_crafdapi.managers.prediction import quarantine

    run = make_wire_run()
    svc = _service(tmp_path)
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))
    svc.get_latest_dataset(mgr, "key", "forecast")                 # mani_good on disk

    if gate == "quarantined":
        monkeypatch.setenv(quarantine.QUARANTINED_FILE_IDS_ENV, "mani_good")
    else:
        monkeypatch.setenv(quarantine.APPROVED_FILE_IDS_ENV, "some_other_run")
    state["phase"] = "none"
    svc._dataframe_cache.clear()

    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataset(mgr, "key", "forecast")
    assert e.value.status_code == 503
    st = svc.forecast_serving_state()
    assert st["degraded"] is True and st["fallback_available"] is False


@pytest.mark.parametrize("where", ["manifest", "artifacts"])
def test_unauthorized_lookup_never_serves_fallback(tmp_path, where):
    """C-268 (#159). The good run is on disk and inside the SLA, so a lookup *outage* would be
    served from the fallback. A 401 is not an outage: the key was revoked. Whether the refusal
    arrives on the manifest lookup or on the shard/sidecar lookup of a newer run, the answer is 401."""
    from views_crafdapi.managers.prediction import MetadataLookupError

    run = make_wire_run()
    svc = _service(tmp_path)
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))
    svc.get_latest_dataset(mgr, "key", "forecast")
    svc._dataframe_cache.clear()

    revoked = MetadataLookupError("not authorized", status=401)
    if where == "manifest":
        mgr.get_latest_manifest.side_effect = revoked
    else:
        state["phase"] = "bad"                       # a newer manifest, whose artifacts 401
        mgr.resolve_artifact_file_ids.side_effect = revoked

    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataset(mgr, "key", "forecast")
    assert e.value.status_code == 401


def test_a_refused_artifact_download_is_401_not_a_fallback(tmp_path):
    """#159 review. The key passes the metadata lookups but the store refuses the download of the
    newer run's bytes (its bucket access was taken away). That is not `ingest_failed` — which would
    serve the last-good run to a key that may no longer read the bucket — it is 401."""
    from views_crafdapi.managers.appwrite import OperationResult

    run = make_wire_run()
    svc = _service(tmp_path)
    mgr, state = _good_then_bad_manager(run, created_at=_iso(5))
    svc.get_latest_dataset(mgr, "key", "forecast")
    svc._dataframe_cache.clear()
    svc._file_cache.clear()

    state["phase"] = "bad"
    mgr.download_prediction.side_effect = lambda fid, *a, **k: OperationResult(
        success=False, error="Download failed: user_unauthorized", code="user_unauthorized",
        status=401,
    )
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataset(mgr, "key", "forecast")
    assert e.value.status_code == 401


def test_the_cold_manifest_refetch_refuses_on_its_own(tmp_path):
    """The per-request manifest lookup failed transiently (an outage, so the cache tiers are tried);
    the cold path's own refetch is then refused. That second answer decides it (#159 guard audit)."""
    from views_crafdapi.managers.prediction import MetadataLookupError

    run = make_wire_run()
    svc = _service(tmp_path)
    mgr, _ = _good_then_bad_manager(run, created_at=_iso(5))
    answers = iter([MetadataLookupError("blip", status=503), MetadataLookupError("no", status=401)])
    mgr.get_latest_manifest.side_effect = lambda: (_ for _ in ()).throw(next(answers))

    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataset(mgr, "key", "forecast")        # cold: nothing warm, nothing on disk
    assert e.value.status_code == 401
