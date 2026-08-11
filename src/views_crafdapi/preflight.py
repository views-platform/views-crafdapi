"""Can *this build* serve a staged wire-contract run? (D1 / #41, epic #40.)

The producer has no dry-run flag — the ``wire_upload_enabled`` interlock IS the dry-run
control (views-models#333). With it closed a full run is written to a staging directory and
**zero** store calls are made. This module answers the question that staging cannot:

    given a staged run directory, would views-crafdapi serve it, and if not, which gate failed?

Without it, the first signal of a malformed run is a bare HTTP 503 raised *after* the whole
run has been downloaded from Appwrite and assembly attempted.

Design rules, learned the hard way in review:

* **Never manufacture confidence.** A gate that could not be evaluated is not a pass. The
  verdict is three-valued — ``SERVABLE`` / ``NOT_SERVABLE`` / ``INCOMPLETE`` — because
  "everything I checked was fine" and "I checked everything and it was fine" are different
  claims, and only the second licenses arming an upload.
* **Mirror the serving path's order, strictness and memory discipline**, not just its rules:
  refuse on capability before touching artifacts; read ``time_id`` as strictly as
  ``dataset_service`` does; take the identifying shard from the *lowest month* as it does;
  hold one month of shard state at a time as it does. A diagnostic that is more permissive
  than the thing it diagnoses is a false-green oracle.
* **Own no rules.** Every gate delegates to the code the serving path runs
  (``forecast.contract``, ``forecast.ingestion.wire_reader``, and ``dataset_service``'s own
  capacity constants). Divergence is caught by a differential test that drives the real
  ``DatasetService._load_wire_run`` over the same corruptions
  (``tests/forecast/test_preflight.py::TestDifferentialAgreementWithServingPath``) — the
  guarantee an earlier version of this docstring merely asserted.

**Dependency direction (ADR-002 / ADP).** Operational and test-time only: imported by
``scripts/preflight_run.py`` and by tests, **never** by a producer at runtime.

**No Class Intent Contract, deliberately (ADR-006 / `docs/CICs/README.md` §"When Is an Intent
Contract Required?").** This module owns no state and enforces no invariant of its own — it
*delegates* every rule to the code the serving path runs and reports the outcome. The README's
mandatory categories cover components that enforce invariants **on the system**; an observer
that only reports is not one. The governing precedent is ``smoke.py``, the operator tool this
mirrors structurally (importable module + thin ``scripts/`` wrapper + exit-code contract),
which likewise has no CIC. Contrast ``docs/CICs/BulkParquetWriter.md``, which does have one for
a function-only module — because it *produces a governed artifact* (the ADR-025 schema), which
this does not. Revisit if preflight ever gains rules of its own rather than borrowing them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from views_crafdapi.forecast import contract
from views_crafdapi.forecast.ingestion import wire_reader

logger = logging.getLogger(__name__)

# Distinguishing the Hop-B RUN manifest from other staged json. Hop-A per-target manifests
# carry a singular `target`; the producer's run-completion summary can otherwise look alike,
# so the `__manifest.json` filename is used as a documented TIEBREAK when shape alone is
# ambiguous — never as the primary rule.
_SHARDS_KEY = "shards"
_HOP_A_ONLY_KEY = "target"
_MANIFEST_SUFFIX = "__manifest.json"


class Verdict(str, Enum):
    SERVABLE = "SERVABLE"
    NOT_SERVABLE = "NOT SERVABLE"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class Gate:
    """One named check and its outcome. ``terminal`` gates halt the run on failure, mirroring
    the serving path's own early refusals."""

    name: str
    passed: bool
    detail: str = ""
    terminal: bool = False

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{'PASS' if self.passed else 'FAIL'}  {self.name}" + (
            f"\n        {self.detail}" if self.detail else ""
        )


@dataclass(frozen=True)
class PreflightReport:
    """The verdict for one staged run.

    ``checked_everything`` is False when a gate was skipped rather than evaluated — an earlier
    terminal failure, or ``assemble=False``. It is what stops a truncated gate list from
    reading as a pass.
    """

    directory: Path
    run_id: Optional[str] = None
    gates: List[Gate] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    checked_everything: bool = True

    @property
    def failures(self) -> List[Gate]:
        return [g for g in self.gates if not g.passed]

    @property
    def verdict(self) -> Verdict:
        if self.failures or not self.gates:
            return Verdict.NOT_SERVABLE
        return Verdict.SERVABLE if self.checked_everything else Verdict.INCOMPLETE

    @property
    def servable(self) -> bool:
        """True only when every gate ran AND every gate passed."""
        return self.verdict is Verdict.SERVABLE


class _Halt(Exception):
    """Raised internally to stop at a terminal gate, as the serving path returns early."""


def find_run_manifest(directory: Path) -> Path:
    """Return the Hop-B *run* manifest for the run staged at ``directory``.

    Searches ``directory`` and, if nothing is found there, its immediate subdirectories — the
    producer sink stages into ``staging_dir/<run_id>/``, so being handed the parent is the
    common operator mistake, not a malformed run.
    """
    for root in (directory, *sorted(p for p in directory.iterdir() if p.is_dir())):
        found = _run_manifests_in(root)
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            by_convention = [p for p in found if p.name.endswith(_MANIFEST_SUFFIX)]
            if len(by_convention) == 1:
                return by_convention[0]
            raise ValueError(
                f"{len(found)} candidate run manifests in {root}: {[p.name for p in found]} — "
                f"cannot disambiguate by shape or by the {_MANIFEST_SUFFIX!r} convention. "
                f"A staging directory must hold exactly one run."
            )
    raise FileNotFoundError(
        f"no Hop-B run manifest in {directory} or its immediate subdirectories — expected a "
        f"*.json carrying {_SHARDS_KEY!r} without a singular {_HOP_A_ONLY_KEY!r} key"
    )


def _run_manifests_in(root: Path) -> List[Path]:
    out = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if isinstance(data, dict) and _SHARDS_KEY in data and _HOP_A_ONLY_KEY not in data:
            out.append(path)
    return out


def _resolve(directory: Path, name: Any) -> Optional[Path]:
    """Resolve a manifest artifact *name* to a file inside ``directory``.

    Containment is enforced: an absolute or ``..``-bearing name would otherwise resolve to a
    file outside the staging directory and be hashed and loaded as if it belonged to the run.
    """
    if not isinstance(name, str) or not name:
        return None
    candidate = (directory / name).resolve()
    try:
        candidate.relative_to(directory.resolve())
    except ValueError:
        logger.warning("manifest artifact name %r escapes the staging directory", name)
        return None
    return candidate if candidate.is_file() else None


def _shard_entries(manifest) -> List[Dict[str, Any]]:
    """The manifest's shard entries, validated to the shape both paths index into."""
    entries = manifest.shards
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest lists no shards — the run would assemble zero months")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"shards[{i}] is {type(entry).__name__}, expected an object")
        for key in ("name", "target", "time_id"):
            if key not in entry:
                # The serving path indexes `entry["time_id"]` directly and reads name/target
                # unguarded; a missing key is a KeyError there, so it must fail here too.
                raise ValueError(f"shards[{i}] has no {key!r} (serving path indexes it directly)")
    return entries


def preflight(directory: Path | str, *, assemble: bool = True) -> PreflightReport:
    """Report whether this build can serve the staged run in ``directory``."""
    directory = Path(directory)
    gates: List[Gate] = []
    notes: List[str] = []
    run_id: Optional[str] = None
    complete = True

    def add(gate: Gate) -> None:
        gates.append(gate)
        if not gate.passed and gate.terminal:
            raise _Halt()

    if not directory.is_dir():
        return PreflightReport(
            directory, None,
            [Gate("staging directory exists", False, f"{directory} is not a directory", True)],
            checked_everything=False,
        )

    try:
        # --- 1. a single, parseable run manifest (terminal) ---------------------------
        try:
            manifest_path = find_run_manifest(directory)
            # Artifacts are resolved relative to the MANIFEST's directory, not the argument:
            # `find_run_manifest` may have descended into `staging_dir/<run_id>/`, and resolving
            # against the parent would then report every artifact missing.
            run_root = manifest_path.parent
            manifest = wire_reader.parse_run_manifest(manifest_path.read_bytes())
            entries = _shard_entries(manifest)
            run_id = manifest.run_id
            add(Gate("run manifest parses", True,
                     f"{manifest_path.name} (run_id={run_id}, {len(entries)} shard entries)"
                     + (f"; run root {run_root.name}/" if run_root != directory else "")))
        except Exception as e:
            add(Gate("run manifest parses", False, f"{type(e).__name__}: {e}" if not str(e)
                     else str(e), terminal=True))

        # --- 2. capability, before touching a single artifact (terminal) ---------------
        # dataset_service refuses here "loudly, before assembling ~GBs"; paying the full
        # read+hash+decode for a refusal knowable from a 500-byte JSON would invert the point.
        declared = manifest.contract_version
        if contract.can_render_contract(declared):
            add(Gate("contract_version renderable", True,
                     f"declared {declared!r}; build serves {contract.SERVED_CONTRACT_VERSION!r}"
                     if str(declared).strip() else
                     f"unstamped — transition-safe pass (build serves "
                     f"{contract.SERVED_CONTRACT_VERSION!r})"))
        else:
            add(Gate("contract_version renderable", False,
                     f"run declares {declared!r}; this build serves "
                     f"{contract.SERVED_CONTRACT_VERSION!r} — would be "
                     f"Refused(schema_capability_mismatch)", terminal=True))

        # --- 3. the manifest declares what per-month assembly requires -----------------
        missing = [n for n, v in (("expected_months", manifest.expected_months),
                                  ("sidecar.name", manifest.sidecar_name))
                   if not v]
        if manifest.expected_cell_count is None:
            missing.append("expected_cell_count")
        add(Gate("manifest declares expected_months, expected_cell_count, sidecar",
                 not missing, f"missing/empty: {missing}" if missing
                 else f"{len(manifest.expected_months)} month(s), "
                      f"{manifest.expected_cell_count} cells/shard", terminal=bool(missing)))

        # --- 4. the manifest agrees with itself ---------------------------------------
        # Nothing downstream checks this: WireRunAssembler constrains the month COUNT, never
        # the ids. A producer off-by-one on the window would serve one range while the
        # manifest — and /provenance, which publishes manifest.targets — advertises another.
        shard_months = {e["time_id"] for e in entries}
        declared_months = set(manifest.expected_months)
        shard_targets = {e["target"] for e in entries}
        declared_targets = set(manifest.targets)
        inconsistencies = []
        if shard_months != declared_months:
            inconsistencies.append(
                f"expected_months {sorted(declared_months)} != shard time_ids {sorted(shard_months)}"
            )
        if declared_targets and shard_targets != declared_targets:
            inconsistencies.append(
                f"targets {sorted(declared_targets)} != shard targets {sorted(shard_targets)}"
            )
        if not declared_targets:
            inconsistencies.append("manifest declares no targets")
        add(Gate("manifest is self-consistent (months, targets)", not inconsistencies,
                 "; ".join(inconsistencies) if inconsistencies
                 else f"{len(declared_months)} month(s) x {len(declared_targets)} target(s)"))

        # --- 5. every referenced artifact is present (terminal) ------------------------
        shard_paths: Dict[str, Path] = {}
        missing_shards = []
        for name in manifest.shard_names:
            resolved = _resolve(run_root, name)
            if resolved is None:
                missing_shards.append(name)
            else:
                shard_paths[name] = resolved
        duplicates = len(manifest.shard_names) - len(set(manifest.shard_names))
        sidecar_path = _resolve(run_root, manifest.sidecar_name)
        artifacts_ok = not missing_shards and sidecar_path is not None and not duplicates
        add(Gate("all manifest artifacts present on disk", artifacts_ok,
                 f"{len(shard_paths)}/{len(manifest.shard_names)} shard(s) resolved"
                 + (f"; {duplicates} duplicate manifest entr(ies)" if duplicates else "")
                 + (f"; missing: {missing_shards[:5]}" if missing_shards else "")
                 + ("; sidecar MISSING" if sidecar_path is None else "; sidecar found"),
                 terminal=not artifacts_ok))

        # --- 6. content hashes ---------------------------------------------------------
        hash_failures, unhashed = [], []
        verified = 0  # counted directly; deriving it by arithmetic over two lists silently
        # miscounts the moment a shard name repeats or a second sidecar appears.
        for name in manifest.shard_names:
            expected = manifest.sha256_for(name)
            if not expected:
                unhashed.append(name)
                continue
            try:
                wire_reader.verify_content_hash(
                    shard_paths[name].read_bytes(), expected, f"shard {name}")
                verified += 1
            except ValueError as e:
                hash_failures.append(str(e))
        if manifest.sidecar_sha256:
            try:
                wire_reader.verify_content_hash(
                    sidecar_path.read_bytes(), manifest.sidecar_sha256, "sidecar")
                verified += 1
            except ValueError as e:
                hash_failures.append(str(e))
        else:
            unhashed.append(manifest.sidecar_name)
        total_artifacts = len(manifest.shard_names) + 1
        # Verdict is `not hash_failures`, matching the serving path exactly: an absent sha256 is
        # a manifest-completeness gap that production skips with a warning, so refusing here
        # would make preflight STRICTER than the thing it diagnoses (a false red, and a
        # differential-agreement failure). The honesty lives in the detail string instead —
        # a gate must never read "hashes match" when nothing was hashed.
        add(Gate("content hashes match the manifest", not hash_failures,
                 "; ".join(hash_failures[:3]) if hash_failures
                 else (f"{verified} of {total_artifacts} artifact(s) verified" if verified else
                       "NOTHING VERIFIED — no artifact carries a sha256 (serving path also skips)")))
        if unhashed:
            notes.append(
                f"{len(unhashed)} artifact(s) carry no sha256 and were NOT verified "
                f"(e.g. {unhashed[:3]}). The serving path also skips these with a warning."
            )

        # --- 7. identifiability, read from the shard the serving path reads -------------
        # dataset_service iterates `sorted(shards_by_month)` and keeps the FIRST state it sees,
        # so the identifying shard is the first entry of the LOWEST month — not manifest order.
        by_month: Dict[int, List[Dict[str, Any]]] = {}
        for entry in entries:
            by_month.setdefault(entry["time_id"], []).append(entry)
        lowest = sorted(by_month)[0]
        first_entry = by_month[lowest][0]
        try:
            first_state = wire_reader.load_shard_state(
                shard_paths[first_entry["name"]].read_bytes())
        except Exception as e:
            # `add` raises _Halt for a failed terminal gate, but relying on that implicitly
            # would leave `first_state` unbound below if the gate were ever made non-terminal,
            # and would emit two same-named contradictory gates. Halt explicitly.
            add(Gate("identifying shard loads", False,
                     f"{first_entry['name']}: {e}", terminal=True))
            raise _Halt()
        add(Gate("identifying shard loads", True,
                 f"{first_entry['name']} (month {lowest}, the shard the serving path reads)"))

        wire_prov = (first_state.get("metadata") or {}).get("provenance") or {}
        ensemble = wire_prov.get("ensemble")
        identifiable = bool(ensemble) and ensemble != "unknown"
        add(Gate("run is identifiable (shard header provenance.ensemble)", identifiable,
                 f"ensemble={ensemble!r}" + ("" if identifiable else
                 " — would be Refused(unidentifiable) AFTER the whole run downloads")))

        # --- 8. capacity, enforced with the serving host's own constants ----------------
        add(_capacity_gate(manifest, first_state, notes))

        # --- 9. streaming assembly ------------------------------------------------------
        if not assemble:
            complete = False
            notes.append(
                "assembly gate SKIPPED (--no-assemble): integrity, sidecar coverage, "
                "rectangularity and plausibility were NOT checked. Verdict is INCOMPLETE."
            )
        else:
            add(_assembly_gate(manifest, sidecar_path, shard_paths, by_month))

        # What a filesystem preflight structurally cannot see. Stating the boundary is part of
        # not manufacturing confidence: a green report here is not a green delivery.
        notes.append(
            "OUT OF SCOPE: this checks the staged BYTES only. It cannot see the Appwrite "
            "document metadata (`type`, `category`, `name=un_crafd`) that decides whether the "
            "consumer can find them — a correctly-staged run uploaded under the wrong `name` "
            "is invisible to every query and 503s with 'no manifested run' (C-77 class)."
        )

    except _Halt:
        complete = False
        notes.append(
            "stopped at the first terminal failure — later gates were NOT evaluated, "
            "exactly as the serving path returns early."
        )

    return PreflightReport(directory, run_id, gates, notes, checked_everything=complete)


def _capacity_gate(manifest, first_state, notes: List[str]) -> Gate:
    """The §4.6 bound, using ``dataset_service``'s own constants rather than restating them.

    Imported lazily and by name so the threshold and safety factor have exactly one definition;
    a copy here would drift the moment the serving host's bound is retuned.
    """
    from views_crafdapi.managers import dataset_service as svc

    # Mirror _guard_run_capacity's early return precisely: with any of these absent it applies
    # NO bound, so asserting one here would invent a ceiling that is not in force.
    if manifest.expected_cell_count is None or not manifest.expected_months or not manifest.targets:
        return Gate("per-month working set within the capacity bound", True,
                    "not estimable from this manifest — the serving path applies no bound either")
    try:
        s = int(first_state["values"].shape[1])
        est = wire_reader.estimate_assembled_bytes(
            len(manifest.targets), 1, manifest.expected_cell_count, s)
        peak = est * svc._ASSEMBLY_SAFETY_FACTOR
        cap = svc._max_assembled_bytes()
    except Exception as e:
        logger.warning("capacity estimate failed: %s", e)
        return Gate("per-month working set within the capacity bound", True,
                    f"not estimable ({type(e).__name__}) — treated as unbounded, as the "
                    f"serving path does when it cannot estimate")
    notes.append(
        f"per-month working set ~{est / 1024**3:.2f} GiB (S={s}); "
        f"x{svc._ASSEMBLY_SAFETY_FACTOR} peak ~{peak / 1024**3:.2f} GiB "
        f"vs bound {cap / 1024**3:.2f} GiB (this host's {svc._MAX_ASSEMBLED_BYTES_ENV})"
    )
    return Gate("per-month working set within the capacity bound", peak <= cap,
                f"peak ~{peak / 1024**3:.2f} GiB exceeds the {cap / 1024**3:.2f} GiB bound — "
                f"would be Refused(ingest_failed)" if peak > cap
                else f"peak ~{peak / 1024**3:.2f} GiB <= {cap / 1024**3:.2f} GiB")


def _assembly_gate(manifest, sidecar_path: Path, shard_paths, by_month) -> Gate:
    """Assemble month by month, holding one month of shard state at a time.

    This mirrors the serving path's S6b-2 memory discipline. Retaining every shard's ``(N, S)``
    block would pin the whole run (~28.6 GB at full scale) — the very ingest wall
    ``WireRunAssembler`` exists to dissolve, in a tool that runs on the producer's host.
    """
    import tempfile

    name = "run assembles (integrity, coverage, rectangularity, plausibility)"
    try:
        sidecar_df = wire_reader.read_sidecar(sidecar_path.read_bytes())
        # Assemble into the staging directory's own filesystem, not $TMPDIR: the assembler
        # preallocates the FULL-run (N, S) memmap, which would ENOSPC a tmpfs /tmp and be
        # misreported as a rectangularity failure in the producer's run.
        with tempfile.TemporaryDirectory(
            prefix=".crafdapi-preflight-", dir=str(sidecar_path.parent)
        ) as tmp:
            assembler = wire_reader.WireRunAssembler(Path(tmp), manifest, sidecar_df)
            for month in sorted(by_month):
                entries = by_month[month]
                states = [
                    wire_reader.load_shard_state(shard_paths[e["name"]].read_bytes())
                    for e in entries
                ]
                assembler.append_month(states, entries, month)
                del states  # one month resident at a time
            rows = assembler.finalize()
            # The serving path does not stop at finalize(): it adopts the value-dir and reads
            # it back with `ForecastDataset.from_value(mmap=True)`. A dir that assembles but
            # does not read back is another false green, so prove the round-trip here too.
            from views_crafdapi.data.handlers import ForecastDataset

            dataset = ForecastDataset.from_value(Path(tmp), mmap=True)
            read_back = len(dataset.dataframe)
            if read_back != rows:
                return Gate(name, False,
                            f"assembled {rows} rows but the value-dir read back {read_back}")
        return Gate(name, True,
                    f"{rows} rows across {len(by_month)} month(s); value-dir reads back clean")
    except Exception as e:
        return Gate(name, False, f"{type(e).__name__}: {e}" if not str(e) else str(e))


def format_report(report: PreflightReport) -> str:
    """Render a report for a terminal. Verdict banners are disjoint strings — 'SERVABLE' must
    not be a substring of a failure banner, or a check on stdout silently inverts."""
    lines = [f"Preflight: {report.directory}",
             f"  run_id: {report.run_id or '(unresolved)'}", ""]
    lines += [f"  {gate}" for gate in report.gates]
    if report.notes:
        lines.append("")
        lines += [f"  NOTE  {note}" for note in report.notes]
    lines.append("")
    if report.verdict is Verdict.SERVABLE:
        lines.append(f"RESULT: SERVABLE — build {contract.SERVED_CONTRACT_VERSION} can serve this run.")
    elif report.verdict is Verdict.INCOMPLETE:
        lines.append("RESULT: INCOMPLETE — every gate that ran passed, but not every gate ran. "
                     "This does NOT license arming an upload.")
    else:
        lines.append(f"RESULT: REFUSED — {len(report.failures)} gate(s) failed: "
                     f"{', '.join(g.name for g in report.failures)}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. 0 = servable, 1 = refused, 2 = incomplete (not fully checked)."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Check whether this views-crafdapi build can serve a staged wire-contract run."
    )
    parser.add_argument("directory", help="the producer staging directory holding one run")
    parser.add_argument("--no-assemble", action="store_true",
                        help="skip the assembly gate; yields INCOMPLETE (exit 2), never SERVABLE")
    args = parser.parse_args(argv)

    report = preflight(args.directory, assemble=not args.no_assemble)
    print(format_report(report), flush=True)
    return {Verdict.SERVABLE: 0, Verdict.NOT_SERVABLE: 1, Verdict.INCOMPLETE: 2}[report.verdict]


if __name__ == "__main__":  # pragma: no cover - module CLI
    import sys

    sys.exit(main())
