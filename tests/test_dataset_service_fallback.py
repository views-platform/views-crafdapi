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
from tests.test_dataset_service_wire import _service
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
