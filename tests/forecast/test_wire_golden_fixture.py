"""S8 (#100) — the producer's golden wire-contract fixture as faoapi's ingest oracle.

This vendors the run-0 skeleton fixture emitted by views-postprocessing's Hop-B sink
(`tests/fixtures/wire_contract/`, ADR-013 §10.1) and pins it two ways:

1. **Tree integrity** — the root hash `sha256(SHA256SUMS)` is pinned to the value the
   producer publishes, and every file listed in SHA256SUMS is checked against its digest.
   A re-vendor that changes any byte fails loudly here (re-confirm with the producer, then
   update `_ROOT_HASH`).
2. **Ingest→serve on the real bytes** — faoapi parses the manifest, verifies each §4.5(c)
   content hash against the *actual* arrow/sidecar bytes it will download in production,
   loads the shard (§4.5(a) count + §4.5(b) `sample`-ordering guards), joins the sidecar,
   and serves `calculate_hdi_map`. This is the format-compatibility contract proven against
   the producer's emitter, not against self-generated synthetic shards.

Scope note: the fixture is single-target / single-month, so it does NOT exercise the
multi-target / multi-month `WireRunAssembler` path (run-0 proper is 3 targets × 36 months).
That assembly is covered against the whole-run oracle in `test_wire_assembler.py`; the
producer additionally unit-tests ragged-run refusal before upload.
"""

import hashlib
from pathlib import Path

import pytest

from views_crafdapi.forecast.ingestion import wire_reader

pytestmark = pytest.mark.layer2_data

_FIXTURE_DIR = Path(__file__).parent / "golden" / "wire_contract"
_SUMS = _FIXTURE_DIR / "SHA256SUMS"

# The producer's published tree hash = sha256 of the SHA256SUMS file, which lists the
# per-file digests of every fixture artifact. Pinning this one value pins the whole tree.
# Re-vendor + update ONLY after the producer confirms a new fixture (views-postprocessing
# tests/fixtures/wire_contract/, ADR-013 §10.1).
_ROOT_HASH = "9658a6484cc9d975412e52624d52f328985f14cf58e3fc9fbdf3e64ab5a0564b"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(name: str) -> bytes:
    return (_FIXTURE_DIR / name).read_bytes()


def _sums_entries():
    """(sha256, filename) pairs parsed from the vendored SHA256SUMS."""
    out = []
    for line in _SUMS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        digest, name = line.split(maxsplit=1)
        out.append((digest, name.strip()))
    return out


def test_golden_root_hash_pinned():
    """The vendored fixture tree matches the producer's published root hash."""
    assert _sha256(_SUMS.read_bytes()) == _ROOT_HASH, (
        "vendored wire-contract fixture drifted from the producer's published tree; "
        "re-confirm with views-postprocessing and update _ROOT_HASH"
    )


def test_golden_files_match_manifest_of_hashes():
    """Every file listed in SHA256SUMS is present and byte-identical to its digest."""
    entries = _sums_entries()
    assert entries, "SHA256SUMS is empty — fixture vendoring is broken"
    for expected, name in entries:
        path = _FIXTURE_DIR / name
        assert path.exists(), f"fixture file listed in SHA256SUMS is missing: {name}"
        assert _sha256(path.read_bytes()) == expected, f"digest mismatch for {name}"


def test_faoapi_ingests_and_serves_the_real_producer_bytes():
    """End-to-end: parse → §4.5 hash/verify → load → sidecar join → serve, on real bytes."""
    manifest = wire_reader.parse_run_manifest(_read("fixture_run_0__manifest.json"))
    assert manifest.run_id == "fixture_run_0"
    assert manifest.expected_cell_count is not None and manifest.expected_cell_count > 0

    states = []
    for shard in manifest.shards:
        raw = _read(shard["name"])
        # §4.5(c): faoapi's hash of the exact bytes it downloads == the manifest's sha256.
        wire_reader.verify_content_hash(
            raw, manifest.sha256_for(shard["name"]), f"shard {shard['name']}"
        )
        # §4.5(a) sample-count + §4.5(b) `sample`-ordering guards run inside load_shard_state.
        states.append(wire_reader.load_shard_state(raw))

    assert manifest.sidecar_name, "manifest is missing its sidecar entry"
    sidecar_bytes = _read(manifest.sidecar_name)
    wire_reader.verify_content_hash(sidecar_bytes, manifest.sidecar_sha256, "sidecar")
    sidecar = wire_reader.read_sidecar(sidecar_bytes)

    dataset = wire_reader.build_dataset(states, sidecar, manifest)
    # Plausibility must not fail-loud on conformant producer output.
    dataset.validate_value_plausibility()
    dataset.validate_metadata_plausibility()

    served = dataset.calculate_hdi_map()
    assert len(served) == manifest.expected_cell_count
    # Aggregation to a coarser level must also serve (exercises the geo collapse).
    aggregated = dataset.calculate_hdi_map(aggregate=True, level="country")
    assert len(aggregated) >= 1
