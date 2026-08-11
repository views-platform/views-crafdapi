"""D1 (#41, epic #40): the consumer preflight must agree with the serving path, and say
exactly WHY when it refuses.

The value of this module is the negative cases. A preflight that only ever passes is worse
than none — it manufactures confidence. Two classes of test enforce that:

* **Named-gate tests** break one thing and assert the report names *that* gate, so an operator
  is pointed at the real defect.
* **Differential tests** (`TestDifferentialAgreementWithServingPath`) drive the REAL
  `DatasetService._load_wire_run` over the same corruptions and assert the two verdicts agree.
  That is the guarantee — a hand-maintained parallel is exactly what drifted in review.

A false RED matters as much as a false green here: preflight must not be stricter than the
path it diagnoses, or it blocks deliveries production would have served.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from views_crafdapi.forecast import contract
from views_crafdapi.managers import selection
from views_crafdapi.preflight import (
    Verdict,
    find_run_manifest,
    format_report,
    main,
    preflight,
)

from ._wire_fixtures import make_wire_run

# layer4_infra, not layer5_audit: TESTING.md says pick the layer by what the test *proves*.
# This proves a deployment/diagnostic tool behaves correctly — infrastructure.
pytestmark = pytest.mark.layer4_infra

_GOLDEN = Path(__file__).resolve().parent / "golden" / "wire_contract"


# ---------------------------------------------------------------- staging helpers


def _stage(tmp_path: Path, run, *, shard_transform=None, manifest_transform=None,
           drop_shard=False, name="staged_run") -> Path:
    """Write a WireRun to a staging directory, optionally breaking one thing.

    Manifest hashes are recomputed AFTER `shard_transform`, so a mutation under test is not
    masked by the content-hash gate firing first.
    """
    staging = tmp_path / name
    staging.mkdir(parents=True, exist_ok=True)

    shard_bytes = run.shard_bytes
    if shard_transform is not None:
        shard_bytes = shard_transform(shard_bytes)
    if not drop_shard:
        (staging / run.shard_name).write_bytes(shard_bytes)
    (staging / run.sidecar_name).write_bytes(run.sidecar_bytes)

    manifest = copy.deepcopy(run.manifest)
    for entry in manifest["shards"]:
        entry["sha256"] = hashlib.sha256(shard_bytes).hexdigest()
    manifest["sidecar"]["sha256"] = hashlib.sha256(run.sidecar_bytes).hexdigest()
    if manifest_transform is not None:
        manifest = manifest_transform(manifest)
    (staging / f"{run.run_id}__manifest.json").write_text(json.dumps(manifest))
    return staging


def _rewrite_shard(shard_bytes: bytes, mutate) -> bytes:
    """Round-trip a shard parquet through `mutate(table, views_frames_header)`, preserving the
    `views_frames` schema metadata (losing it would break the load unrelatedly)."""
    table = pq.read_table(io.BytesIO(shard_bytes))
    header = json.loads((table.schema.metadata or {})[b"views_frames"])
    table, header = mutate(table, header)
    table = table.replace_schema_metadata({b"views_frames": json.dumps(header).encode()})
    out = io.BytesIO()
    pq.write_table(table, out)
    return out.getvalue()


def _gate(report, fragment: str):
    matches = [g for g in report.gates if fragment in g.name]
    assert matches, f"no gate matching {fragment!r}; gates: {[g.name for g in report.gates]}"
    return matches[0]


def _drop_ensemble(table, header):
    header["metadata"].setdefault("provenance", {}).pop("ensemble", None)
    return table, header


def _permute_sample(table, header):
    sample = table.column("sample").to_pylist()
    s = header["n_samples"]
    sample[:s] = list(reversed(sample[:s]))
    return table.set_column(
        table.schema.get_field_index("sample"), "sample",
        pa.array(sample, type=table.schema.field("sample").type),
    ), header


# ---------------------------------------------------------------- the happy path


class TestServableRun:

    def test_the_vendored_golden_fixture_is_servable(self):
        report = preflight(_GOLDEN)
        assert report.servable, format_report(report)
        assert report.run_id == "fixture_run_0"

    def test_a_freshly_generated_conformant_run_is_servable(self, tmp_path):
        report = preflight(_stage(tmp_path, make_wire_run()))
        assert report.servable, format_report(report)

    def test_every_gate_is_evaluated_on_a_good_run(self, tmp_path):
        """Guard the guard: a truncated gate list must not read as a pass."""
        report = preflight(_stage(tmp_path, make_wire_run()))
        assert report.checked_everything
        assert len(report.gates) >= 10, [g.name for g in report.gates]
        assert all(g.passed for g in report.gates)


# ---------------------------------------------------------------- verdict honesty


class TestVerdictCannotManufactureConfidence:
    """Every path that previously reported SERVABLE for a run the serving path refuses."""

    def test_an_empty_shards_list_is_refused_not_silently_passed(self, tmp_path):
        """Zero shards means zero gates could meaningfully run; production raises
        'no months were assembled'. A truncated all-pass gate list must not read as SERVABLE."""
        def empty(manifest):
            manifest["shards"] = []
            return manifest

        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=empty))
        assert report.verdict is Verdict.NOT_SERVABLE
        assert not report.servable

    def test_no_assemble_is_incomplete_never_servable(self, tmp_path):
        """The skipped gate covers the entire integrity block, so 'everything I checked was
        fine' must not be reported as 'this run is servable'."""
        report = preflight(_stage(tmp_path, make_wire_run()), assemble=False)
        assert report.verdict is Verdict.INCOMPLETE
        assert not report.servable
        assert not report.checked_everything
        assert any("SKIPPED" in n for n in report.notes)

    def test_a_shard_entry_without_time_id_is_refused(self, tmp_path):
        """The serving path indexes `entry["time_id"]` directly and raises KeyError. Reading it
        permissively here would green-light a run that 503s, and would also silently disable
        build_dataset's header-vs-manifest time_id cross-check."""
        def strip(manifest):
            manifest["shards"][0].pop("time_id")
            return manifest

        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=strip))
        assert not report.servable
        assert "time_id" in _gate(report, "run manifest parses").detail

    def test_declared_months_must_match_the_shard_time_ids(self, tmp_path):
        """Nothing downstream checks this: the assembler constrains the month COUNT, never the
        ids, so a producer off-by-one serves one window while /provenance advertises another."""
        def shift(manifest):
            manifest["expected_months"] = [999]
            return manifest

        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=shift))
        assert not report.servable
        gate = _gate(report, "self-consistent")
        assert not gate.passed and "expected_months" in gate.detail

    def test_declared_targets_must_match_the_shard_targets(self, tmp_path):
        def extra(manifest):
            manifest["targets"] = ["lr_ged_sb", "lr_ged_ns"]
            return manifest

        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=extra))
        assert not report.servable
        assert "targets" in _gate(report, "self-consistent").detail

    def test_capacity_bound_is_enforced_not_merely_noted(self, tmp_path, monkeypatch):
        """The serving path RAISES on this; demoting it to prose would let an operator arm an
        upload for a run that 503s on every request."""
        monkeypatch.setenv("CRAFDAPI_MAX_ASSEMBLED_BYTES", "100")
        report = preflight(_stage(tmp_path, make_wire_run()))
        assert not report.servable
        gate = _gate(report, "capacity bound")
        assert not gate.passed and "exceeds" in gate.detail

    def test_identifiability_reads_the_lowest_month_shard_like_the_serving_path(self, tmp_path):
        """`first_state` is the first shard of the LOWEST month, not of manifest list order.
        With a late-month-first manifest and only the earliest shard unidentifiable, reading
        manifest order reports PASS while production refuses after a full download."""
        early = make_wire_run(run_id="r", time_id=543)
        late = make_wire_run(run_id="r", time_id=544)

        staging = tmp_path / "two_months"
        staging.mkdir()
        early_bytes = _rewrite_shard(early.shard_bytes, _drop_ensemble)  # earliest loses it
        (staging / early.shard_name).write_bytes(early_bytes)
        (staging / late.shard_name).write_bytes(late.shard_bytes)
        (staging / early.sidecar_name).write_bytes(early.sidecar_bytes)

        manifest = copy.deepcopy(early.manifest)
        manifest["expected_months"] = [543, 544]
        manifest["shards"] = [  # LATE first — manifest order disagrees with month order
            {"name": late.shard_name, "target": late.target, "time_id": 544,
             "sha256": hashlib.sha256(late.shard_bytes).hexdigest()},
            {"name": early.shard_name, "target": early.target, "time_id": 543,
             "sha256": hashlib.sha256(early_bytes).hexdigest()},
        ]
        (staging / "r__manifest.json").write_text(json.dumps(manifest))

        report = preflight(staging)
        gate = _gate(report, "identifiable")
        assert not gate.passed, "must read the earliest month's shard, as the serving path does"
        assert not report.servable


# ---------------------------------------------------------------- named-gate failures


class TestUnservableRunNamesTheFailingGate:

    def test_dropped_shard_is_reported_as_a_missing_artifact(self, tmp_path):
        report = preflight(_stage(tmp_path, make_wire_run(), drop_shard=True))
        assert not report.servable
        gate = _gate(report, "artifacts present")
        assert not gate.passed and "0/1" in gate.detail

    def test_permuted_sample_column_is_reported_as_an_ordering_violation(self, tmp_path):
        """ADR-013 4.5(b): the loader reshapes positionally, so a permuted `sample` column
        would silently mis-slot draws. The defence is doubled — `views_frames.io.arrow.load`
        raises on it too — so assert the semantics, not which layer reported it."""
        report = preflight(_stage(
            tmp_path, make_wire_run(),
            shard_transform=lambda b: _rewrite_shard(b, _permute_sample),
        ))
        assert not report.servable
        gate = _gate(report, "identifying shard loads")
        assert not gate.passed
        assert "sample" in gate.detail
        assert any(t in gate.detail for t in ("tile", "reshape", "cell-major")), gate.detail

    @pytest.mark.parametrize("ensemble", [None, "unknown"])
    def test_unidentifiable_run_is_reported_before_any_upload(self, tmp_path, ensemble):
        def set_ensemble(table, header):
            prov = header["metadata"].setdefault("provenance", {})
            prov.pop("ensemble", None) if ensemble is None else prov.update(ensemble=ensemble)
            return table, header

        report = preflight(_stage(
            tmp_path, make_wire_run(),
            shard_transform=lambda b: _rewrite_shard(b, set_ensemble),
        ))
        assert not report.servable
        gate = _gate(report, "identifiable")
        assert not gate.passed and "Refused(unidentifiable)" in gate.detail

    def test_unrenderable_contract_version_halts_before_touching_artifacts(self, tmp_path):
        """The serving path refuses 'loudly, before assembling ~GBs'. Paying the full
        read+hash+decode for a refusal knowable from a 500-byte JSON inverts the point."""
        def bump(manifest):
            manifest["contract_version"] = "1.6"
            return manifest

        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=bump))
        assert not report.servable
        gate = _gate(report, "contract_version")
        assert not gate.passed
        assert "1.6" in gate.detail and contract.SERVED_CONTRACT_VERSION in gate.detail
        assert not any("assembles" in g.name for g in report.gates), "must halt, not carry on"
        assert not report.checked_everything

    def test_a_tampered_shard_fails_the_content_hash_gate(self, tmp_path):
        run = make_wire_run()
        staging = _stage(tmp_path, run)
        shard = staging / run.shard_name
        shard.write_bytes(shard.read_bytes() + b"\x00")
        report = preflight(staging)
        assert not report.servable
        assert not _gate(report, "content hashes").passed

    def test_missing_expected_cell_count_is_reported(self, tmp_path):
        def strip(manifest):
            manifest["expected_cell_count"] = None
            return manifest

        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=strip))
        assert not report.servable
        assert "expected_cell_count" in _gate(report, "manifest declares").detail


# ---------------------------------------------------------------- robustness


class TestMalformedInputFailsCleanlyNotWithATraceback:
    """The serving path performs these same accesses inside a blanket handler and returns a
    named Refused. A diagnostic that is less robust than the path it diagnoses is worse than
    useless for the malformed-manifest case it exists to catch."""

    @pytest.mark.parametrize("mutate, why", [
        (lambda m: {**m, "shards": [{"target": "t", "time_id": 1}]}, "shard entry with no name"),
        (lambda m: {**m, "shards": "not-a-list"}, "shards is not a list"),
        (lambda m: {**m, "shards": [["a"]]}, "shard entry is not an object"),
        (lambda m: {**m, "shards": [{"name": 7, "target": "t", "time_id": 1}]}, "non-str name"),
    ])
    def test_malformed_manifest_yields_a_report_not_an_exception(self, tmp_path, mutate, why):
        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=mutate))
        assert not report.servable, why
        assert report.failures, "a failure must be named, not raised"

    def test_an_artifact_name_escaping_the_staging_dir_is_not_resolved(self, tmp_path):
        """`directory / '/etc/passwd'` is `/etc/passwd`; hashing and loading a file outside the
        staging directory would green-light a directory that does not contain the run."""
        def escape(manifest):
            manifest["shards"][0]["name"] = "../outside.parquet"
            return manifest

        outside = tmp_path / "outside.parquet"
        outside.write_bytes(b"not a shard")
        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=escape))
        assert not report.servable
        assert not _gate(report, "artifacts present").passed

    def test_duplicate_shard_entries_are_reported_at_the_artifact_gate(self, tmp_path):
        def duplicate(manifest):
            manifest["shards"] = manifest["shards"] * 2
            return manifest

        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=duplicate))
        assert not report.servable
        assert "duplicate" in _gate(report, "artifacts present").detail

    def test_a_directory_with_no_run_manifest_fails_cleanly(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        report = preflight(empty)
        assert not report.servable
        assert not _gate(report, "run manifest parses").passed

    def test_a_nonexistent_directory_fails_cleanly(self, tmp_path):
        assert not preflight(tmp_path / "nope").servable

    def test_absent_hashes_are_noted_but_do_not_make_preflight_stricter_than_serving(self, tmp_path):
        """Production skips verification with a warning and serves. Refusing here would be a
        false RED and a differential-agreement failure — the honesty goes in the detail."""
        def drop_hashes(manifest):
            for entry in manifest["shards"]:
                entry.pop("sha256", None)
            manifest["sidecar"].pop("sha256", None)
            return manifest

        report = preflight(_stage(tmp_path, make_wire_run(), manifest_transform=drop_hashes))
        assert report.servable, "must agree with the serving path"
        assert "NOTHING VERIFIED" in _gate(report, "content hashes").detail
        assert any("NOT verified" in n for n in report.notes)


# ---------------------------------------------------------------- manifest discovery


class TestRunManifestDiscovery:

    def test_the_hop_a_per_target_manifest_is_not_mistaken_for_the_run_manifest(self):
        found = find_run_manifest(_GOLDEN)
        assert found.name == "fixture_run_0__manifest.json"
        data = json.loads(found.read_text())
        assert "targets" in data and "target" not in data

    def test_a_producer_run_summary_beside_the_manifest_does_not_cause_a_false_red(self, tmp_path):
        """The producer's run-completion summary has the same key shape. Adding the natural
        'did the run succeed' artifact must not be reported as two staged runs."""
        run = make_wire_run()
        staging = _stage(tmp_path, run)
        (staging / "run_summary.json").write_text(json.dumps(
            {"run_id": run.run_id, "targets": [run.target], "shards": [], "sidecar": {},
             "uploaded": False}
        ))
        report = preflight(staging)
        assert report.servable, format_report(report)

    def test_the_parent_of_the_run_directory_is_searched_one_level_down(self, tmp_path):
        """The sink stages into `staging_dir/<run_id>/`, so being handed the parent is the
        common operator mistake, not a malformed run."""
        parent = tmp_path / "wire_contract"
        _stage(parent, make_wire_run(), name="run_abc")
        assert preflight(parent).servable

    def test_two_genuine_runs_in_one_directory_is_an_error(self, tmp_path):
        run = make_wire_run()
        staging = _stage(tmp_path, run)
        (staging / "other_run__manifest.json").write_text(
            (staging / f"{run.run_id}__manifest.json").read_text())
        report = preflight(staging)
        assert not report.servable
        assert "exactly one run" in _gate(report, "run manifest parses").detail


# ---------------------------------------------------------------- differential oracle


def _wire_manager(run, manifest: dict):
    """A store manager over a staged run's bytes — the harness `tests/test_selection.py` uses
    to drive the real `_load_wire_run`."""
    mgr = MagicMock()
    mgr.get_latest_manifest.return_value = {"fileId": "mani_1", "$createdAt": "2026-08-01T00:00:00Z"}
    mgr.resolve_artifact_file_ids.side_effect = lambda names, t: {
        n: f"art_{n}" for n in names
    }
    payload = {
        "mani_1": json.dumps(manifest).encode(),
        f"art_{run.shard_name}": run.shard_bytes,
        f"art_{run.sidecar_name}": run.sidecar_bytes,
    }
    mgr.download_prediction.side_effect = lambda fid, *a, **k: MagicMock(
        success=bool(payload.get(fid)), data={"file_bytes": payload.get(fid)},
        error="absent",
    )
    return mgr


class TestDifferentialAgreementWithServingPath:
    """Preflight's verdict must equal the real serving path's verdict for the same run.

    This is the drift guarantee. A hand-maintained parallel is precisely what diverged in
    review — four gates had silently become more permissive than the code they mirrored.
    """

    @pytest.mark.parametrize("manifest_transform, label", [
        (None, "conformant run"),
        (lambda m: {**m, "contract_version": "1.6"}, "unrenderable dialect"),
        (lambda m: {**m, "shards": []}, "no shards"),
        (lambda m: {**m, "expected_cell_count": None}, "no expected_cell_count"),
        (lambda m: {**m, "expected_months": []}, "no expected_months"),
    ])
    def test_verdicts_agree(self, tmp_path, manifest_transform, label):
        from views_crafdapi.managers.dataset_service import DatasetService

        run = make_wire_run()
        staging = _stage(tmp_path, run, manifest_transform=manifest_transform)
        staged_manifest = json.loads(
            (staging / f"{run.run_id}__manifest.json").read_text())

        mine = preflight(staging).servable

        # The disk cache is stubbed to SUCCEED, so the only thing that can make the serving
        # path refuse is the run itself — which is what the comparison is about.
        (tmp_path / "svc_staging").mkdir(exist_ok=True)
        disk_cache = MagicMock(**{
            "staging_dir.return_value": tmp_path / "svc_staging",
            "write_value_dir.return_value": True,
            "read.return_value": {"dataset": MagicMock(), "timestamp": 1.0},
        })
        svc = DatasetService(
            dataframe_cache={}, file_cache={}, disk_cache=disk_cache,
            prediction_bucket_id="b", configs_getter=dict,
            api_key_hash_fn=lambda k: "h", check_staleness_fn=lambda t: MagicMock(is_stale=False),
        )
        theirs = svc._load_wire_run(
            _wire_manager(run, staged_manifest), "h", "forecast", {}, time.time(),
        )
        served = isinstance(theirs, selection.Served)

        assert mine == served, (
            f"{label}: preflight says servable={mine}, serving path says {type(theirs).__name__}"
        )


# ---------------------------------------------------------------- the CLI contract


class TestCli:
    """Banners must be disjoint strings. 'SERVABLE' as a substring of 'NOT SERVABLE' made the
    original success assertion vacuous — the sibling `smoke.py` uses disjoint banners for the
    same reason."""

    def test_servable_run_exits_zero_with_the_servable_banner(self, capsys):
        assert main([str(_GOLDEN)]) == 0
        out = capsys.readouterr().out
        assert "RESULT: SERVABLE" in out
        assert "REFUSED" not in out and "INCOMPLETE" not in out

    def test_refused_run_exits_one_with_a_disjoint_banner(self, tmp_path, capsys):
        assert main([str(_stage(tmp_path, make_wire_run(), drop_shard=True))]) == 1
        out = capsys.readouterr().out
        assert "RESULT: REFUSED" in out
        assert "RESULT: SERVABLE" not in out

    def test_no_assemble_exits_two_and_never_claims_servable(self, tmp_path, capsys):
        assert main([str(_stage(tmp_path, make_wire_run())), "--no-assemble"]) == 2
        out = capsys.readouterr().out
        assert "RESULT: INCOMPLETE" in out
        assert "RESULT: SERVABLE" not in out

    def test_the_three_banners_are_pairwise_disjoint(self):
        """Guard the guard: a substring relation between banners silently inverts any check
        on stdout, which is how the original success assertion became vacuous."""
        banners = ["RESULT: SERVABLE", "RESULT: REFUSED", "RESULT: INCOMPLETE"]
        for a in banners:
            for b in banners:
                assert a == b or a not in b
