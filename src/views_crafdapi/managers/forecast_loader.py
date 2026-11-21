"""The service loads the forecast itself, before any partner asks (#208, ADR-044).

At start, and then every few minutes, the loader asks the store whether a new forecast or new
recorded deaths have been delivered. When they have, it loads them with the server's own read
key, builds the nine bulk files, and only then hands the new data to the routes in one step.
Partners are always answered from data that is already loaded; while nothing has been loaded yet
they get a 503.

The loading runs in a worker thread, so requests keep being answered meanwhile. The routes run on
the event loop. The two must never use the same pandas objects at the same time: a pandas index
builds its lookup table on first use, and two threads doing that at once read a half-built table
(measured: 8 wrong answers in 30, #224). So the thread hands over datasets read freshly from the
disk cache, which nothing on the event loop has touched, and after the hand-over it never uses
them again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from views_crafdapi.forecast.serialize import outcome_table
from views_crafdapi.forecast.serialize.bulk_file import write_bulk_file

logger = logging.getLogger(__name__)

# How often the store is asked whether something new was delivered. A delivery arrives once a
# month, so a few minutes' delay is invisible; each check is two small metadata queries.
DEFAULT_CHECK_MINUTES = 5.0

# The server's read key, `crafdapi-server` in the platform registry (views-appwrite). systemd reads
# it from a file of its own, apart from the other APPWRITE_* settings (ADR-044).
SERVER_KEY_ENV = "APPWRITE_CRAFDAPI_SERVER_API_KEY"


def read_server_key() -> Optional[str]:
    """The server's read key, or None, logged, when it is not set: the service then starts but
    never loads, and answers 503. The key itself is never logged."""
    key = (os.getenv(SERVER_KEY_ENV) or "").strip()
    if not key:
        logger.critical("%s is not set: the forecast cannot be loaded", SERVER_KEY_ENV)
        return None
    return key


def check_minutes() -> float:
    """Minutes between checks, from `CRAFDAPI_CHECK_MINUTES`: 1 to 60, else the default."""
    raw = os.getenv("CRAFDAPI_CHECK_MINUTES")
    try:
        value = float(raw) if raw else DEFAULT_CHECK_MINUTES
    except ValueError:
        return DEFAULT_CHECK_MINUTES
    return value if 1.0 <= value <= 60.0 else DEFAULT_CHECK_MINUTES


# A delivery that could not be served completely (the newest run refused, or no history) is tried
# again at most this often, not at every check: a refused run is several gigabytes to download.
RETRY_SECONDS = 3600.0


@dataclass(frozen=True)
class Served:
    """What the routes answer from: one forecast and, if it could be loaded, its history, with
    what /health reports about them, all taken at the hand-over."""

    forecast: object
    history: Optional[object]
    loaded_at: float
    provenance: Optional[dict] = None      # the served run's record (its creation time)
    entry: Optional[dict] = None           # which build wrote the served disk slot
    serving_state: Optional[dict] = None   # degraded or not, when it was handed over


class ForecastLoader:
    """Loads with the server key and hands over complete, fresh data to the routes."""

    def __init__(self, dataset_service, disk_cache, bulk_files,
                 server_key: Optional[str], make_manager: Callable[[str], object]):
        self._service = dataset_service
        self._disk_cache = disk_cache
        self._bulk_files = bulk_files
        self._key = server_key
        self._make_manager = make_manager
        self._served: Optional[Served] = None
        self._seen: Optional[tuple] = None        # the delivery ids last handed over
        self._retry_at: Optional[float] = None    # when an incomplete hand-over is tried again
        self._stopping = threading.Event()
        self._failed_at: Optional[float] = None   # the last check that could not load, if failing

    def served(self) -> Optional[Served]:
        """The data the routes answer from, or None before the first complete load. Read on the
        event loop; replaced in one assignment by the loader."""
        return self._served

    def failing_since(self) -> Optional[float]:
        """When checks started failing to load what was delivered, or None. /health reports it:
        partners are still served the data already loaded, but the newest delivery is not in it."""
        return self._failed_at

    def stop(self) -> None:
        """Ask a running load to stop at its next step (at shutdown)."""
        self._stopping.set()

    def refresh(self) -> bool:
        """Load what is new, if anything. Returns True when new data was handed over.

        Never raises: a failure is logged, the data already served stays, and the next check
        tries again.
        """
        if self._key is None or self._stopping.is_set():
            return False
        try:
            manager = self._make_manager(self._key)
            delivered = self._delivered_ids(manager)
            if self._served is not None and delivered == self._seen and (
                    self._retry_at is None or time.time() < self._retry_at):
                return False
            forecast = self._service.get_latest_dataset(manager, self._key, "forecast")
            try:
                history = self._service.get_latest_dataset(manager, self._key, "historical")
            except Exception as e:   # the forecast is still served; slices of past months answer 503
                logger.error("loader: the recorded deaths could not be loaded: %s", e)
                history = None
            forecast, history = self._fresh(forecast, "forecast"), self._fresh(history, "historical")
            if forecast is None or self._stopping.is_set():
                return False
            self._build_bulk_files(forecast, history)
            if self._stopping.is_set():
                return False
            state = dict(self._service.forecast_serving_state() or {})
            previous = self._served
            self._served = Served(
                forecast=forecast, history=history, loaded_at=time.time(),
                provenance=self._service.served_forecast_provenance(),
                entry=self._service.served_forecast_entry(),
                serving_state=state,
            )
            self._seen = delivered
            # The newest run refused (the last good one served), or no history: try again within
            # the hour, not at every check.
            incomplete = state.get("degraded") or history is None
            self._retry_at = time.time() + RETRY_SECONDS if incomplete else None
            # Keep the previous delivery's files: a download that began before the hand-over may
            # still open one. They go at the next new delivery.
            keep = [(forecast, history)] + ([(previous.forecast, previous.history)] if previous else [])
            self._bulk_files.remove_other_deliveries(keep)
            self._failed_at = None
            logger.info("loader: forecast %s and history %s are served",
                        getattr(forecast, "source_file_id", None),
                        getattr(history, "source_file_id", None))
            return True
        except Exception as e:
            logger.error("loader: loading failed; the data already served stays: %r", e)
            self._failed_at = self._failed_at or time.time()
            return False

    def _delivered_ids(self, manager) -> tuple:
        """The store's ids of the newest forecast manifest and the newest history file."""
        manifest = manager.get_latest_manifest()
        forecast_id = manifest.get("fileId") if isinstance(manifest, dict) else None
        history_id = manager.get_latest_file_id({"category": "historical"})
        history_id = history_id if isinstance(history_id, str) else None
        # One line per check, naming what each listing found. With the server key it is the
        # request that proves `rows.read` (ADR-044); the download line proves `files.read`.
        logger.info("loader: store listing: newest forecast manifest %s, newest history file %s",
                    forecast_id or "NONE", history_id or "NONE")
        if forecast_id is None:
            # An empty listing looks the same whether nothing was delivered or the key may not
            # read the table. Say both, so neither is taken for a quiet month.
            logger.error("loader: the store listed no forecast delivery: none was delivered, or "
                         "the server key cannot read the delivery table (rows.read)")
        return (forecast_id, history_id)

    def _fresh(self, dataset, category: str):
        """The same delivery read again from the disk cache: new objects that nothing else uses.

        If the disk holds a different delivery, or none, the loaded dataset shares its pandas
        index with the service's cache, and so possibly with what the routes are reading. It is
        handed over only at the first load, when the routes are reading nothing; later the hand-over
        is refused and tried again at the next check.
        """
        if dataset is None:
            return None
        key_hash = self._service._api_key_hash(self._key)
        disk = self._disk_cache.read(key_hash, category, enforce_ttl=False)
        fresh = disk["dataset"] if disk else None
        if fresh is not None and getattr(fresh, "source_file_id", None) == getattr(dataset, "source_file_id", None):
            return fresh
        if self._served is None:
            logger.warning("loader: no matching %s on disk; handing over the loaded copy at first load", category)
            return dataset
        raise RuntimeError(f"no matching {category} on disk to hand over safely")

    def _build_bulk_files(self, forecast, history) -> None:
        """Build the nine bulk files before the hand-over, so no partner waits for one. A file
        that fails is logged and built on its first download instead."""
        if history is None:
            return
        for outcome in outcome_table.outcomes(forecast):
            if outcome_table.target_for(history, outcome) is None:
                continue
            for level in outcome_table.LEVELS:
                if self._stopping.is_set():
                    return
                try:
                    self._bulk_files.get(
                        forecast, history, level, outcome,
                        build=lambda p, lv=level, oc=outcome: write_bulk_file(p, forecast, history, lv, oc),
                    )
                except Exception as e:
                    logger.error("loader: bulk file %s %s not built: %r", level, outcome, e)

    async def run(self, minutes: float) -> None:
        """Check at start, then every `minutes`, in a worker thread. Runs until cancelled."""
        while not self._stopping.is_set():
            await asyncio.to_thread(self.refresh)
            # Wait in short steps, so a stop at shutdown is seen within a second.
            end = time.monotonic() + minutes * 60
            while not self._stopping.is_set() and time.monotonic() < end:
                await asyncio.sleep(min(1.0, end - time.monotonic()))
