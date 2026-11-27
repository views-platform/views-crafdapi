from views_crafdapi.managers.log import LoggingModule
from views_crafdapi.managers.model import APIPathManager
import logging
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Dict, Any
from pathlib import Path
import asyncio
import threading
import json
import os
import re
import time
from views_crafdapi.managers.appwrite import AppWriteFileManager, AppwriteConfig
from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager
from views_crafdapi.managers.dataset_service import (
    UNAUTHORIZED_DETAIL,
    DatasetService,
    refuse_if_unauthorized,
)
from views_crafdapi.managers.prediction import PredictionStoreManager
from views_crafdapi.managers import freshness
from views_crafdapi.time import date_to_month_id, month_id_to_date
from views_crafdapi.version import version_info
from views_crafdapi.seam_contract import CONSUMER_DOCUMENT_NAME
from views_crafdapi.managers.serialization import estimate_records_bytes

from fastapi import FastAPI, HTTPException, Depends, Query, Header, Request
from fastapi.responses import Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import hashlib
from cachetools import LRUCache, TTLCache

logger = logging.getLogger(__name__)


@dataclass
class StalenessResult:
    is_stale: bool
    age_hours: float
    threshold_hours: float


# The budget above which the slice refuses (C-284), applied to two estimates made before any
# work: the JSON answer (rows x columns, `estimate_records_bytes`) and the work (the cell-months
# read, with every forecast draw, at 8 bytes each). A country is one row of answer and hundreds
# of cells of work, so the answer alone would admit requests that cost gigabytes.
#
# 512 MiB is the figure measured on the server this service used to share with a sibling API,
# against the same serializer. The refusal says to ask for fewer places or months, or to download the
# bulk file, which holds the whole table.
_DEFAULT_MAX_RESPONSE_BYTES = 512 * 1024 ** 2


def _max_response_bytes() -> int:
    """The active budget, overridable per deployment.

    An env var rather than a constant because the box is shared and its headroom is a
    deployment fact, not a property of this code -- and because a bound that cannot be raised
    in an incident is one an operator will comment out instead.
    """
    raw = os.getenv("CRAFDAPI_MAX_RESPONSE_BYTES")
    if not raw:
        return _DEFAULT_MAX_RESPONSE_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "CRAFDAPI_MAX_RESPONSE_BYTES=%r is not an integer; using the %d byte default",
            raw, _DEFAULT_MAX_RESPONSE_BYTES,
        )
        return _DEFAULT_MAX_RESPONSE_BYTES
    if value <= 0:
        logger.warning(
            "CRAFDAPI_MAX_RESPONSE_BYTES=%d is not positive; using the %d byte default",
            value, _DEFAULT_MAX_RESPONSE_BYTES,
        )
        return _DEFAULT_MAX_RESPONSE_BYTES
    return value


def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GiB"


def _too_large_detail(what: str, estimated: int, budget: int, level: str, outcome: str) -> str:
    """The body of the 413: what was too large, and what to do instead."""
    return (
        f"This slice {what}, about {_human_bytes(estimated)}, above the {_human_bytes(budget)} "
        f"limit. Ask for fewer places or months, or download the whole table: /bulk/{level}/{outcome}"
    )


def _refuse_unknown_level_or_outcome(level: str, outcome: str) -> None:
    """404 for a level or outcome code that does not exist. Checked against the schema before the
    forecast loads, so a typo on a cold server is answered at once."""
    if level not in outcome_table.LEVELS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown level {level!r}. Levels: {', '.join(outcome_table.LEVELS)}",
        )
    if outcome not in schema.SERIES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown outcome {outcome!r}. Outcomes: {', '.join(schema.SERIES)}",
        )


def _forecast_made_in(forecast_ds) -> str:
    """The month the forecast was made in, written `2026-07`: its first month. The newest
    recorded month is the month the forecast is made in, and the forecast starts there."""
    return month_id_to_date(outcome_table.forecast_months(forecast_ds)[0])


def _refuse_outcome_not_forecast(forecast_ds, outcome: str) -> None:
    """404 for an outcome code the served forecast does not hold."""
    served = outcome_table.outcomes(forecast_ds)
    if outcome not in served:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown outcome {outcome!r}. Outcomes: {', '.join(served)}",
        )


# ASCII digits only: `\d` also matches other scripts' digits, which `int()` accepts, so one id
# could be written two ways and asked for twice.
_PLACE = re.compile(r"-?[0-9]+")   # negative: GAUL gives the Azores admin-1 codes -3727 to -3730
_MONTH = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _parse_names(raw: Optional[str]) -> Optional[list[str]]:
    """A comma-separated list, or `None` when the option is left out."""
    if raw is None:
        return None
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise ValueError("An option was given but empty; leave it out to get everything")
    return list(dict.fromkeys(names))


def _parse_places(raw: Optional[str]) -> Optional[list[int]]:
    """Place ids are whole numbers at every level (PRIO-GRID cell, GAUL admin-1, GAUL country).
    Repeats are removed after reading them as numbers, so `100,0100` is one place."""
    names = _parse_names(raw)
    if names is None:
        return None
    bad = [n for n in names if not _PLACE.fullmatch(n)]
    if bad:
        raise ValueError(f"Place ids must be whole numbers, got {bad[:20]}")
    return list(dict.fromkeys(int(n) for n in names))


def _parse_months(raw: Optional[str]) -> Optional[list[int]]:
    """Months written year-month (`2027-01`), returned as VIEWS month ids. Each once: in this
    fixed form two different strings never name one month, so `_parse_names` already removed
    the repeats."""
    names = _parse_names(raw)
    if names is None:
        return None
    bad = [n for n in names if not _MONTH.fullmatch(n)]
    if bad:
        raise ValueError(f"Months must be written year-month, such as 2027-01; got {bad[:20]}")
    return [date_to_month_id(int(n[:4]), int(n[5:])) for n in names]


def _refuse_repeated_options(request: Request) -> None:
    """`?places=1&places=2` would keep only the last value and answer with part of the table.
    That is how some HTTP clients send a list, so it is refused with the way to write it."""
    for name in ("places", "months", "columns"):
        if len(request.query_params.getlist(name)) > 1:
            raise ValueError(f"Give {name!r} once, comma-separated, such as {name}=a,b")


_REQUIRED_APPWRITE_ENV_VARS = [
    "APPWRITE_ENDPOINT",
    "APPWRITE_CRAFD_PROJECT_ID",  # CRAF'd's own project, not the shared one (þing 04 D3)
    "APPWRITE_CRAFD_BUCKET_ID",
    "APPWRITE_CRAFD_BUCKET_NAME",
    "APPWRITE_CRAFD_COLLECTION_ID",
    "APPWRITE_CRAFD_COLLECTION_NAME",
    "APPWRITE_METADATA_DATABASE_ID",
    "APPWRITE_METADATA_DATABASE_NAME",
]


def _registry_coordinates(registry_path: str) -> dict[str, str]:
    """Read the non-secret coordinate values (connection + target classes) from a PLATFORM-001
    registry TOML. Secrets are slots (no value) and are never read here. (þing-01 #278 / D6.)"""
    import tomllib

    with open(registry_path, "rb") as fh:
        registry = tomllib.load(fh)
    coords: dict[str, str] = {}
    for cls in ("connection", "target"):
        for name, entry in registry.get(cls, {}).items():
            if "value" in entry:
                coords[name] = entry["value"]
    return coords


def _validate_env_against_registry() -> None:
    """þing-01 #278 (D6): per-repo preflight. If the PLATFORM-001 registry is reachable
    (``APPWRITE_REGISTRY`` points at it), every coordinate present in the environment must MATCH the
    registry's declared value — the registry is canonical — and every required coordinate must have
    a value there. Fail loud naming the exact variable(s), all problems in one message.

    Transition-safe: if the registry is not reachable in this environment, only the presence check
    in ``_validate_appwrite_env`` runs. That is the case on the server, whose service environment
    does not set ``APPWRITE_REGISTRY``; there the env file is built from the registry at deploy
    time and ``bootstrap.sh part2`` refuses to install one that lacks a required name.
    """
    path = os.getenv("APPWRITE_REGISTRY", "").strip()
    if not path or not os.path.isfile(path):
        return
    coords = _registry_coordinates(path)
    # A required name the registry does not carry would pass the comparison below unchecked
    # (þing 04 orð_10), so its absence is a failure of its own.
    problems = [
        f"{name} has no value in the registry"
        for name in _REQUIRED_APPWRITE_ENV_VARS
        if name not in coords
    ]
    problems += [
        f"{name}={os.getenv(name)!r} but the registry declares {expected!r}"
        for name, expected in coords.items()
        if os.getenv(name) is not None and os.getenv(name) != expected
    ]
    if problems:
        msg = (
            "APPWRITE coordinate(s) fail the PLATFORM-001 registry check (þing-01 #278 / D6) — "
            "the registry is canonical, fix the environment or update the registry by PR: "
            + "; ".join(problems)
        )
        logger.critical(msg)
        raise ValueError(msg)


def _validate_appwrite_env() -> dict[str, str]:
    env = {var: os.getenv(var) for var in _REQUIRED_APPWRITE_ENV_VARS}
    missing = [var for var, val in env.items() if not val]
    if missing:
        logger.critical(f"Missing required environment variables: {', '.join(missing)}")
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    _validate_env_against_registry()  # þing-01 #278: coordinates must match the canonical registry
    return env


from views_crafdapi.forecast.serialize import outcome_table, schema  # noqa: E402
from views_crafdapi.forecast.serialize.bulk_file import write_bulk_file  # noqa: E402
from views_crafdapi.managers.bulk_files import BulkFiles  # noqa: E402
from views_crafdapi.managers.forecast_loader import ForecastLoader, check_minutes, read_server_key  # noqa: E402

class CrafdApiManager:
    """
    Manages the CRAF'd API lifecycle including startup, shutdown, and maintenance.
    """

    def __init__(
        self,
        model_path: APIPathManager,
    ) -> None:
        """
        Initializes the CrafdApiManager.

        Args:
            model_path (APIPathManager): The path manager for the API.
        """
        self._model_path = model_path
        self._logger = LoggingModule(logging_path=model_path.logging).get_logger()
        # Settings only the test constructor `from_config` fills; the service has none.
        self._config_deployment = self._config_hyperparameters = self._config_meta = None

        logger.info(f"{str(self._model_path.dotenv)}")
        load_dotenv(dotenv_path=self._model_path.dotenv)
        validated_env = _validate_appwrite_env()

        self._staleness_threshold_hours = 24.0
        self._forecast_freshness_sla_days = freshness.freshness_sla_days()  # S3 (#246, C-50)
        self._init_caches()

        self._disk_cache = CrafdDiskCacheManager(self._model_path.cache / "datasets")
        self._bulk_files = BulkFiles(self._model_path.cache / "bulk")

        self._prediction_bucket_id = validated_env["APPWRITE_CRAFD_BUCKET_ID"]

        # Compose the data-fetch pipeline (epic #144 / S2): caches stay owned here and
        # are injected by reference so routes/lifecycle/stats see the same objects.
        self._dataset_service = DatasetService(
            dataframe_cache=self._dataframe_cache,
            file_cache=self._file_cache,
            disk_cache=self._disk_cache,
            prediction_bucket_id=self._prediction_bucket_id,
            configs_getter=lambda: self.configs,
            api_key_hash_fn=self._get_api_key_hash,
            check_staleness_fn=self._check_staleness,
        )
        # ADR-044: the forecast is loaded with the server's own read key, at start and when a new
        # delivery appears, never in a partner's request.
        self._loader = self._make_loader(read_server_key())

        self.app = FastAPI(
            title="VIEWS-CRAF'd Conflict Forecast API",
            description="VIEWS historical and forecasted fatalities data and statistical analysis for the Complex Risk Analytics Fund (CRAF'd).",
            # Derived, not literal: a hardcoded copy here drifted from pyproject
            # and is one more thing to remember at release time.
            version=version_info()["version"],
            lifespan=self._lifespan
        )
        

        # No signal handlers of our own (#236): uvicorn's handle SIGTERM and SIGINT and run the
        # lifespan shutdown (`_lifespan`: stop the loader, wait for it, then clear the caches).
        # A handler installed here replaced uvicorn's, so a stop skipped all of that and cleared
        # the caches under a running load (checked 2026-11-22 with a real SIGTERM).

        # Register routes
        self._register_routes()

    # How long shutdown waits for a running load to stop before leaving without clearing caches.
    _STOP_WAIT_SECONDS = 60

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        """Start the loader at start-up (ADR-044) and stop it at shutdown."""
        logger.info("CRAF'd API is starting up...")
        # Requests are answered at once; until the first load is complete they get a 503.
        loading = asyncio.create_task(self._loader.run(check_minutes()))

        yield

        # Stop a running load at its next step, and wait up to a minute for it (a grid file being
        # written finishes first, about 40 seconds). The caches are cleared only once it stopped.
        # This runs after uvicorn has finished the open requests (up to 30 s): until then the
        # loader keeps checking, and a load it starts then is stopped here like any other.
        self._loader.stop()
        try:
            await asyncio.wait_for(loading, timeout=self._STOP_WAIT_SECONDS)
        except asyncio.TimeoutError:
            # The worker thread may still be writing the caches: leave them. On SIGTERM (how
            # systemd stops it) the process then ends at once: uvicorn sends the signal to itself
            # again once it has finished, and the thread dies in the middle of its step. Bulk files
            # are written under a temporary name, so none is left half-written; the loader's sweep
            # removes old temporary files (BulkFiles.remove_other_deliveries), and #243 tracks the
            # cache's staging folders.
            logger.warning("CRAF'd API is shutting down; the forecast load did not stop within %s "
                           "seconds, so the caches are left to it", self._STOP_WAIT_SECONDS)
            return
        except Exception as e:   # the load ended with an error: it no longer writes the caches
            logger.error("the forecast load ended with an error at shutdown: %r", e)
        logger.info("CRAF'd API is shutting down...")
        self._manager_cache.clear()
        self._dataframe_cache.clear()
        self._file_cache.clear()
        logger.info("Cleared all caches")

    @property
    def configs(self) -> Dict:
        """The three settings dicts merged; later ones win: hyperparameters, deployment, meta."""
        config = {}
        for part in (self._config_hyperparameters, self._config_deployment, self._config_meta):
            config.update(part or {})
        return config

    @classmethod
    def from_config(
        cls,
        config: Dict[str, Any],
        *,
        cache_dir: Optional[Path] = None,
    ) -> "CrafdApiManager":
        """Create a CrafdApiManager from a pre-built config dict, bypassing filesystem config discovery.

        This is the supported test seam — use it instead of ``object.__new__()`` followed
        by manual attribute setup.
        """
        instance = object.__new__(cls)
        instance._config_deployment = config.get("deployment")
        instance._config_hyperparameters = config.get("hyperparameters")
        instance._config_meta = config.get("meta")
        instance._staleness_threshold_hours = float(config.get("staleness_threshold_hours", 24.0))
        instance._forecast_freshness_sla_days = freshness.freshness_sla_days()  # S3 (#246, C-50)
        instance._init_caches()

        if cache_dir is None:
            import tempfile
            cache_dir = Path(tempfile.mkdtemp()) / "datasets"
        instance._disk_cache = CrafdDiskCacheManager(cache_dir)
        instance._bulk_files = BulkFiles(cache_dir.parent / "bulk")
        instance._prediction_bucket_id = config.get("prediction_bucket_id", "test-bucket")

        instance._dataset_service = DatasetService(
            dataframe_cache=instance._dataframe_cache,
            file_cache=instance._file_cache,
            disk_cache=instance._disk_cache,
            prediction_bucket_id=instance._prediction_bucket_id,
            configs_getter=lambda: instance.configs,
            api_key_hash_fn=instance._get_api_key_hash,
            check_staleness_fn=instance._check_staleness,
        )
        # Tests give the loader a stand-in key and store (`server_key` in the config).
        instance._loader = instance._make_loader(config.get("server_key"))

        return instance

    def _init_caches(self):
        self._manager_cache: LRUCache = LRUCache(maxsize=100)
        # Partner keys found valid, by key hash. An entry lasts one minute, so a key deleted in
        # Appwrite stops working within a minute (service specification: "within minutes").
        self._valid_keys: TTLCache = TTLCache(maxsize=1000, ttl=60)
        # FastAPI runs a plain `def` dependency in a worker thread, and the cache is not safe to
        # change from two threads at once.
        self._valid_keys_lock = threading.Lock()
        self._dataframe_cache: TTLCache = TTLCache(maxsize=50, ttl=4 * 3600)
        self._file_cache: LRUCache = LRUCache(maxsize=20)

    def _get_api_key_hash(self, x_api_key: str) -> str:
        """Generate a hash of the API key for use as a cache key."""
        return hashlib.sha256(x_api_key.encode()).hexdigest()[:16]

    def _check_staleness(self, cached_timestamp: float, threshold_hours: Optional[float] = None) -> StalenessResult:
        threshold = threshold_hours if threshold_hours is not None else self._staleness_threshold_hours
        age_hours = (time.time() - cached_timestamp) / 3600
        return StalenessResult(
            is_stale=age_hours > threshold,
            age_hours=age_hours,
            threshold_hours=threshold,
        )

    def _create_appwrite_config(self, x_api_key: str) -> AppwriteConfig:
        """
        Create an AppwriteConfig instance with the provided API key.

        Args:
            x_api_key: The API key for authentication

        Returns:
            AppwriteConfig instance

        Raises:
            ValueError: If any required APPWRITE_* environment variable is missing.
        """
        env = _validate_appwrite_env()

        return AppwriteConfig(
            # Without this the config's `path_manager` defaults to None, so
            # `PredictionStoreManager.model_path` is None, so
            # `get_predictions_by_metadata`'s `hasattr(self.model_path, 'model_name')` is
            # False and the `name` filter is **silently skipped** — every prediction query
            # runs unscoped across the collection. That defeats the S1/#53 seam-contract
            # binding (`seam_contract.CONSUMER_DOCUMENT_NAME`, "the name this API owns and
            # filters every prediction query on") and re-opens the ADR-017 §1
            # invisible-delivery hole it closes: a producer uploading under another
            # consumer's name would be served happily. Every test that appeared to cover
            # this injected a MagicMock config, whose `model_name` attribute auto-exists.
            # getattr, not attribute access: the `from_config` alternate constructor does not
            # set `_model_path`. Production always does (`CrafdApiManager(model_path=...)`).
            path_manager=getattr(self, "_model_path", None),
            endpoint=env["APPWRITE_ENDPOINT"],
            project_id=env["APPWRITE_CRAFD_PROJECT_ID"],
            credentials=x_api_key,
            auth_method="api_key",
            cache_ttl_hours=24,
            bucket_id=env["APPWRITE_CRAFD_BUCKET_ID"],
            bucket_name=env["APPWRITE_CRAFD_BUCKET_NAME"],
            collection_id=env["APPWRITE_CRAFD_COLLECTION_ID"],
            collection_name=env["APPWRITE_CRAFD_COLLECTION_NAME"],
            database_id=env["APPWRITE_METADATA_DATABASE_ID"],
            database_name=env["APPWRITE_METADATA_DATABASE_NAME"],
        )


    def _validate_api_key(self, x_api_key: str) -> AppWriteFileManager:
        """
        Validate the API key and return an AppWriteFileManager instance.
        This method validates the API key on every call.
        """
        try:
            config = self._create_appwrite_config(x_api_key)
            manager = AppWriteFileManager(config)
            
            # Validate the API key by making a simple API call
            validation_result = manager.list_buckets(limit=1)
            if not validation_result.success:
                status = validation_result.status
                # The store's own wording stays in the log, not the response (C-264).
                logger.warning("API key validation failed (status=%s): %s",
                               status, validation_result.error)
                if status in (401, 403):
                    raise HTTPException(status_code=401, detail=UNAUTHORIZED_DETAIL)
                # Anything else — no answer, a rate limit, a timeout, a wrong endpoint or project,
                # a server error — says nothing about the key. Telling the caller "invalid key"
                # sent them to rotate a key that was fine (#159).
                raise HTTPException(status_code=503, detail="Metadata store unavailable")

            return manager

        except ValueError as e:
            # A deployment error (e.g. missing APPWRITE_* variables): its detail is for the
            # operator's log, not for whoever sent the request (C-264).
            logger.critical("API key validation could not be configured: %s", e)
            raise HTTPException(status_code=500, detail="Server configuration error") from e
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("API key validation could not reach the store: %r", e)
            raise HTTPException(status_code=503, detail="Metadata store unavailable") from e

        
    def _get_appwrite_manager(self, x_api_key: str = Header(..., description="Appwrite API Key")) -> AppWriteFileManager:
        """
        Dependency to get or create an AppWriteFileManager instance for the provided API key.
        Managers are cached to avoid recreating them for each request.
        """
        api_key_hash = self._get_api_key_hash(x_api_key)
        
        if api_key_hash not in self._manager_cache:
            # Validate the API key and create manager
            manager = self._validate_api_key(x_api_key)
            self._manager_cache[api_key_hash] = {
                "appwrite": manager,
                "prediction": None  # Will be created on demand
            }
        
        return self._manager_cache[api_key_hash]["appwrite"]

    def _get_prediction_manager(self, x_api_key: str = Header(..., description="Appwrite API Key")) -> PredictionStoreManager:
        """
        Dependency to get or create a PredictionStoreManager instance for the provided API key.
        """
        api_key_hash = self._get_api_key_hash(x_api_key)
        
        if api_key_hash not in self._manager_cache:
            # Initialize cache entry with AppWrite manager first
            appwrite_manager = self._validate_api_key(x_api_key)
            self._manager_cache[api_key_hash] = {
                "appwrite": appwrite_manager,
                "prediction": None
            }
        
        # Check if we need to create the prediction manager
        if self._manager_cache[api_key_hash].get("prediction") is None:
            try:
                # Create AppwriteConfig for PredictionStoreManager
                config = self._create_appwrite_config(x_api_key)
                
                # Create PredictionStoreManager with the config
                prediction_manager = PredictionStoreManager(config)
                self._manager_cache[api_key_hash]["prediction"] = prediction_manager
                
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create PredictionStoreManager: {str(e)}"
                )
        
        return self._manager_cache[api_key_hash]["prediction"]

    def _make_loader(self, server_key: Optional[str]) -> ForecastLoader:
        """The loader (ADR-044), built the same way by `__init__` and `from_config`."""
        return ForecastLoader(
            self._dataset_service, self._disk_cache, self._bulk_files,
            server_key, self._make_server_manager,
        )

    def _make_server_manager(self, server_key: str) -> PredictionStoreManager:
        """The store reader the loader uses, with the server's own key (ADR-044)."""
        return PredictionStoreManager(self._create_appwrite_config(server_key))

    def _check_partner_key(self, x_api_key: str = Header(..., description="Your API key")) -> None:
        """Let a partner in, or answer 401. The key is checked with the store, and a valid key is
        remembered for one minute. Partner keys only decide who gets in; the data is read with
        the server's key (ADR-044)."""
        key_hash = self._get_api_key_hash(x_api_key)
        with self._valid_keys_lock:
            if key_hash in self._valid_keys:
                return
        self._validate_api_key(x_api_key)   # outside the lock: a network call
        with self._valid_keys_lock:
            self._valid_keys[key_hash] = True

    def _served(self):
        """The loaded forecast and history the routes answer from; 503 before the first load."""
        served = self._loader.served()
        if served is None:
            raise HTTPException(
                status_code=503,
                detail="No forecast is available right now. Try again later.",
            )
        return served

    def _get_latest_dataframe(self, manager: PredictionStoreManager, x_api_key: str, category: str, force_refresh: bool = False) -> pd.DataFrame:
        """Delegate the data-fetch pipeline to the composed DatasetService (epic #144 / S2)."""
        return self._dataset_service.get_latest_dataframe(manager, x_api_key, category, force_refresh)

    @staticmethod
    def _history_holding(served, outcome: str):
        """The loaded history, which must hold `outcome`; otherwise a 503. Served empty, the
        recorded deaths would read as "nothing recorded", so the answer is refused until the
        history is back."""
        history = served.history
        if history is None or outcome_table.target_for(history, outcome) is None:
            raise HTTPException(
                status_code=503,
                detail="The recorded deaths are unavailable right now. Try again later.",
            )
        return history.copy()

    def _register_routes(self):
        """Register all API routes."""
        
        @self.app.get("/ping")
        async def ping():
            """Unauthenticated liveness probe for external uptime monitors.

            Returns 200 without touching Appwrite, the cache, or any auth — it answers
            iff the process is up and serving HTTP. Distinct from `/health`, which
            requires an API key and verifies the Appwrite connection (readiness).
            """
            return {"status": "ok"}

        @self.app.get("/version")
        async def version():
            """Unauthenticated deployed-version probe — the installed package version
            and the pinned deploy tag (S4), for remote verification of what is live."""
            return version_info()

        # Historical data endpoints
        # `/data/{category}/latest` was RETIRED here (2026-08-24). It answered HTTP 200 with rows
        # carrying no values — the ADR-030 §5 store migration moved samples and scalars out of
        # `.dataframe`, and the handlers served that index-only frame directly (register C-232,
        # Tier 1, open since 2026-08-10). Measured live before removal: 88,357,820 bytes in 9.04 s
        # for `/data/forecast/latest`, every row carrying only `month_id` and `priogrid_id`.
        #
        # Retired rather than bounded because the fix and the defect pointed the same way: nothing
        # this repo ships ever called it (not the client, not smoke.py, not a notebook), and anyone
        # who DID call it was already receiving a successful-looking empty answer. A 404 tells a
        # caller to stop; a valueless 200 does not.
        #
        # `_get_latest_dataframe` survives deliberately as a test seam (~30 tests drive the pipeline
        # through it). The routes read what the loader has loaded (`_served`, ADR-044).

        @self.app.get("/bulk/{level}/{outcome}")
        async def get_bulk(
            level: str,
            outcome: str,
            _: None = Depends(self._check_partner_key),
        ):
            """The whole table of one outcome at one level, as one Parquet file (#203).

            Exactly what a slice with no options returns: every place and every month, the
            recorded months with the recorded deaths, the forecast months with the summary
            columns. Built on the first request after a new forecast or history, then served as
            is, to every key (`BulkFiles`). Without the recorded deaths the file is refused (503),
            as the slice is.
            """
            _refuse_unknown_level_or_outcome(level, outcome)
            served = self._served()
            try:
                forecast_ds = served.forecast.copy()
                _refuse_outcome_not_forecast(forecast_ds, outcome)
                historical_ds = self._history_holding(served, outcome)
                path, kept = self._bulk_files.get(
                    forecast_ds, historical_ds, level, outcome,
                    build=lambda p: write_bulk_file(p, forecast_ds, historical_ds, level, outcome),
                )
                return FileResponse(
                    path,
                    media_type="application/vnd.apache.parquet",
                    filename=f"{outcome}_{level}.parquet",
                    headers={"X-Forecast-Made-In": _forecast_made_in(forecast_ds)},
                    background=None if kept else BackgroundTask(path.unlink, missing_ok=True),
                )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # `async def`, like every route here: the work runs on the event loop, one request at a
        # time. A plain `def` would run slices side by side in worker threads, on shallow copies
        # of one dataset that share pandas index objects; the first concurrent use of a freshly
        # loaded index then reads a half-built lookup table (measured: 8 wrong answers in 30).
        # The cost is that /ping and /health wait while a slice is computed (register C-285).
        @self.app.get("/slice/{level}/{outcome}")
        async def get_slice(
            request: Request,
            level: str,
            outcome: str,
            places: Optional[str] = Query(None, description="Place ids at this level, comma-separated"),
            months: Optional[str] = Query(None, description="Months written year-month (2027-01), comma-separated"),
            columns: Optional[str] = Query(None, description="Summary column names, comma-separated"),
            _: None = Depends(self._check_partner_key),
        ):
            """A slice of one outcome at one level: four options, as the specification says.

            Rows are places and months; columns are the place's id and names, `month`,
            `month_id`, and the outcome's summary columns (all, or the ones named). Never samples.
            """
            _refuse_unknown_level_or_outcome(level, outcome)
            try:
                _refuse_repeated_options(request)
                parsed_places = _parse_places(places)
                parsed_months = _parse_months(months)
                asked_columns = _parse_names(columns)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            served = self._served()
            try:
                forecast_ds = served.forecast.copy()
                _refuse_outcome_not_forecast(forecast_ds, outcome)
                value_columns = outcome_table.value_columns(outcome)
                if asked_columns is not None:
                    unknown = [c for c in asked_columns if c not in value_columns]
                    if unknown:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Unknown columns {unknown}. Columns: {', '.join(value_columns)}",
                        )
                    value_columns = [c for c in value_columns if c in set(asked_columns)]

                # The history is read only when the request reaches months before the forecast.
                # Then it must be there and hold this outcome: served empty, the recorded deaths
                # would read as "nothing recorded", so the slice is refused until it is back.
                historical_ds = None
                if outcome_table.needs_history(forecast_ds, parsed_months):
                    historical_ds = self._history_holding(served, outcome)

                identity = outcome_table.place_identity(forecast_ds, level)
                if parsed_places is not None:
                    unknown = [p for p in parsed_places if p not in identity.index]
                    if unknown:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Unknown place ids at level {level!r}: {unknown[:20]}",
                        )
                fc_months = outcome_table.forecast_months(forecast_ds)
                all_months = sorted(set(fc_months) | set(outcome_table.recorded_months(forecast_ds, historical_ds)))
                if parsed_months is not None:
                    unknown = [month_id_to_date(m) for m in parsed_months if m not in set(all_months)]
                    if unknown:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"No data for months {unknown[:20]}. Months served: "
                                f"{month_id_to_date(all_months[0])} to {month_id_to_date(all_months[-1])}"
                            ),
                        )

                # Decide before computing (C-284), on two sizes that follow from the request:
                # the answer (rows x columns), and the work (the cell-months read, with every
                # draw of the forecast). A country is one row of answer and hundreds of cells
                # of work, so the answer alone would admit requests that cost gigabytes.
                asked_months = parsed_months if parsed_months is not None else all_months
                n_rows = (len(parsed_places) if parsed_places is not None else len(identity)) * len(asked_months)
                n_columns = len(outcome_table.IDENTITY_COLUMNS[level]) + len(value_columns)
                n_cells = outcome_table.cells_read(forecast_ds, level, parsed_places)
                n_forecast = sum(1 for m in asked_months if m in set(fc_months))
                values_read = n_cells * (n_forecast * forecast_ds.sample_size + len(asked_months) - n_forecast)
                budget = _max_response_bytes()
                for what, estimated in (
                    (f"answer is {n_rows:,} rows", estimate_records_bytes(n_rows, n_columns, 0)),
                    (f"reads {values_read:,} values", values_read * 8),
                ):
                    if estimated > budget:
                        raise HTTPException(
                            status_code=413,
                            detail=_too_large_detail(what, estimated, budget, level, outcome),
                        )

                table = outcome_table.build_outcome_table(
                    forecast_ds, historical_ds, level, outcome,
                    places=parsed_places, months=parsed_months, identity=identity,
                )
                table = table[outcome_table.IDENTITY_COLUMNS[level] + value_columns]
                # `to_json` writes NaN and missing whole numbers as null, and is far faster than
                # building Python dicts, which matters while the event loop waits.
                rows = table.to_json(orient="records", double_precision=15)
                body = (
                    f'{{"level": {json.dumps(level)}, "outcome": {json.dumps(outcome)}, '
                    f'"forecast_made_in": {json.dumps(_forecast_made_in(forecast_ds))}, '
                    f'"columns": {json.dumps(table.columns.tolist())}, "rows": {rows}}}'
                )
                return Response(content=body, media_type="application/json")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/health")
        async def health_check(
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: AppWriteFileManager = Depends(self._get_appwrite_manager),
            prediction_manager: PredictionStoreManager = Depends(self._get_prediction_manager),
        ):
            """Health check: Appwrite connectivity + forecast freshness (S3/#246, C-50).

            HTTP status stays about *service* health (401 for a refused key; 503 only if
            the check raises). The body's ``status`` is ``"degraded"`` (service up, data not
            vouched for) when nothing is loaded, the history is missing, the newest delivery
            failed to load, the forecast is older than the SLA or its age cannot be measured,
            the store answers with an error other than a refused key, or a fallback is served
            (#248). The Better Stack monitor reads only ``status`` (ADR-041).
            """
            try:
                result = manager.list_buckets(limit=1)
                if not result.success and result.status in (401, 403):
                    # The managers are cached without revalidation (ADR-011 §3), so this probe is
                    # where a revoked key shows up on /health (C-268, #159).
                    raise HTTPException(status_code=401, detail=UNAUTHORIZED_DETAIL)
                health = {
                    "status": "healthy",
                    "appwrite_connected": result.success,
                }
                if not result.success:
                    # The store answered with an error other than a refused key, or could not be
                    # reached: the next delivery cannot be loaded, so the monitor must alarm (#248).
                    health["status"] = "degraded"
                    logger.warning("Store probe FAILED: status=%s code=%s error=%s",   # log only (C-264)
                                   getattr(result, "status", None), getattr(result, "code", None),
                                   getattr(result, "error", None))
                # What partners are answered from (ADR-044). Nothing loaded yet is degraded:
                # every data request answers 503 until the first load is complete.
                loaded = self._loader.served()
                health["forecast_loaded"] = loaded is not None
                if loaded is None:
                    health["status"] = "degraded"
                health["history_loaded"] = bool(loaded and loaded.history is not None)
                if loaded is not None and loaded.history is None:
                    # The forecast is served, but every bulk file and every slice of past months
                    # answers 503 until the recorded deaths load.
                    health["status"] = "degraded"
                if self._loader.failing_since() is not None:
                    # The data already loaded is still served; the newest delivery is not in it.
                    health["status"] = "degraded"
                    health["forecast_loading"] = "the newest delivery could not be loaded; the previous one is served"
                # S3 (#246, C-50): a stale forecast degrades health rather than logging silently.
                # #290 sibling: freshness must be about the SERVED / newest-manifested run, not the
                # store's newest metadata record — which can still be a superseded legacy artifact
                # (e.g. the March orange_ensemble), falsely reading stale while a 1-day-old wire run
                # is being served. Prefer the served run's created_at, then the newest manifest's
                # completion time, then (only if neither exists) the legacy provenance record.
                try:
                    served = loaded.provenance if loaded else None
                    created_at = served.get("created_at") if served else None
                    if not created_at:
                        try:
                            m = prediction_manager.get_latest_manifest()
                            created_at = m.get("$createdAt") if isinstance(m, dict) else None
                        except Exception as e:
                            refuse_if_unauthorized(e)   # a refusal is not a freshness blip (#159)
                            created_at = None
                    if not created_at:
                        prov = prediction_manager.get_latest_provenance(filters={"category": "forecast"})
                        created_at = prov.created_at if prov else None
                    fr = freshness.forecast_freshness(
                        created_at, self._forecast_freshness_sla_days
                    )
                    health["forecast_freshness"] = fr
                    if fr.get("is_stale") is None:
                        # No usable date: the age of what is served cannot be measured, so nothing
                        # can say it is current (#248).
                        health["status"] = "degraded"
                        logger.warning("Forecast age UNKNOWN: no usable created_at (%r)", created_at)
                    if fr.get("is_stale"):
                        health["status"] = "degraded"
                        logger.warning(
                            "Forecast STALE: %.1f days old > %.0f-day SLA (created=%s)",
                            fr["age_days"], fr["sla_days"], fr["created_at"],
                        )
                except HTTPException:
                    raise   # the 401 above; best-effort never covers an unauthorised key
                except Exception as e:  # freshness is best-effort; never fail the health check on it
                    refuse_if_unauthorized(e)
                    logger.warning("Forecast freshness check failed (non-blocking): %s", e)
                    health["forecast_freshness"] = {"error": "freshness check failed"}  # C-264
                    health["status"] = "degraded"   # the age is unknown (#248)
                # S4 (#249, ADR-033 §6): a bounded grace fallback (the newest manifested run was
                # refused, a last-good run is being served) is a degraded state — surface it and flip
                # status, even when the served last-good is itself still within the freshness SLA.
                # Taken when the served data was handed over, so it describes what partners get.
                serving_state = loaded.serving_state if loaded else None
                if serving_state is not None:
                    health["forecast_serving_state"] = serving_state
                    # `cached_unverified` records that the *last* forecast was served while the
                    # store could not be asked. Once this probe reaches the store again it
                    # describes a past response, not the service: show it, but do not page on
                    # it — the loader's next check re-verifies the cache (#158 review, ADR-044).
                    unverified_but_store_back = (
                        serving_state.get("serving") == "cached_unverified" and result.success
                    )
                    if serving_state.get("degraded") and not unverified_but_store_back:
                        health["status"] = "degraded"
                        logger.warning(
                            "Forecast serving DEGRADED: newest run refused (reason=%s); "
                            "grace-fallback available=%s",
                            serving_state.get("reason"), serving_state.get("fallback_available"),
                        )
                # C-282 (#170): which build wrote the forecast being served. After a deploy the
                # slot can still be the previous build's; this says whether a fix is in effect.
                # Reported, not judged: an older writer is not a fault and does not degrade.
                served_entry = loaded.entry if loaded else None
                if served_entry is not None:
                    health["served_entry"] = served_entry
                # Which forecast run partners get (the delivery's own name), so a check from
                # outside can confirm the intended run is the one served (#262).
                served_run = (loaded.provenance or {}).get("run_id") if loaded else None
                if served_run:
                    health["served_run"] = served_run
                return health
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("Health check failed")
                raise HTTPException(
                    status_code=503,
                    detail="Service unhealthy"  # the cause goes to the log, not the response (C-264)
                ) from e


# Module-level app instance for uvicorn import string (required for multi-worker mode)
_fao_manager: Optional[CrafdApiManager] = None
app: Optional[FastAPI] = None


def _service_paths() -> APIPathManager:
    """The service's paths, named by the store-document name it serves (seam_contract)."""
    return APIPathManager(CONSUMER_DOCUMENT_NAME)


def create_app() -> FastAPI:
    """
    Factory function to create the FastAPI app for uvicorn workers.
    
    This is needed when running with multiple workers, as uvicorn requires
    an import string (e.g., 'module:app') rather than an app object.
    
    Returns:
        FastAPI: The configured FastAPI application instance.
    """
    global _fao_manager, app
    if app is None:
        # The service must boot from a clean checkout: `apis/un_crafd/` (cache and logs) is
        # ignored by git and created on demand. Tested by tests/test_create_app_boot.py.
        model_path = _service_paths()
        _fao_manager = CrafdApiManager(model_path=model_path)
        app = _fao_manager.app
    return app


