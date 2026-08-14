"""Which forecast run is live — the source precedence for `/provenance/forecast`.

Three sources can answer, and a caller that consults only one gets a wrong answer:

* ``served``   — the run **this worker** last served. Richest, and authoritative when present
                 (#290). ``None`` until this worker has served a forecast, so it can never be
                 the only source consulted: a fresh worker would report "no forecast" about a
                 bucket full of them.
* ``manifest`` — the newest wire-contract run manifest in the store. Worker-independent, and
                 its query names ``type`` explicitly, so the ADR-013 §11.4 guard leaves it alone.
* ``stored``   — the newest **legacy** (``type="model"``) record. This is what a type-less
                 category selection resolves, because §11.4 pins such queries to the legacy
                 type on purpose.

`crafd_bucket` is greenfield on the wire contract and holds **no** legacy documents, so
``stored`` is ``None`` here as a matter of course. Requiring it before reporting anything is
how #60 returned 404 for a run the API was serving correctly.

Precedence is **served > manifest > stored**. Callers pass all three and rank none of them.
"""
from typing import Any, Dict, Mapping, Optional


def forecast_record(
    *,
    served: Optional[Mapping[str, Any]],
    manifest_record: Optional[Mapping[str, Any]],
    stored_record: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """The forecast lineage record, or ``None`` when no forecast exists at all.

    ``manifest_record`` and ``stored_record`` arrive in ``PredictionProvenance.to_dict()``
    shape; the store owns that shape and this module does not rebuild it.

    Returns ``None`` only when all three sources are absent — the one state in which a 404
    is honest.
    """
    base = _base_record(manifest_record, stored_record)
    if base is None:
        if not served:
            return None
        base = {}

    data = dict(base)
    _apply_served(data, served)
    return data


def freshness_input(
    served: Optional[Mapping[str, Any]], data: Mapping[str, Any]
) -> Optional[str]:
    """The timestamp the freshness verdict is computed from.

    Not simply ``data["created_at"]``: the overlay below writes any non-``None`` served value,
    including ``""``. Falling back to the store's timestamp keeps ``forecast_freshness`` from
    judging against an empty string — its policy is to return an unknown verdict rather than
    assert staleness on a signal it cannot compute.
    """
    if served and served.get("created_at"):
        return served["created_at"]
    return data.get("created_at")


def _base_record(
    manifest_record: Optional[Mapping[str, Any]],
    stored_record: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    """The store-side record to build on: the manifested run if there is one, else legacy.

    Manifest wins because a manifested run is what this build serves (ADR-013 §4.3,
    manifest-first selection); a legacy record beside one describes a superseded artifact.

    Empty mappings are rejected rather than returned — ``dict(MagicMock())`` is ``{}``, and a
    test that forgets to stub a source must not thereby suppress an honest 404.
    """
    for candidate in (manifest_record, stored_record):
        if not candidate:
            continue
        record = dict(candidate)
        if record:
            return record
    return None


def _apply_served(data: Dict[str, Any], served: Optional[Mapping[str, Any]]) -> None:
    """Overlay the served run — #290's reconciliation, unchanged.

    The served run is authoritative: without this, a superseded record's labels bleed through
    and read as "still serving <old ensemble>" while a wire run is in fact live.

    **Known defect, tracked separately (not #60, deliberately not fixed here):** when the base
    record is *this same run's own manifest* rather than a superseded artifact, the full
    reconcile below overwrites correct values — the manifest's real ``targets``, ``file_hash``
    and ``filename`` — with ``None`` and a bare run id. #290 was written when the base could
    only be a superseded legacy record; #60 gave it a second possible base.
    """
    if not served:
        data["artifact_id"] = data.get("file_id")
        data["mode"] = None
        return

    data["artifact_id"] = served.get("file_id")
    data["mode"] = served.get("mode")          # "wire" | "legacy"
    data["status"] = served.get("status")      # producer-declared maturity
    data["source"] = served.get("source", data.get("source"))
    # #290: the served run is authoritative — overlay its identity/time labels too, so the
    # record is internally consistent.
    for key in ("name", "filename", "created_at", "run_id"):
        if served.get(key) is not None:
            data[key] = served[key]

    if served.get("mode") != "wire":
        return

    # #290 hardening: a wire run assembled by a pre-#290 build stored no name/filename in its
    # cached provenance (the disk cache survives restart, C-66), so after a deploy those keys
    # stay absent from `served` until a re-ingest. Reconstruct them from the run's own
    # source/run_id so a stale legacy label can never survive a wire serve.
    if served.get("name") is None and served.get("source"):
        data["name"] = served["source"]
    if served.get("filename") is None and served.get("run_id"):
        data["filename"] = served["run_id"]
    # #290 full reconcile: the served wire run owns the WHOLE record — replace the store's
    # descriptive fields so nothing from a superseded artifact bleeds through. `targets` is
    # None for a run cached by a pre-#290 build — honest-absent beats wrong.
    if served.get("file_id"):
        data["file_id"] = served["file_id"]
    data["targets"] = served.get("targets")
    data["file_hash"] = None  # a wire run is a manifest of many hashed shards, not one file
    if served.get("run_id"):
        data["description"] = f"Sampled-forecast wire-contract run {served['run_id']}"
