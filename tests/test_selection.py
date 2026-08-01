"""S1 (#245, ADR-033 §1): the typed forecast-selection decision and its branches.

Two layers: the pure `selection` module (`Served`/`Refused`/`NoRun` + `run_gates`), and
`DatasetService._load_wire_run` returning the right typed result on each path — the
distinct, machine-readable `reason` that S1 makes first-class (replacing `dataframe | None`).
"""

import json
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.forecast._wire_fixtures import make_wire_run
from views_crafdapi.managers import selection
from views_crafdapi.managers.dataset_service import (
    _MANIFEST_UNFETCHED,
    DatasetService,
    _identifiable_gate,
)
from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager
from views_crafdapi.managers.prediction import SHARD_ARTIFACT_TYPE, SIDECAR_ARTIFACT_TYPE

pytestmark = pytest.mark.layer4_infra

HASH = "deadbeef00000000"


# --- the pure selection module -----------------------------------------------------------
def test_result_types_are_distinct_and_frozen():
    served = selection.Served(dataframe="df", dataset="ds", file_id="f", mode="wire")
    refused = selection.Refused("ingest_failed", detail="boom", file_id="f")
    assert served.mode == "wire" and served.provenance is None
    assert refused.reason == "ingest_failed" and refused.detail == "boom"
    assert isinstance(selection.NoRun(), selection.NoRun)
    with pytest.raises(Exception):  # frozen dataclass
        refused.reason = "x"


def test_run_gates_short_circuits_on_first_refused():
    served = selection.Served(dataframe="df", dataset="ds", file_id="f", mode="wire")
    calls = []
    out = selection.run_gates(
        served,
        [
            lambda r: (calls.append("a"), r)[1],
            lambda r: (calls.append("b"), selection.Refused("blocked"))[1],
            lambda r: (calls.append("c"), r)[1],
        ],
    )
    assert isinstance(out, selection.Refused) and out.reason == "blocked"
    assert calls == ["a", "b"]  # the gate after the first Refused never runs


def test_run_gates_only_applies_to_served():
    refused = selection.Refused("x")
    ran = []
    out = selection.run_gates(refused, [lambda r: ran.append(1) or r])
    assert out is refused and ran == []


def test_run_gates_empty_is_identity():
    served = selection.Served(dataframe="df", dataset="ds", file_id="f", mode="wire")
    assert selection.run_gates(served, ()) is served


# --- S2 (#247): the identifiable gate (ADR-033 §3) ---------------------------------------
def test_identifiable_gate_refuses_unknown_or_absent_source():
    for prov in ({"source": "unknown"}, {"source": ""}, {}, None):
        served = selection.Served(dataframe="df", dataset="ds", file_id="f", mode="wire",
                                  provenance=prov)
        out = _identifiable_gate(served)
        assert isinstance(out, selection.Refused) and out.reason == "unidentifiable"


def test_identifiable_gate_passes_a_named_source():
    served = selection.Served(dataframe="df", dataset="ds", file_id="f", mode="wire",
                              provenance={"source": "rusty_bucket"})
    assert _identifiable_gate(served) is served


# --- _load_wire_run typed branches -------------------------------------------------------
def _service():
    disk = CrafdDiskCacheManager(Path(tempfile.mkdtemp()))
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


def _empty_cache():
    return {"data": None, "file_id": None, "timestamp": None, "dataset": None,
            "source_kind": None, "provenance": None}


def _wire_manager(run):
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
    mgr.download_prediction.side_effect = lambda fid, *a, **k: MagicMock(
        success=True, data={"file_bytes": bytes_by_id[fid]}
    )
    return mgr


def test_no_manifest_returns_NoRun():
    out = _service()._load_wire_run(
        MagicMock(), HASH, "forecast", _empty_cache(), time.time(), manifest_doc=None
    )
    assert isinstance(out, selection.NoRun)


def test_malformed_manifest_returns_Refused():
    out = _service()._load_wire_run(
        MagicMock(), HASH, "forecast", _empty_cache(), time.time(), manifest_doc={}
    )
    assert isinstance(out, selection.Refused) and out.reason == "manifest_malformed"


def test_manifest_lookup_error_returns_Refused():
    mgr = MagicMock()
    mgr.get_latest_manifest.side_effect = RuntimeError("appwrite down")
    out = _service()._load_wire_run(
        mgr, HASH, "forecast", _empty_cache(), time.time(), manifest_doc=_MANIFEST_UNFETCHED
    )
    assert isinstance(out, selection.Refused) and out.reason == "manifest_lookup_error"


def test_ingest_failure_returns_Refused():
    mgr = MagicMock()
    mgr.download_prediction.side_effect = RuntimeError("boom")
    out = _service()._load_wire_run(
        mgr, HASH, "forecast", _empty_cache(), time.time(), manifest_doc={"fileId": "mani_1"}
    )
    assert isinstance(out, selection.Refused) and out.reason == "ingest_failed"
    assert out.file_id == "mani_1"


def test_valid_run_returns_Served():
    run = make_wire_run()
    out = _service()._load_wire_run(
        _wire_manager(run), HASH, "forecast", _empty_cache(), time.time(),
        manifest_doc=run.manifest_doc,
    )
    assert isinstance(out, selection.Served)
    assert out.mode == "wire"
    assert out.provenance["run_id"] == run.run_id
    assert out.provenance["mode"] == "wire"      # S2: surfaced
    assert "status" in out.provenance            # S2: surfaced (None until producer stamps it)
    assert len(out.dataframe) == len(run.units)
