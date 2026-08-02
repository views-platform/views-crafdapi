"""The forecast/historical data-fetch pipeline (epic #144 / C-36, S2).

`DatasetService` owns the three-tier fetch — in-memory cache → disk cache → remote
Appwrite download → format-cascade parse → `ForecastDataset` construction →
value/metadata plausibility (C-72) → disk-cache write → provenance lineage (C-86).
It was extracted from `CrafdApiManager._get_latest_dataframe` (a 205-LOC method) so
the data pipeline has one reason to change (the fetch/cache strategy), separate
from the HTTP surface (routing) and lifecycle that remain in `CrafdApiManager`.

It is a **composed collaborator**, not a subclass: the caches it mutates are owned
by `CrafdApiManager` and injected by reference (so routes, lifecycle clears, and
cache-stats all see the same objects), along with the API-key hash and staleness
callables (single source of truth) and a config getter. Behaviour is byte-identical
to the pre-extraction method.
"""
import io
import logging
import os
import time
from collections import defaultdict
from typing import Any, Callable, Dict, Optional

import pandas as pd
from fastapi import HTTPException

from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast import contract
from views_crafdapi.forecast.ingestion import wire_reader
from views_crafdapi.managers import freshness, selection
from views_crafdapi.managers.prediction import (
    SHARD_ARTIFACT_TYPE,
    SIDECAR_ARTIFACT_TYPE,
    PredictionStoreManager,
)

logger = logging.getLogger(__name__)

# §4.6 (S4, #206) — the documented capacity bound. A wire run whose fully-assembled
# `_sample_store` (one (rows, S) float32 block per target, rows = months × cells) would
# exceed this is refused (→ fail-safe legacy) rather than materialized. Default 4 GiB: safe
# on the 16–24 GB host alongside the ~4 GB historical dataset. Production-scale serving of a
# full-S run (≈28.6 GB) is delivered by lazy per-month loading in S6 (#208), not here.
_MAX_ASSEMBLED_BYTES_ENV = "CRAFDAPI_MAX_ASSEMBLED_BYTES"
_DEFAULT_MAX_ASSEMBLED_BYTES = 4 * 1024**3
# ADR-013 §4.6 "assembled-run size × safety factor ≤ RAM": the estimate below is the FINAL
# per-target store, but assembly transiently holds the shard arrays + the per-cell object
# source frame + the freshly-stacked blocks at once (peak ≈ 2–2.5× the final store). This
# factor keeps that transient peak under the bound.
_ASSEMBLY_SAFETY_FACTOR = 3

# S5 (#207): sentinel distinguishing "the caller has not looked up the manifest" (→ _load_wire_run
# fetches it) from "the caller looked it up and there is none" (a genuine None → do not re-query).
_MANIFEST_UNFETCHED = object()


def _max_assembled_bytes() -> int:
    raw = os.getenv(_MAX_ASSEMBLED_BYTES_ENV, "")
    try:
        val = int(raw) if raw.strip() else _DEFAULT_MAX_ASSEMBLED_BYTES
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using the default %d bytes",
            _MAX_ASSEMBLED_BYTES_ENV, raw, _DEFAULT_MAX_ASSEMBLED_BYTES,
        )
        return _DEFAULT_MAX_ASSEMBLED_BYTES
    if val <= 0:
        logger.warning(
            "%s=%r is not positive; using the default %d bytes",
            _MAX_ASSEMBLED_BYTES_ENV, raw, _DEFAULT_MAX_ASSEMBLED_BYTES,
        )
        return _DEFAULT_MAX_ASSEMBLED_BYTES
    return val


def _identifiable_gate(served: selection.Served) -> selection.SelectionResult:
    """S2 (#247, ADR-033 §3): refuse to serve a run we cannot identify — no/`unknown` `source`.

    An *audit* requirement (which run, from which producer), **not** an eligibility judgment:
    a manifested wire run carries its producing-ensemble provenance, so this excludes only an
    unattributable artifact. Composable gate (ADR-033 §1); appended to the selection gate set.
    """
    source = (served.provenance or {}).get("source")
    if not source or source == "unknown":
        return selection.Refused(
            "unidentifiable",
            detail=f"served run has no identifiable source (source={source!r})",
            file_id=served.file_id,
        )
    return served


class DatasetService:
    """Fetch-and-cache the latest `ForecastDataset` for an (api_key, category)."""

    def __init__(
        self,
        *,
        dataframe_cache,
        file_cache,
        disk_cache,
        prediction_bucket_id: str,
        configs_getter: Callable[[], Dict[str, Any]],
        api_key_hash_fn: Callable[[str], str],
        check_staleness_fn: Callable[[float], Any],
    ) -> None:
        # Injected by reference — owned by CrafdApiManager, shared with routes/lifecycle.
        self._dataframe_cache = dataframe_cache
        self._file_cache = file_cache
        self._disk_cache = disk_cache
        self._prediction_bucket_id = prediction_bucket_id
        self._configs_getter = configs_getter
        self._api_key_hash = api_key_hash_fn
        self._check_staleness = check_staleness_fn
        # S4 (#249, ADR-033 §6): the last forecast-serve outcome, surfaced on /health + /provenance.
        # None until a forecast has been served; ``{"degraded": False}`` after a normal serve;
        # ``{"degraded": True, "reason", "file_id", "created_at", "age_days", "sla_days"}`` while a
        # bounded last-good grace fallback is active. Read via ``forecast_serving_state()``.
        self._forecast_serving_state: Optional[Dict[str, Any]] = None
        # S7 (#252, ADR-033 observability): the provenance of the forecast actually being served
        # (authoritative for /provenance — the store's *newest* record may differ from the served
        # run). None until the first forecast serve. Read via ``served_forecast_provenance()``.
        self._last_forecast_provenance: Optional[Dict[str, Any]] = None

    def forecast_serving_state(self) -> Optional[Dict[str, Any]]:
        """S4 (#249): the last forecast-serve outcome (see ``self._forecast_serving_state``).

        ``None`` until the first forecast serve. ``degraded=True`` means the newest manifested run
        was refused and a bounded last-good grace fallback is currently being served — /health flips
        to ``degraded`` and /provenance flags it. Cleared to ``degraded=False`` when a normal serve
        resumes (producer recovered)."""
        return self._forecast_serving_state

    def served_forecast_provenance(self) -> Optional[Dict[str, Any]]:
        """S7 (#252): provenance of the forecast currently being served — the authoritative source
        for /provenance's ``{artifact_id, mode, status, created_at, source}``. Carries ``mode``
        (``"wire"``/``"legacy"``) and, for a wire run, the producer-declared ``status`` (maturity).
        ``None`` until the first forecast serve."""
        return self._last_forecast_provenance

    def get_latest_dataframe(
        self,
        manager: PredictionStoreManager,
        x_api_key: str,
        category: str,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Get the latest dataframe from the prediction bucket, using a multi-tier cache:
        1. In-memory cache (fastest, per-worker)
        2. Disk cache (shared across workers)
        3. Remote download from Appwrite (slowest)

        Args:
            manager: The PredictionStoreManager instance
            x_api_key: The API key for cache keying
            category: Either "historical" or "forecast"
            force_refresh: Whether to force refresh the cache

        Returns:
            The latest dataframe as a pandas DataFrame
        """
        api_key_hash = self._api_key_hash(x_api_key)
        current_time = time.time()
        cache_ttl = self._disk_cache.ttl_seconds

        # Initialize in-memory cache for this API key if it doesn't exist
        if api_key_hash not in self._dataframe_cache:
            self._dataframe_cache[api_key_hash] = {
                "historical": {"data": None, "file_id": None, "timestamp": None,
                               "dataset": None, "source_kind": None},
                "forecast": {"data": None, "file_id": None, "timestamp": None,
                             "dataset": None, "source_kind": None}
            }

        cache = self._dataframe_cache[api_key_hash][category]

        # S5 (#207): the run-manifest fileId is the wire run's cache identity. Look it up ONCE
        # per forecast request (a lightweight listDocuments — no download) so a new run or an
        # operator's manifest quarantine (§4.4 run-level rollback) invalidates the cache on the
        # very next request rather than up to a TTL late. Fail-safe: a lookup blip must NOT 500
        # the warm path — it degrades to serving the existing cache (wire_identity stays None).
        # Computed for EVERY forecast request (incl. force_refresh) so the write-backs below key
        # the entry on the manifest identity uniformly — even a force_refresh that falls back to
        # legacy under a present-but-unservable manifest keys on the manifest fileId, not the
        # legacy artifact id (else the next request would needlessly re-ingest). The tier gates
        # themselves stay force_refresh-guarded, so force_refresh still bypasses warm + disk.
        manifest_doc = _MANIFEST_UNFETCHED
        wire_identity = None
        if category == "forecast":
            try:
                manifest_doc = manager.get_latest_manifest()
                if isinstance(manifest_doc, dict) and isinstance(manifest_doc.get("fileId"), str):
                    wire_identity = manifest_doc["fileId"]
            except Exception as e:
                # A lookup blip must not 500 the warm path: serve the cache (wire_identity stays
                # None), and leave manifest_doc unfetched so the miss path retries the lookup.
                logger.warning(f"Manifest lookup failed (non-blocking; serving cache): {e}")
                manifest_doc = _MANIFEST_UNFETCHED

        # C-172: the non-forecast (historical) path has no manifest to re-key on, so a cached
        # historical entry kept being served until TTL even after a newer historical artifact was
        # uploaded — stale up to the 3.5-week disk TTL. Look up the newest historical fileId once
        # (a cheap metadata query, NO download — mirrors the manifest lookup above) and use it to
        # invalidate a superseded cache entry in the warm/disk gates below. Non-blocking: a lookup
        # blip leaves `latest_legacy_id` None ⇒ serve whatever is cached.
        latest_legacy_id = None
        if category != "forecast":
            try:
                lid = manager.get_latest_file_id({"category": category})
                if isinstance(lid, str):  # only act on a real id (mirrors the manifest fileId guard)
                    latest_legacy_id = lid
            except Exception as e:
                logger.warning(f"{category} latest-file lookup failed (non-blocking; serving cache): {e}")

        # Check in-memory cache first (fastest). S5 (#207): a warm forecast is served only if
        # its identity still matches the current manifest — a changed/quarantined manifest
        # (or a vanished one, for a wire entry) falls through to re-ingest. S1 (#264): a warm
        # *forecast* entry must additionally be a WIRE entry — a legacy forecast entry is never
        # served (that would re-open the loose-artifact seam this story closes).
        if (not force_refresh and
            cache["data"] is not None and
            cache["timestamp"] is not None and
            current_time - cache["timestamp"] <= cache_ttl and
            self._forecast_entry_servable(category, cache.get("source_kind")) and
            self._identity_ok(cache, wire_identity, manifest_doc is not _MANIFEST_UNFETCHED) and
            self._legacy_identity_ok(category, cache, latest_legacy_id)):
            logger.debug(f"In-memory cache hit for {api_key_hash}/{category}")
            try:
                staleness = self._check_staleness(cache["timestamp"])
                if staleness.is_stale:
                    logger.warning(
                        f"Stale {category} prediction for {api_key_hash}: "
                        f"{staleness.age_hours:.1f}h old (threshold: {staleness.threshold_hours}h)"
                    )
            except Exception as e:
                logger.debug(f"Staleness check failed (non-blocking): {e}")
            # S4 (#249): a warm hit means the current manifest's run is what's cached — servable,
            # so any prior degraded/fallback state is cleared (e.g. a rollback to the prior run).
            if category == "forecast":
                self._forecast_serving_state = {"degraded": False}
                self._last_forecast_provenance = cache.get("provenance")  # S7 (#252)
            return cache["data"].copy()

        # Check disk cache second (shared across workers). Post-S5 (#154) a hit returns a
        # dataset reconstructed from its persisted VALUE (ForecastDataset.from_value) — no
        # pickle — so `dataset.dataframe` is the column-less metadata frame (samples live in
        # `_sample_store`), identical to the in-memory post-S4d dataset.
        # S5 (#207): if a manifest is present, the disk entry is only servable when its stored
        # fileId matches the manifest fileId (re-key via the existing check_file_id primitive).
        # A mismatch — a stale run, or a legacy entry now superseded by a manifested run — skips
        # disk and forces re-ingest. With no manifest (wire_identity None) the disk read is
        # unchanged (legacy/historical, TTL-governed). `not force_refresh` short-circuits first so
        # a forced refresh skips the disk read AND its check_file_id probe.
        # C-172: mirror the forecast re-key for historical — skip the disk read (BEFORE loading the
        # value-dir) when a newer historical file exists, via the same cheap check_file_id meta probe.
        legacy_fresh = (
            category == "forecast"
            or latest_legacy_id is None
            or self._disk_cache.check_file_id(api_key_hash, category, latest_legacy_id)
        )
        if not force_refresh and legacy_fresh and (
            wire_identity is None
            or self._disk_cache.check_file_id(api_key_hash, category, wire_identity)
        ):
            disk_cache = self._disk_cache.read(api_key_hash, category)
            # S6a (#208, C-166 §4.4 disk-half): a disk entry is servable only if it passes the
            # SAME identity guard as a warm entry. This matters once wire runs live on disk: when
            # the manifest is quarantined-to-nothing (wire_identity None), check_file_id above is
            # bypassed, so without this a stale disk WIRE entry would still be served. _identity_ok
            # on the entry's (file_id, source_kind) refuses it and falls through to re-ingest/legacy.
            if disk_cache is not None and self._forecast_entry_servable(
                category, disk_cache.get("source_kind", "legacy")
            ) and self._identity_ok(
                {"file_id": disk_cache["file_id"],
                 "source_kind": disk_cache.get("source_kind", "legacy")},
                wire_identity, manifest_doc is not _MANIFEST_UNFETCHED,
            ):
                dataset = disk_cache['dataset']
                file_id = disk_cache['file_id']

                # Update in-memory cache with the loaded dataset. S6a (#208, C-166 resolved):
                # derive source_kind + provenance from the disk meta — a disk-persisted WIRE run
                # (now possible) must NOT be mistagged "legacy" (that would defeat the §4.4
                # zero-manifest guard in _identity_ok), and its C-86 lineage must survive a restart.
                cache["data"] = dataset.dataframe
                cache["file_id"] = file_id
                cache["timestamp"] = disk_cache['timestamp']
                cache["dataset"] = dataset
                # `.get` defaults tolerate a partial/legacy meta; the real read always supplies both.
                cache["source_kind"] = disk_cache.get("source_kind", "legacy")
                cache["provenance"] = disk_cache.get("provenance")

                logger.info(f"Loaded {category} dataset from disk cache ({len(dataset.dataframe)} rows)")
                # S4 (#249): an identity-matched disk hit is a normal serve — clear degraded state.
                if category == "forecast":
                    self._forecast_serving_state = {"degraded": False}
                    self._last_forecast_provenance = cache.get("provenance")  # S7 (#252)
                return cache["data"].copy()

        # Need to download from remote
        logger.info(f"Refreshing latest {category} dataframe cache for API key: {api_key_hash}")

        # S2 (#204, ADR-013 §4.3): manifest-first wire-contract path. For forecasts, if a
        # Sampled-Forecast Wire Contract *run manifest* exists in the bucket, serve that
        # manifested run; otherwise fall through to the legacy provenance path below,
        # entirely unchanged (no-regression for existing type="model" artifacts).
        if category == "forecast":
            result = self._load_wire_run(
                manager, api_key_hash, category, cache, current_time, manifest_doc=manifest_doc
            )
            # S2 (#247, ADR-033 §2/§3): serve the newest complete manifested run for the line,
            # gated (identifiable source, §3), then act on the typed decision:
            #   Served  → serve.
            #   Refused → a manifested run exists but is NOT servable: FAIL VISIBLE. Do NOT silently
            #             serve the legacy artifact (the C-170 fix). Interim degraded form is a 5xx
            #             carrying the reason; the last-good/flagged form is S4/D3. Decision (i)
            #             (ADR-033 §2): a persistently-unservable run re-ingests + re-refuses each
            #             request (no fallback-caching); refusal-caching is deferred to S4.
            #   NoRun   → no manifested run for this line: fall through to the legacy path
            #             (transition-safe, until legacy is retired).
            result = selection.run_gates(result, (_identifiable_gate,))
            if isinstance(result, selection.Served):
                # Normal serve — clear any prior degraded/fallback state (producer recovered).
                self._forecast_serving_state = {"degraded": False}
                self._last_forecast_provenance = result.provenance  # S7 (#252): served-run lineage
                return result.dataframe
            if isinstance(result, selection.Refused):
                logger.error(
                    "Wire run refused (reason=%s, file_id=%s): %s",
                    result.reason, result.file_id, result.detail,
                )
                # S4 (#249, ADR-033 §6, D-24): bounded, alarmed grace fallback. If a previously
                # served manifested run is still on disk AND within the freshness SLA, serve it —
                # loudly (degraded provenance + WARNING log), never silently. A failed ingest of the
                # newest run discards only its staging dir, leaving the last-good disk entry intact
                # (§4.6 / S6b-2). Past the SLA (or no last-good at all) → fail visible.
                served = self._serve_last_good_within_sla(api_key_hash, category, result.reason)
                if served is not None:
                    return served
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"No servable forecast: wire run refused ({result.reason}) "
                        "and no last-good run within the freshness SLA"
                    ),
                )
            # `NoRun` → no manifested run exists. S1 (#264, ADR-033 §2): a forecast NEVER falls back
            # to a loose legacy artifact (that seam is where a stale placeholder could be served). It
            # serves the last-good MANIFESTED run within the freshness SLA (bounded, alarmed grace),
            # else fails visible (503). Only `/historical` still uses the legacy path below.
            if isinstance(result, selection.NoRun):
                served = self._serve_last_good_within_sla(api_key_hash, category, "no_manifest")
                if served is not None:
                    return served
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "No current forecast: no manifested run is available and no last-good run "
                        "within the freshness SLA. Refusing to serve a legacy artifact."
                    ),
                )

        # The legacy provenance path. Post-S1 (#264) this is reached ONLY for `category !=
        # "forecast"` (i.e. `/historical`): the forecast block above handles every SelectionResult
        # (Served/Refused/NoRun) with a return-or-raise and never falls through, so a forecast can
        # no longer be served from a loose legacy artifact. Historical still arrives as loose files
        # until the producer co-delivers it on the wire path (epic #263 S5 / C-169).
        # Get the latest file from the prediction bucket with category filter.
        # Resolve provenance (C-86) in the same query: this records *which* artifact and
        # *which upstream source/pipeline* is being brought into service.
        filters = {"category": category}
        provenance = manager.get_latest_provenance(filters=filters)
        if not provenance:
            raise HTTPException(
                status_code=404,
                detail=f"No {category} prediction files found in the bucket: {self._prediction_bucket_id}"
            )
        file_id = provenance.file_id

        # C-86: emit a lineage record so a silent upstream source switch (viewser→datafactory)
        # is visible. `source=unknown` means the producer did not stamp a provenance field.
        logger.info(
            f"Serving {category} provenance: source={provenance.source} "
            f"methodology={provenance.methodology_version} file_id={file_id} "
            f"hash={provenance.file_hash} created={provenance.created_at} filename={provenance.filename}"
        )

        logger.info(f"Downloading latest {category} file with ID: {file_id}")

        # Download bytes via the shared helper (also used by the wire path) — one
        # implementation of the file-cache check + download + empty guard.
        file_bytes = self._download_file_bytes(manager, file_id, current_time)

        # Try to read with different formats and encodings
        df = None
        errors = []

        # Try parquet first (common for prediction data)
        try:
            df = pd.read_parquet(io.BytesIO(file_bytes))
            logger.info("Successfully read file as parquet")
        except Exception as e:
            errors.append(f"Parquet: {str(e)}")
            if file_bytes[:4] == b"PAR1":
                err_msg = f"File {file_id} has parquet header but failed to parse: {e}"
                logger.error(err_msg)
                raise HTTPException(status_code=500, detail=err_msg)

        # Try CSV with utf-8 only — latin-1/iso-8859-1/cp1252 accept any byte
        # sequence, which masks format detection failures and produces garbage.
        if df is None:
            try:
                df = pd.read_csv(io.BytesIO(file_bytes), encoding='utf-8')
                logger.info("Successfully read file as CSV with utf-8 encoding")
            except Exception as e:
                errors.append(f"CSV (utf-8): {str(e)}")

        # Try JSON
        if df is None:
            try:
                df = pd.read_json(io.BytesIO(file_bytes))
                logger.info("Successfully read file as JSON")
            except Exception as e:
                errors.append(f"JSON: {str(e)}")

        # Pickle is intentionally NOT in the cascade: pd.read_pickle() uses Python's
        # pickle.load(), which executes arbitrary code during deserialization — a remote
        # code-execution path on untrusted Appwrite bytes (register C-59). No production
        # file uses pickle (all are parquet), so it is excluded entirely.

        # Try feather
        if df is None:
            try:
                df = pd.read_feather(io.BytesIO(file_bytes))
                logger.info("Successfully read file as feather")
            except Exception as e:
                errors.append(f"Feather: {str(e)}")

        # If all attempts failed, raise an error with details
        if df is None:
            error_details = "\n".join(errors)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to parse file. Attempted formats:\n{error_details}"
            )

        # Memory: downcast the low-cardinality geography name/ISO3 columns to `category` and
        # release the raw file bytes BEFORE building the dataset. Placement is load-bearing for
        # peak RAM on the global historical (~28M rows): held as object strings these four
        # columns cost ~6-7 GB, and ForecastDataset then builds ~10 GB of per-cell target
        # arrays — casting here frees the string block so the two large transients never
        # co-exist. That is the difference between fitting the 24 GB box and OOM-killing the
        # worker on a cold historical load. (ForecastDataset.__init__ repeats the cast as an
        # idempotent safety net; here it is a no-op.) The `*_code` columns stay numeric.
        for _col in ForecastDataset._CATEGORICAL_METADATA_COLS:
            if _col in df.columns and df[_col].dtype == object:
                df[_col] = df[_col].astype("category")
        del file_bytes

        # Create a ViewsDataset from the dataframe
        try:
            if category == "historical":
                targets = self._configs_getter().get("historical_targets")
                if not targets:
                    index_cols = {"month_id", "priogrid_id", "priogrid_gid"}
                    meta_cols = set(ForecastDataset._METADATA_COLS)
                    targets = [c for c in df.columns if c not in index_cols | meta_cols]
                    logger.info(f"Auto-detected historical targets: {targets}")
                dataset = ForecastDataset(df, targets=targets)
            else:
                dataset = ForecastDataset(df)
            # C-72: reject schema-valid-but-implausible values before they are cached and
            # served to FAO — prediction values (non-finite / negative) and geographic
            # metadata (out-of-range coordinates, malformed ISO3, negative GAUL codes).
            dataset.validate_value_plausibility()
            dataset.validate_metadata_plausibility()
        except Exception as e:
            logger.error(f"Failed to create ViewsDataset: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to process downloaded data into a valid dataset."
            )

        # Write processed dataset to disk cache for other workers to use. Keyed on the legacy
        # ARTIFACT file_id (not the manifest identity): when a manifest is present the disk gate
        # deliberately misses (check_file_id vs the manifest fileId) so a cold worker re-attempts
        # the wire ingest rather than serving a persisted legacy fallback (correct for the S6
        # transition, when a now-servable run must win over a stale legacy disk entry).
        self._disk_cache.write(
            api_key_hash, category, dataset, file_id,
            source_kind="legacy", provenance=provenance.to_dict(),
        )

        # Update in-memory cache with the processed dataframe from the dataset. S5 (#207): when a
        # manifest is present but its run was NOT servable (over-capacity, malformed, or missing
        # artifacts → _load_wire_run returned `Refused`/`NoRun`), key the warm entry on the MANIFEST
        # fileId, not
        # the legacy artifact id — otherwise the next request's identity check (which compares to
        # the manifest fileId) misses and the legacy forecast is re-downloaded on EVERY request.
        cache["data"] = dataset.dataframe
        cache["file_id"] = wire_identity if wire_identity is not None else file_id
        cache["timestamp"] = current_time
        cache["dataset"] = dataset
        cache["source_kind"] = "legacy"  # S5 (#207): powers the §4.4 zero-manifest guard
        cache["provenance"] = provenance.to_dict()  # C-86: lineage of the served artifact

        logger.info(f"Successfully loaded {category} dataframe with {len(dataset.dataframe)} rows")

        # S1 (#264): the legacy path is `/historical`-only now — a forecast never reaches here (it is
        # served from a manifested run or fails visible above), so no forecast serving-state is set.

        return cache["data"].copy()

    def _serve_last_good_within_sla(
        self, api_key_hash: str, category: str, reason: str
    ) -> Optional[pd.DataFrame]:
        """S4 (#249, ADR-033 §6, D-24) + S1 (#264): the bounded, alarmed grace fallback.

        There is no currently-servable manifested run — either the newest run was ``Refused``
        (``reason`` = the refusal reason) or **no manifest exists at all** (``reason="no_manifest"``,
        S1: forecast no longer falls through to a legacy artifact). If a previously-served
        **manifested** run is still on disk AND still within the freshness SLA (S3), serve it —
        bounding a transient gap without going dark — but *loudly*: a WARNING alarm and a degraded
        serving-state that flips /health to ``degraded`` and flags /provenance. Returns the last-good
        dataframe, or ``None`` to fail visible (past the SLA, unknown age, or no manifested last-good).

        Deliberately does **not** repopulate the warm in-memory cache: every request re-evaluates the
        newest run (decision (i), ADR-033 §2), so recovery is picked up immediately and the fallback
        never becomes sticky. A failed ingest discards only its staging dir (§4.6), so the last-good
        value-dir this reads is intact.
        """
        try:
            disk = self._disk_cache.read(api_key_hash, category)
        except Exception as e:  # a corrupt/unreadable last-good must not mask the refusal
            logger.warning("Last-good disk read failed during grace fallback: %s", e)
            disk = None
        # Only a manifested (wire) last-good qualifies — never fall back to a legacy artifact
        # (that is the C-170 hole S2 closed). No last-good ⇒ fail visible.
        if disk is None or disk.get("source_kind") != "wire":
            self._forecast_serving_state = {
                "degraded": True, "reason": reason, "fallback_available": False,
            }
            return None
        prov = dict(disk.get("provenance") or {})
        verdict = freshness.forecast_freshness(prov.get("created_at"), freshness.freshness_sla_days())
        # Serve the last-good ONLY if we can positively confirm it is fresh (is_stale is False).
        # Stale (True) or unknown (None) ⇒ fail visible: we never serve a possibly-outdated forecast
        # under a fallback we cannot bound.
        if verdict.get("is_stale") is not False:
            logger.error(
                "Grace fallback refused: last-good manifested run %s is not within the %.0f-day "
                "freshness SLA (verdict=%s); failing visible",
                disk.get("file_id"), verdict.get("sla_days"), verdict,
            )
            self._forecast_serving_state = {
                "degraded": True, "reason": reason, "fallback_available": False,
                "last_good_file_id": disk.get("file_id"),
                "last_good_age_days": verdict.get("age_days"), "sla_days": verdict.get("sla_days"),
            }
            return None
        dataset = disk["dataset"]
        self._forecast_serving_state = {
            "degraded": True,
            "reason": reason,
            "fallback_available": True,
            "serving": "last_good_manifested",
            "file_id": disk.get("file_id"),
            "created_at": prov.get("created_at"),
            "age_days": verdict.get("age_days"),
            "sla_days": verdict.get("sla_days"),
        }
        # S7 (#252): the served lineage is the last-good manifested run (degraded fallback).
        self._last_forecast_provenance = {**prov, "mode": prov.get("mode", "wire")}
        logger.warning(
            "DEGRADED: newest manifested run refused (%s); serving last-good run %s "
            "(%.1f days old, within %.0f-day SLA) as a bounded grace fallback",
            reason, disk.get("file_id"), verdict.get("age_days"), verdict.get("sla_days"),
        )
        return dataset.dataframe.copy()

    @staticmethod
    def _forecast_entry_servable(category: str, source_kind: Optional[str]) -> bool:
        """S1 (#264): may a cached entry (warm or disk) of `category` be served?

        A **forecast** is served from cache only when the entry is a WIRE run — a legacy forecast
        cache entry (e.g. a pre-deploy `orange_ensemble` on disk) is never served, so the guarantee
        "a forecast is only ever a manifested run, else fail-visible" holds on the cache tiers too,
        not just on a cold ingest. Any non-forecast category (`/historical`) is unaffected."""
        if category != "forecast":
            return True
        return source_kind == "wire"

    @staticmethod
    def _identity_ok(cache: Dict[str, Any], wire_identity, manifest_known: bool) -> bool:
        """S5 (#207): may the warm in-memory entry still be served given the current wire
        identity?

        - ``manifest_known`` False (the lookup threw — a blip): we could not determine the
          identity, so degrade to serving whatever is warm (TTL still governs). Never drop a
          good entry because the metadata query flickered.
        - a manifest is present (``wire_identity`` set): the entry must carry that exact fileId.
        - the lookup confirmed NO manifest (``wire_identity`` None, ``manifest_known`` True): a
          legacy/historical entry serves on TTL, but a stale WIRE entry is refused (§4.4:
          manifest gone/quarantined ⇒ do not keep serving the old run)."""
        if not manifest_known:
            return True
        if wire_identity is not None:
            return cache.get("file_id") == wire_identity
        return cache.get("source_kind") != "wire"

    @staticmethod
    def _legacy_identity_ok(category: str, cache: Dict[str, Any], latest_file_id) -> bool:
        """C-172: may a cached NON-forecast (historical) entry still be served?

        Historical has no manifest to re-key on (`_identity_ok` above governs the wire path), so
        without this an entry was served until TTL even after a newer historical artifact was
        uploaded. Serve the entry only while it is still the newest historical file. ``latest_file_id``
        None (the lookup threw, or category is forecast) ⇒ do not drop the entry (non-blocking / not
        our concern); otherwise the entry must carry the newest fileId."""
        if category == "forecast" or latest_file_id is None:
            return True
        return cache.get("file_id") == latest_file_id

    def _download_file_bytes(
        self, manager: PredictionStoreManager, file_id: str, current_time: float
    ) -> bytes:
        """Download one artifact's bytes by file-id, via the shared in-memory file cache."""
        if file_id in self._file_cache:
            logger.info(f"Using in-memory file cache: {file_id}")
            return self._file_cache[file_id]["data"]
        result = manager.download_prediction(file_id)
        if not result.success:
            raise HTTPException(status_code=500, detail=f"Failed to download {file_id}: {result.error}")
        data = result.data if isinstance(result.data, dict) else {}
        file_bytes = data.get("file_bytes")
        if not file_bytes:
            raise HTTPException(status_code=500, detail=f"Downloaded file {file_id} is empty")
        self._file_cache[file_id] = {"data": file_bytes, "timestamp": current_time}
        return file_bytes

    def _guard_run_capacity(self, manifest, first_shard_state) -> None:
        """§4.6 (S4): refuse a run whose fully-assembled ``_sample_store`` would exceed the
        documented capacity bound. Estimated from the manifest (targets × months × cells) and
        the first shard's S; raises (→ fail-safe legacy) before the rest is downloaded."""
        if manifest.expected_cell_count is None or not manifest.expected_months or not manifest.targets:
            return  # cannot estimate; correctness asserts still bound the run
        s = int(first_shard_state["values"].shape[1])
        # The estimate is the FINAL per-target store; assembly transiently holds the shard
        # arrays + the per-cell object source frame + the freshly-stacked blocks at once, so
        # the peak is a multiple of this. ADR-013 §4.6 mandates a safety factor — apply it so
        # the bound reflects the transient assembly peak, not just the resident store.
        # S6b-2 (#208): the run is assembled to disk ONE MONTH at a time, so the peak is one
        # month's working set (targets × cells × S × 4), not the whole run — drop the n_months
        # factor. A full-scale run's per-month set (~0.8 GB) is well under the 4 GiB default, so
        # it now serves instead of being refused; the bound still catches a pathological month.
        est = wire_reader.estimate_assembled_bytes(
            len(manifest.targets), 1, manifest.expected_cell_count, s
        )
        peak = est * _ASSEMBLY_SAFETY_FACTOR
        cap = _max_assembled_bytes()
        if peak > cap:
            raise ValueError(
                f"per-month working set ~{est / 1024**3:.2f} GiB (×{_ASSEMBLY_SAFETY_FACTOR} "
                f"peak ~{peak / 1024**3:.2f} GiB) exceeds the capacity bound "
                f"{cap / 1024**3:.2f} GiB (targets={len(manifest.targets)}, "
                f"cells={manifest.expected_cell_count}, S={s}) — "
                f"set {_MAX_ASSEMBLED_BYTES_ENV} to override"
            )

    def _load_wire_run(
        self,
        manager: PredictionStoreManager,
        api_key_hash: str,
        category: str,
        cache: Dict[str, Any],
        current_time: float,
        manifest_doc: Any = _MANIFEST_UNFETCHED,
    ) -> selection.SelectionResult:
        """S2 (#204): serve the latest manifested Sampled-Forecast Wire Contract run.

        Returns a typed ``selection.SelectionResult`` (S1/#245, ADR-033 §1): ``Served`` on
        success, ``Refused(reason)`` for any reason the run cannot be served, or ``NoRun``
        when no manifested run exists. The caller serves ``Served`` and falls back to the
        legacy path on ``Refused``/``NoRun`` (behaviour-preserving), with the reason captured.

        **Fail-safe (code-review 2026-07-20):** a ``Refused``/``NoRun`` result is not only "no
        manifest" — it is *any* reason the wire run cannot be served. Once a manifest is detected,
        the entire ingest is guarded; a malformed manifest, an absent shard/sidecar, or a
        build/validate error all LOG and return ``Refused(reason)`` so the UN-facing forecast
        endpoint keeps serving the last-good legacy forecast instead of 500-ing the whole fleet
        during a producer's upload window.

        S3 (#205) adds the remaining fail-loud integrity asserts (sample_count-vs-manifest,
        shard content-hash, header-vs-manifest target/time_id); the §4.5(b) sample-ordering
        guard is enforced in ``wire_reader.load_shard_state`` already. S4 (#206) generalizes
        to multi-shard assembly; S5 (#207) re-keys the cache on the manifest file-id.

        S5 (#207): the caller passes the ``manifest_doc`` it already fetched (to re-key the
        cache) so we don't query twice — including a genuine ``None`` (no manifest). Only the
        ``_MANIFEST_UNFETCHED`` sentinel (force_refresh, or a lookup blip) triggers a fetch here.
        """
        if manifest_doc is _MANIFEST_UNFETCHED:
            try:
                manifest_doc = manager.get_latest_manifest()
            except Exception as e:
                # Cold-cache manifest-lookup blip: degrade to legacy rather than 500.
                logger.warning(f"Manifest lookup failed: {e}")
                return selection.Refused("manifest_lookup_error", detail=str(e))
        # A real store manifest is a dict (or None); anything else ⇒ no manifested run.
        if not isinstance(manifest_doc, dict):
            return selection.NoRun()
        manifest_file_id = manifest_doc.get("fileId")
        if not isinstance(manifest_file_id, str):
            return selection.Refused(
                "manifest_malformed", detail="run manifest has no string fileId"
            )

        # A manifest exists — attempt to serve it, but transition-safely: ANY failure below
        # falls back to legacy (return None) rather than raising to the endpoint.
        staging = None  # the wire assembler's on-disk staging dir; cleaned in `finally`
        try:
            logger.info(f"Ingesting manifested wire-contract run (manifest file_id={manifest_file_id})")
            manifest = wire_reader.parse_run_manifest(
                self._download_file_bytes(manager, manifest_file_id, current_time)
            )
            # S5 (#250, ADR-033 §7, C-171): deploy/serve capability gate. Refuse — loudly, before
            # assembling ~GBs — a run whose declared wire-contract dialect this build cannot render,
            # rather than serve it in a degraded/old schema. `Refused` → 503 (S2) or last-good grace
            # (S4); never a silent degraded serve. Cheap: knowable from the manifest alone.
            if not contract.can_render_contract(manifest.contract_version):
                return selection.Refused(
                    "schema_capability_mismatch",
                    detail=(
                        f"run {manifest.run_id!r} declares contract_version="
                        f"{manifest.contract_version!r}; this build serves "
                        f"{contract.SERVED_CONTRACT_VERSION!r}"
                    ),
                    file_id=manifest_file_id,
                )
            shard_ids = manager.resolve_artifact_file_ids(manifest.shard_names, SHARD_ARTIFACT_TYPE)
            sidecar_ids = manager.resolve_artifact_file_ids([manifest.sidecar_name], SIDECAR_ARTIFACT_TYPE)
            missing = [n for n in manifest.shard_names if n not in shard_ids]
            if missing or manifest.sidecar_name not in sidecar_ids:
                raise ValueError(
                    f"manifested run {manifest.run_id!r} references artifacts absent from the bucket "
                    f"(shards missing: {missing}; sidecar present: {manifest.sidecar_name in sidecar_ids})"
                )
            # §4.5(c): verify each artifact's content hash against the manifest BEFORE trusting
            # its bytes (fail-loud → legacy fallback). §4.6 (S6b-2, #208): assemble the run to a
            # disk value-dir ONE MONTH at a time — peak RAM is one month's dataset (+ one in-flight
            # shard), never the whole ~28.6 GB run — then serve it back mmap'd (S6b-1). The
            # capacity guard (now per-month) still fires after the first shard reveals S.
            sidecar_bytes = self._download_file_bytes(
                manager, sidecar_ids[manifest.sidecar_name], current_time
            )
            wire_reader.verify_content_hash(sidecar_bytes, manifest.sidecar_sha256, "sidecar")
            sidecar_df = wire_reader.read_sidecar(sidecar_bytes)

            shards_by_month = defaultdict(list)
            for entry in manifest.shards:
                shards_by_month[entry["time_id"]].append(entry)

            staging = self._disk_cache.staging_dir(api_key_hash, category)
            assembler = wire_reader.WireRunAssembler(staging, manifest, sidecar_df)
            first_state = None
            for month in sorted(shards_by_month):
                month_shards = shards_by_month[month]
                month_states = []
                for entry in month_shards:
                    name = entry["name"]
                    shard_bytes = self._download_file_bytes(manager, shard_ids[name], current_time)
                    wire_reader.verify_content_hash(shard_bytes, manifest.sha256_for(name), f"shard {name}")
                    state = wire_reader.load_shard_state(shard_bytes)
                    if first_state is None:
                        first_state = state
                        self._guard_run_capacity(manifest, state)
                    month_states.append(state)
                    # Hold the per-month peak: once a shard is loaded, drop its bytes from the
                    # in-memory file cache — else every shard stays resident and defeats the goal.
                    self._file_cache.pop(shard_ids[name], None)
                assembler.append_month(month_states, month_shards, month)
            rows = assembler.finalize()

            # Adopt the assembled value-dir into the cache slot (keyed on the manifest fileId,
            # S5/S6a) and read it back MMAP'd (S6b-1) — serving then pages per month.
            # S2 (#247, ADR-033 §2/§8): surface the run's declared identity/status. `status` is
            # producer-declared maturity (vpp ADR-013 / views-postprocessing#133); `None` until the
            # producer stamps it. faoapi *surfaces* it (informational) — it is never a serving gate.
            wire_prov = (first_state.get("metadata") or {}).get("provenance") or {}
            provenance = {
                "file_id": manifest_file_id,
                "run_id": manifest.run_id,
                "contract_version": manifest.contract_version,
                "source": wire_prov.get("ensemble", "unknown"),
                "mode": "wire",
                "status": wire_prov.get("status"),
                # #290: carry the served run's own identity labels so /provenance can overlay them
                # and stay internally consistent — otherwise the store's newest LEGACY record's
                # `name`/`filename` bleed through (e.g. "orange_ensemble") while a wire run is live.
                "name": wire_prov.get("ensemble", "unknown"),
                "filename": manifest_doc.get("filename") or manifest.run_id,
                "targets": manifest.targets,
                # S4 (#249, ADR-033 §4/§6): freshness signal for /health, /provenance and the
                # bounded last-good fallback. The manifest is uploaded LAST (vpp ADR-013 §11.4),
                # so its `$createdAt` marks run completion; the producer's own `created_at` (if
                # stamped in the sidecar provenance) takes precedence.
                "created_at": wire_prov.get("created_at") or manifest_doc.get("$createdAt"),
            }
            if not self._disk_cache.write_value_dir(
                api_key_hash, category, assembler.out_dir, manifest_file_id,
                rows=rows, columns=[], source_kind="wire", provenance=provenance,
            ):
                raise RuntimeError("failed to adopt the assembled wire value-dir into the cache")
            disk = self._disk_cache.read(api_key_hash, category)
            if disk is None:
                raise RuntimeError("assembled wire value-dir not readable after adopt")
            dataset = disk["dataset"]
        except Exception as e:
            # Fail-safe: any ingest failure — a failed integrity assert, an over-capacity run
            # (§4.6), a non-rectangular run, a malformed manifest — falls back to legacy serving
            # rather than 500-ing the UN-facing endpoint. Loud in logs; the bad run is refused.
            logger.error(
                f"Wire-contract ingest failed for manifest {manifest_file_id}: {e}",
                exc_info=True,
            )
            return selection.Refused("ingest_failed", detail=str(e), file_id=manifest_file_id)
        finally:
            # On success `write_value_dir` has moved the staging dir into the cache slot (so this
            # is a no-op); on ANY failure this reclaims the full-N preallocated staging dir.
            if staging is not None:
                self._disk_cache.discard_staging(staging)

        # Success. The run is persisted to disk (restart survival, C-66) and served mmap'd — no
        # whole-run dataset is ever held in RAM (S6b-2 dissolves the §4.6 ingest wall).
        cache["data"] = dataset.dataframe
        cache["file_id"] = manifest_file_id
        cache["timestamp"] = disk["timestamp"]
        cache["dataset"] = dataset
        cache["source_kind"] = "wire"  # S5 (#207): powers the §4.4 zero-manifest guard
        cache["provenance"] = provenance
        logger.info(
            f"Served manifested wire run {manifest.run_id!r}: {len(dataset.dataframe)} rows, "
            f"targets={dataset.targets}"
        )
        return selection.Served(
            dataframe=cache["data"].copy(),
            dataset=dataset,
            file_id=manifest_file_id,
            mode="wire",
            provenance=provenance,
        )

    def get_latest_dataset(
        self,
        manager: PredictionStoreManager,
        x_api_key: str,
        category: str,
        force_refresh: bool = False,
    ) -> ForecastDataset:
        """
        Get the latest ViewsDataset from the prediction bucket, using a per-API-key cache.

        Args:
            manager: The PredictionStoreManager instance
            x_api_key: The API key for cache keying
            category: Either "historical" or "forecast"
            force_refresh: Whether to force refresh the cache

        Returns:
            The latest ViewsDataset
        """
        api_key_hash = self._api_key_hash(x_api_key)

        # Ensure the dataframe is loaded
        self.get_latest_dataframe(manager, x_api_key, category, force_refresh)

        return self._dataframe_cache[api_key_hash][category]["dataset"].copy()
