from views_crafdapi.managers.model import APIManager, APIPathManager
import logging
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Dict, Any
from pathlib import Path
import os
import re
import shutil
import tempfile
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
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.time import date_to_month_id, month_id_to_date
from views_crafdapi.version import version_info
from views_crafdapi.seam_contract import CONSUMER_DOCUMENT_NAME
from views_crafdapi.managers.serialization import estimate_records_bytes

from fastapi import FastAPI, HTTPException, Depends, Query, Header
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from contextlib import asynccontextmanager
import uvicorn
import signal
import sys
from dotenv import load_dotenv
import hashlib
from cachetools import LRUCache, TTLCache

logger = logging.getLogger(__name__)


@dataclass
class StalenessResult:
    is_stale: bool
    age_hours: float
    threshold_hours: float


# The estimated rendering size above which a subset or hdi-map route refuses (C-284).
#
# A BYTE budget, not a row count, and the reason is measured: a historical row costs ~1,625 B
# through `dataframe_to_dict` and a forecast row ~7,731 B, because the second carries a posterior
# draw per sample per target -- and the sample width varies per delivery, so a 32-draw run and a
# 1000-draw run differ by another factor of thirty. No single row number is right for both
# categories, let alone for two runs of one. `estimate_records_bytes` carries the calibration.
#
# 512 MiB is the figure already measured for THIS box against this same serializer, by the
# sibling API it co-hosts: 22 GiB total, shared between the two services, against a multi-GiB
# warm baseline. It admits ~320,000 historical rows and ~65,000 rows of a 32-draw forecast.
# Holding both co-tenants to one number is the point -- it stops each assuming the whole machine.
#
# This bounds a RENDERING, not the data. Every table stays retrievable in full by the paths the
# refusal names, which are the ones CRAF'd already uses.
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


def _too_large_detail(n_rows: int, estimated: int, budget: int) -> str:
    """The body of the 413: what was asked, and what to do instead."""
    return (
        f"This slice is {n_rows:,} rows and would need about {_human_bytes(estimated)} to "
        f"render as JSON, above the {_human_bytes(budget)} limit. Ask for fewer places or "
        "months, or use the bulk download."
    )


def _parse_names(raw: Optional[str]) -> Optional[list[str]]:
    """A comma-separated list, or `None` when the option is left out."""
    if raw is None:
        return None
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise ValueError("An option was given but empty; leave it out to get everything")
    return names


def _parse_places(raw: Optional[str]) -> Optional[list[int]]:
    """Place ids are whole numbers at every level (PRIO-GRID cell, GAUL admin-1, GAUL country)."""
    names = _parse_names(raw)
    if names is None:
        return None
    bad = [n for n in names if not re.fullmatch(r"\d+", n)]
    if bad:
        raise ValueError(f"Place ids must be whole numbers, got {bad[:20]}")
    return [int(n) for n in names]


def _parse_months(raw: Optional[str]) -> Optional[list[int]]:
    """Months written year-month (`2027-01`), returned as VIEWS month ids."""
    names = _parse_names(raw)
    if names is None:
        return None
    bad = [n for n in names if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", n)]
    if bad:
        raise ValueError(f"Months must be written year-month, such as 2027-01; got {bad[:20]}")
    return [date_to_month_id(int(n[:4]), int(n[5:])) for n in names]


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


from views_crafdapi.managers.serialization import convert_numpy_types  # noqa: E402
from views_crafdapi.forecast.serialize import outcome_table  # noqa: E402
from views_crafdapi.forecast.serialize.bulk_parquet import write_bulk_parquet  # noqa: E402

class CrafdApiManager(APIManager):
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
        super().__init__(model_path=model_path)

        logger.info(f"{str(self._model_path.dotenv)}")
        load_dotenv(dotenv_path=self._model_path.dotenv)
        validated_env = _validate_appwrite_env()

        self._staleness_threshold_hours = 24.0
        self._forecast_freshness_sla_days = freshness.freshness_sla_days()  # S3 (#246, C-50)
        self._init_caches()

        self._disk_cache = CrafdDiskCacheManager(self._model_path.cache / "datasets")

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

        # Define lifespan context manager before creating FastAPI app
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # Startup logic
            logger.info("CRAF'd API is starting up...")
            # Add any startup logic here (e.g., cache warming, DB connections)
            
            yield
            
            # Shutdown logic
            logger.info("CRAF'd API is shutting down...")
            # Clear manager cache
            self._manager_cache.clear()
            self._dataframe_cache.clear()
            self._file_cache.clear()
            logger.info("Cleared all caches")
        
        self.app = FastAPI(
            title="VIEWS-CRAF'd Conflict Forecast API",
            description="VIEWS historical and forecasted fatalities data and statistical analysis for the Complex Risk Analytics Fund (CRAF'd).",
            # Derived, not literal: a hardcoded copy here drifted from pyproject
            # and is one more thing to remember at release time.
            version=version_info()["version"],
            lifespan=lifespan
        )
        
        self.appwrite_manager: Optional[AppWriteFileManager] = None
        self._server_config = {
            "host": self.configs.get("host", "0.0.0.0"),
            "port": int(self.configs.get("port", 80)),
            "reload": self.configs.get("reload", "false").lower() == "true",
            "workers": int(self.configs.get("workers", 1)),
        }
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        # Register routes
        self._register_routes()

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

        return instance

    def _init_caches(self):
        self._manager_cache: LRUCache = LRUCache(maxsize=100)
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

    def _get_latest_dataframe(self, manager: PredictionStoreManager, x_api_key: str, category: str, force_refresh: bool = False) -> pd.DataFrame:
        """Delegate the data-fetch pipeline to the composed DatasetService (epic #144 / S2)."""
        return self._dataset_service.get_latest_dataframe(manager, x_api_key, category, force_refresh)

    def _get_latest_dataset(self, manager: PredictionStoreManager, x_api_key: str, category: str, force_refresh: bool = False) -> ForecastDataset:
        """Delegate the data-fetch pipeline to the composed DatasetService (epic #144 / S2)."""
        return self._dataset_service.get_latest_dataset(manager, x_api_key, category, force_refresh)

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
        # through it). Every route uses `_get_latest_dataset`, which returns the dataset the request
        # selected; `_get_latest_dataframe` is that dataset's frame (C-356, #156).

        @self.app.get("/data/forecast/bulk")
        async def get_forecast_bulk_parquet(
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: PredictionStoreManager = Depends(self._get_prediction_manager),
        ):
            """ADR-025 admin-1 bulk artifact (#222/S6): the full forecast table — MAP + 50/90/95
            HDIs + `severe_scenario` + historical `actual` — as one wide parquet, one row per
            `(month_id, admin1_code)`. The same numbers as the JSON API, packaged for a single
            load. Historical is best-effort: if no historical artifact is available, `s_actual`
            is served as NaN rather than failing the download."""
            try:
                forecast_ds = self._get_latest_dataset(manager, x_api_key, "forecast")
                try:
                    historical_ds = self._get_latest_dataset(
                        manager, x_api_key, "historical"
                    )
                except HTTPException:
                    historical_ds = None
                    logger.warning("bulk parquet: no historical artifact — s_actual served as NaN")
                tmpdir = tempfile.mkdtemp()
                out = Path(tmpdir) / "crafd_forecast_bulk.parquet"
                write_bulk_parquet(out, forecast_ds, historical_ds)
                # Clean the per-request temp dir once the file has been streamed (no disk leak).
                return FileResponse(
                    out,
                    media_type="application/octet-stream",
                    filename="crafd_forecast_bulk.parquet",
                    background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
                )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/slice/{level}/{outcome}")
        async def get_slice(
            level: str,
            outcome: str,
            places: Optional[str] = Query(None, description="Place ids at this level, comma-separated"),
            months: Optional[str] = Query(None, description="Months written year-month (2027-01), comma-separated"),
            columns: Optional[str] = Query(None, description="Summary column names, comma-separated"),
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: PredictionStoreManager = Depends(self._get_prediction_manager),
        ):
            """A slice of one outcome at one level: four options, as the specification says.

            Rows are places and months; columns are the place's id and names, `month`, and the
            outcome's summary columns (all, or the ones named). Never samples.
            """
            if level not in outcome_table.LEVELS:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown level {level!r}. Levels: {', '.join(outcome_table.LEVELS)}",
                )
            try:
                parsed_places = _parse_places(places)
                parsed_months = _parse_months(months)
                asked_columns = _parse_names(columns)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))
            try:
                forecast_ds = self._get_latest_dataset(manager, x_api_key, "forecast")
                if outcome not in outcome_table.outcomes(forecast_ds):
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"Unknown outcome {outcome!r}. Outcomes: "
                            f"{', '.join(outcome_table.outcomes(forecast_ds))}"
                        ),
                    )
                value_columns = outcome_table.value_columns(outcome)
                if asked_columns is not None:
                    unknown = [c for c in asked_columns if c not in value_columns]
                    if unknown:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Unknown columns {unknown}. Columns: {', '.join(value_columns)}",
                        )
                    value_columns = [c for c in value_columns if c in set(asked_columns)]
                try:
                    historical_ds = self._get_latest_dataset(manager, x_api_key, "historical")
                except HTTPException as e:
                    if e.status_code in (401, 403):
                        raise  # a refused key is not a missing history (C-268)
                    historical_ds = None
                    logger.warning("slice: no historical artifact; recorded deaths served as empty")

                identity = outcome_table.place_identity(forecast_ds, level)
                if parsed_places is not None:
                    unknown = [p for p in parsed_places if p not in identity.index]
                    if unknown:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Unknown place ids at level {level!r}: {unknown[:20]}",
                        )
                all_months = outcome_table.served_months(forecast_ds, historical_ds)
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

                # Decide before computing (C-284): the size follows from the request alone.
                n_rows = (
                    len(parsed_places) if parsed_places is not None else len(identity)
                ) * (len(parsed_months) if parsed_months is not None else len(all_months))
                n_columns = len(outcome_table.IDENTITY_COLUMNS[level]) + 1 + len(value_columns)
                estimated = estimate_records_bytes(n_rows, n_columns, 0)
                budget = _max_response_bytes()
                if estimated > budget:
                    raise HTTPException(
                        status_code=413,
                        detail=_too_large_detail(n_rows, estimated, budget),
                    )

                table = outcome_table.build_outcome_table(
                    forecast_ds, historical_ds, level, outcome,
                    places=parsed_places, months=parsed_months,
                )
                table = table[outcome_table.IDENTITY_COLUMNS[level] + ["month"] + value_columns]
                return {
                    "level": level,
                    "outcome": outcome,
                    "columns": table.columns.tolist(),
                    "rows": convert_numpy_types(table.astype(object).to_dict(orient="records")),
                }
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
            the check raises). *Data* freshness is reported in the body: a served forecast
            older than the SLA sets ``status="degraded"`` (service is up, the forecast is
            stale) so an external monitor can page on the body without conflating it with a
            service outage.
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
                # S3 (#246, C-50): a stale forecast degrades health rather than logging silently.
                # #290 sibling: freshness must be about the SERVED / newest-manifested run, not the
                # store's newest metadata record — which can still be a superseded legacy artifact
                # (e.g. the March orange_ensemble), falsely reading stale while a 1-day-old wire run
                # is being served. Prefer the served run's created_at, then the newest manifest's
                # completion time, then (only if neither exists) the legacy provenance record.
                try:
                    served = self._dataset_service.served_forecast_provenance()
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
                # S4 (#249, ADR-033 §6): a bounded grace fallback (the newest manifested run was
                # refused, a last-good run is being served) is a degraded state — surface it and flip
                # status, even when the served last-good is itself still within the freshness SLA.
                serving_state = self._dataset_service.forecast_serving_state()
                if serving_state is not None:
                    health["forecast_serving_state"] = serving_state
                    # `cached_unverified` records that the *last* forecast was served while the
                    # store could not be asked. Once this probe reaches the store again it
                    # describes a past response, not the service: show it, but do not page on
                    # it — the next forecast request re-verifies the cache (#158 review).
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
                served_entry = self._dataset_service.served_forecast_entry()
                if served_entry is not None:
                    health["served_entry"] = served_entry
                return health
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("Health check failed")
                raise HTTPException(
                    status_code=503,
                    detail="Service unhealthy"  # the cause goes to the log, not the response (C-264)
                ) from e

        

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._is_running = False
        self._shutdown()
        sys.exit(0)

    def _startup(self):
        """Initialize and start the API server."""
        logger.info("Starting CRAF'd API server...")
        
        try:
            # Start the server
            logger.info(f"Starting server on {self._server_config['host']}:{self._server_config['port']}")
            
            workers = self._server_config["workers"]
            reload_enabled = self._server_config["reload"]
            
            # When using workers > 1 or reload, uvicorn requires an import string
            if workers > 1 or reload_enabled:
                # Cannot use both reload and workers > 1 simultaneously
                if reload_enabled and workers > 1:
                    logger.warning("Cannot use reload with multiple workers. Disabling reload.")
                    reload_enabled = False
                
                # Use import string format for multi-worker or reload mode
                uvicorn.run(
                    "views_crafdapi.managers.api:create_app",
                    host=self._server_config["host"],
                    port=self._server_config["port"],
                    reload=reload_enabled,
                    workers=workers if not reload_enabled else 1,
                    log_level="info",
                    factory=True,
                )
            else:
                # Single worker mode can use the app object directly
                uvicorn.run(
                    self.app,
                    host=self._server_config["host"],
                    port=self._server_config["port"],
                    reload=False,
                    workers=1,
                    log_level="info"
                )
            
        except Exception as e:
            logger.error(f"Failed to start API server: {e}")
            raise

    def _shutdown(self):
        """Gracefully shutdown the API server."""
        logger.info("Shutting down CRAF'd API server...")
        
        try:
            # Clear manager cache
            for api_key_hash, managers in self._manager_cache.items():
                # Clear any resources held by managers
                if isinstance(managers, dict):
                    managers.clear()
            
            self._manager_cache.clear()
            self._dataframe_cache.clear()
            self._file_cache.clear()
            logger.info("Cleared in-memory caches")

            # C-66: shutdown must NOT delete the on-disk dataset cache.
            # `CrafdDiskCacheManager` (cache/datasets/) is a durable, 3.5-week
            # cross-restart cache (ADR-011); wiping it on shutdown forced a full
            # cold rebuild on every restart under a systemd Restart=always service.
            # Shutdown releases only ephemeral, process-local state. Purging the
            # durable cache is a deliberate, explicit operation
            # (`_maintenance(clear_cache=True)`), never a shutdown side-effect;
            # version-based invalidation (C-138) lives in `disk_cache.read`.

            # Close any open file handles or database connections
            logger.info("Cleaning up resources...")
            
            self._is_running = False
            logger.info("CRAF'd API server shutdown complete")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            raise


    def _health_check(self):
        """Perform health checks on the API server."""
        logger.info("Performing health check...")
        
        health_status = {
            "status": "healthy",
            "checks": {}
        }
        
        try:
            # Check if server is running
            health_status["checks"]["server"] = "running" if self._is_running else "stopped"
            
            # Check manager cache
            health_status["checks"]["manager_cache_size"] = len(self._manager_cache)
            
            # Check cache directory
            if hasattr(self._model_path, 'cache'):
                cache_exists = self._model_path.cache.exists()
                health_status["checks"]["cache"] = "available" if cache_exists else "unavailable"
                
            logger.info(f"Health check complete: {health_status}")
            return health_status
            
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            health_status["status"] = "unhealthy"
            health_status["error"] = str(e)
            return health_status

    def _maintenance(self):
        """Perform maintenance tasks on the API."""
        logger.info("Starting maintenance tasks...")
        
        try:
            # Deliberate, opt-in purge of the durable on-disk cache (C-66). This is
            # the *explicit* way to wipe the persistent dataset cache — gated on the
            # `clear_cache` config, never automatic. Shutdown does NOT do this.
            if self.configs.get('clear_cache', False):
                logger.info("Clearing cache...")
                if hasattr(self._model_path, 'cache') and self._model_path.cache.exists():
                    import shutil
                    shutil.rmtree(self._model_path.cache)
                    self._model_path.cache.mkdir(parents=True, exist_ok=True)
                    logger.info("Cache cleared successfully")
            
            # Clear manager cache
            if self.configs.get('clear_manager_cache', False):
                logger.info("Clearing manager cache...")
                self._manager_cache.clear()
                self._dataframe_cache.clear()
                self._file_cache.clear()
                logger.info("Manager cache cleared successfully")
                
            logger.info("Maintenance tasks completed successfully")
            
        except Exception as e:
            logger.error(f"Maintenance tasks failed: {e}")
            raise


# Module-level app instance for uvicorn import string (required for multi-worker mode)
_fao_manager: Optional[CrafdApiManager] = None
app: Optional[FastAPI] = None


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
        # The API is a read-only CONSUMER, not a model trainer, so it must boot from a
        # clean checkout that has no apis/un_crafd/ model-training tree — that directory is
        # gitignored (a pure runtime artifact holding cache/ and logs/). With the default
        # validate=True, APIPathManager both (a) raises FileNotFoundError when the tree is
        # absent and (b) returns None for runtime dirs like .cache, which then crashes
        # CrafdDiskCacheManager (`None / "datasets"`). validate=False makes .cache a real
        # Path the disk-cache manager creates on demand. Regression-tested by
        # tests/test_create_app_boot.py (the from_config test seam did not cover this path).
        model_path = APIPathManager(CONSUMER_DOCUMENT_NAME, validate=False)
        _fao_manager = CrafdApiManager(model_path=model_path)
        app = _fao_manager.app
    return app


