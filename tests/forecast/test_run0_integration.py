"""S6 (#251): a run-0-shaped delivery is **served-or-loudly-refused, never silent-stale**.

`test_wire_golden_fixture.py` pins the vendored contract bytes + a `wire_reader`-level ingest. This
drives those SAME real committed bytes through the **full production selection path**
(`DatasetService.get_latest_dataframe` → S1 typed decision, S2 fail-visible, S3 freshness, S4 grace
fallback, S5 capability gate) and asserts the epic invariant end to end: the delivery is either
`Served` (with surfaced source/status/freshness and the expected cells/targets) or refused with a
**visible** 503 — and on refusal it never silently drops to the legacy artifact. A failure→signal
matrix pins that each corruption maps to its own loud outcome.

The fixture is the run-0 skeleton (`fixture_run_0`, GAUL global-land sidecar, contract 1.5). Its
byte/tree integrity is pinned separately in `test_wire_golden_fixture.py`; here we exercise selection.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from tests.test_dataset_service_wire import HASH, _service
from views_crafdapi.managers import freshness
from views_crafdapi.managers.prediction import SHARD_ARTIFACT_TYPE, SIDECAR_ARTIFACT_TYPE

pytestmark = pytest.mark.layer4_infra

_DIR = Path(__file__).parent / "golden" / "wire_contract"


def _read(name: str) -> bytes:
    return (_DIR / name).read_bytes()


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _golden_manager(*, created_at, mutate=None, drop_shard=False):
    """A store manager that serves the committed run-0 golden as Appwrite would.

    ``created_at`` stamps the (delivery-time) manifest ``$createdAt``. ``mutate(manifest_dict)``
    corrupts the manifest before serving; ``drop_shard=True`` makes the shard vanish from the bucket
    (artifact resolution returns nothing). A legacy provenance is wired but MUST NOT be reached on a
    refusal — the tests assert `get_latest_provenance.assert_not_called()`.
    """
    manifest = json.loads(_read("fixture_run_0__manifest.json"))
    if mutate is not None:
        manifest = mutate(manifest)
    shard_names = [s["name"] for s in manifest.get("shards", [])]
    sidecar_name = (manifest.get("sidecar") or {}).get("name")

    manifest_doc = {
        "fileId": "mani", "filename": "fixture_run_0__manifest.json",
        "type": "sampled_forecast_manifest", "category": "forecast", "name": "un_crafd",
        "$createdAt": created_at,
    }
    mgr = MagicMock()
    mgr.get_latest_manifest.return_value = manifest_doc

    def resolve(names, artifact_type):
        if artifact_type == SHARD_ARTIFACT_TYPE:
            if drop_shard:
                return {}
            return {n: f"shard::{n}" for n in shard_names if n in names}
        if artifact_type == SIDECAR_ARTIFACT_TYPE:
            return {sidecar_name: "sidecar"} if sidecar_name in names else {}
        return {}

    mgr.resolve_artifact_file_ids.side_effect = resolve
    bytes_by_id = {"mani": json.dumps(manifest).encode(), "sidecar": _read(sidecar_name) if sidecar_name else b""}
    for n in shard_names:
        bytes_by_id[f"shard::{n}"] = _read(n)
    mgr.download_prediction.side_effect = lambda fid, *a, **k: MagicMock(
        success=True, data={"file_bytes": bytes_by_id[fid]}
    )
    legacy = MagicMock(file_id="legacy_1", source="datafactory", methodology_version="v1",
                       file_hash="h", created_at="2026", filename="legacy.parquet")
    legacy.to_dict.return_value = {"file_id": "legacy_1"}
    mgr.get_latest_provenance.return_value = legacy
    return mgr


def _prov(svc):
    return svc._dataframe_cache[HASH]["forecast"]["provenance"]


# --- Served: the happy path, fully surfaced -----------------------------------------------------

def test_run0_shaped_delivery_is_served_with_surfaced_provenance():
    """A fresh, conformant run-0-shaped delivery serves — with the expected cells/targets and a
    surfaced, identifiable, fresh provenance — and never touches the legacy selector."""
    svc = _service()
    mgr = _golden_manager(created_at=_iso(5))
    served = svc.get_latest_dataframe(mgr, "key", "forecast")

    assert len(served) == 6  # every delivered cell served (fixture_run_0 = 6 land cells)
    ds = svc._dataframe_cache[HASH]["forecast"]["dataset"]
    assert ds.targets == ["pred_lr_ged_sb"]

    prov = _prov(svc)
    assert prov["mode"] == "wire"
    assert prov["source"] == "fixture_ensemble"      # identifiable (S3 gate passes)
    assert "status" in prov                          # maturity surfaced (None until stamped)
    verdict = freshness.forecast_freshness(prov["created_at"], freshness.freshness_sla_days())
    assert verdict["is_stale"] is False              # fresh delivery
    mgr.get_latest_provenance.assert_not_called()    # served the wire run, not legacy

    # S7 (#252): the served-decision surface /provenance reports is the live wire run.
    served = svc.served_forecast_provenance()
    assert served["mode"] == "wire" and served["file_id"] == prov["file_id"]
    assert "status" in served


def test_run0_shaped_stale_delivery_still_serves_but_is_flagged_stale():
    """A delivery older than the SLA is NOT silently dropped — it serves, but the freshness verdict
    is `is_stale` (the /health-visible signal, S3), so 'silent-stale' is impossible."""
    svc = _service()
    mgr = _golden_manager(created_at=_iso(120))  # past the 45-day SLA
    served = svc.get_latest_dataframe(mgr, "key", "forecast")

    assert len(served) == 6
    verdict = freshness.forecast_freshness(_prov(svc)["created_at"], freshness.freshness_sla_days())
    assert verdict["is_stale"] is True
    mgr.get_latest_provenance.assert_not_called()


# --- Loudly refused: the failure→signal matrix --------------------------------------------------

def _corrupt_shard_hash(manifest):
    manifest["shards"] = [dict(manifest["shards"][0], sha256="0" * 64)]
    return manifest


def _bump_to_unsupported_schema(manifest):
    manifest["contract_version"] = "2.0"
    return manifest


@pytest.mark.parametrize(
    "kwargs, why",
    [
        ({"mutate": _corrupt_shard_hash}, "shard bytes != manifest sha256 (§4.5c)"),
        ({"drop_shard": True}, "a referenced shard is absent from the bucket"),
        ({"mutate": _bump_to_unsupported_schema}, "declares a contract dialect this build can't render (S5)"),
        ({"mutate": (lambda m: {"run_id": "x"})}, "malformed manifest (missing shards/sidecar)"),
    ],
)
def test_run0_shaped_corruption_fails_visible_never_silent_legacy(kwargs, why):
    """Each corruption of the run-0-shaped delivery maps to a VISIBLE 503 — never a silent serve of
    the legacy artifact (the C-170 hole). One assertion per failure mode, all on the real bytes."""
    svc = _service()
    mgr = _golden_manager(created_at=_iso(5), **kwargs)
    with pytest.raises(HTTPException) as e:
        svc.get_latest_dataframe(mgr, "key", "forecast")
    assert e.value.status_code == 503, why
    mgr.get_latest_provenance.assert_not_called()  # loud refusal, not a silent legacy fallback
