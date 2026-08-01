from views_crafdapi.managers.model import APIManager, APIPathManager
import logging
import pandas as pd
from dataclasses import dataclass
from typing import Optional, Dict, Any
from pathlib import Path
import io
import os
import shutil
import tempfile
import time
from views_crafdapi.managers.appwrite import AppWriteFileManager, AppwriteConfig
from views_crafdapi.managers.disk_cache import CrafdDiskCacheManager
from views_crafdapi.managers.dataset_service import DatasetService
from views_crafdapi.managers.prediction import PredictionStoreManager
from views_crafdapi.managers import freshness
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.version import version_info

from fastapi import FastAPI, HTTPException, Depends, Query, Header
from fastapi.responses import FileResponse, StreamingResponse
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


_REQUIRED_APPWRITE_ENV_VARS = [
    "APPWRITE_ENDPOINT",
    "APPWRITE_DATASTORE_PROJECT_ID",
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
    registry's declared value — the registry is canonical. Fail loud naming the exact variable(s).

    Transition-safe: if the registry is not reachable in this environment, only the presence check
    above runs (the central live validator is deferred — A7). This is the in-process preflight only.
    """
    path = os.getenv("APPWRITE_REGISTRY", "").strip()
    if not path or not os.path.isfile(path):
        return
    coords = _registry_coordinates(path)
    mismatches = [
        f"{name}={os.getenv(name)!r} but the registry declares {expected!r}"
        for name, expected in coords.items()
        if os.getenv(name) is not None and os.getenv(name) != expected
    ]
    if mismatches:
        msg = (
            "APPWRITE coordinate(s) disagree with the PLATFORM-001 registry (þing-01 #278 / D6) — "
            "the registry is canonical, fix the environment or update the registry by PR: "
            + "; ".join(mismatches)
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


# HTTP (de)serialization helpers live in `serialization.py` (SRP; epic #144 / C-36).
# Re-exported here so existing importers (`from ...managers.api import dataframe_to_dict`,
# etc.) keep resolving; the routes below call them by name.
from views_crafdapi.managers.serialization import (  # noqa: E402
    dataframe_to_dict,
    parse_list_param,
    parse_string_list_param,
)
from views_crafdapi.forecast.serialize.json_contract import to_consumer_columns  # noqa: E402
from views_crafdapi.forecast.serialize.bulk_parquet import write_bulk_parquet  # noqa: E402

class CrafdApiManager(APIManager):
    """
    Manages the CRAF'd API lifecycle including startup, shutdown, and maintenance.
    """

    def __init__(
        self,
        model_path: APIPathManager,
        wandb_notifications: bool = False,
    ) -> None:
        """
        Initializes the CrafdApiManager.

        Args:
            model_path (APIPathManager): The path manager for the API.
            wandb_notifications (bool, optional): Enable or disable Weights & Biases notifications. Defaults to False.
        """
        super().__init__(
            model_path=model_path,
            wandb_notifications=wandb_notifications,
        )

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
            version="0.1.0",
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
            endpoint=env["APPWRITE_ENDPOINT"],
            project_id=env["APPWRITE_DATASTORE_PROJECT_ID"],
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
                raise HTTPException(
                    status_code=401,
                    detail=f"Invalid API key: {validation_result.error or 'Authentication failed'}"
                )
            
            return manager
            
        except ValueError as e:
            raise HTTPException(
                status_code=500,
                detail=str(e)
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=401,
                detail=f"Invalid API key or Appwrite configuration: {str(e)}"
            )

        
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
        
        @self.app.get("/")
        async def root():
            return {
            "message": "CRAF'd Prediction API", 
            "note": "Include 'X-API-Key' header with your Appwrite API key in all requests",
            "endpoints": {
                # Historical endpoints by level
                "historical_latest": "/data/historical/latest",
                "historical_pg_subset": "/pg/data/historical/subset",
                "historical_country_subset": "/country/data/historical/subset",
                "historical_gaul0_subset": "/gaul0/data/historical/subset",
                "historical_gaul1_subset": "/gaul1/data/historical/subset",
                "historical_gaul2_subset": "/gaul2/data/historical/subset",
                
                # Forecast endpoints by level
                "forecast_latest": "/data/forecast/latest",
                "forecast_bulk": "/data/forecast/bulk",
                "forecast_pg_subset": "/pg/data/forecast/subset",
                "forecast_country_subset": "/country/data/forecast/subset",
                "forecast_gaul0_subset": "/gaul0/data/forecast/subset",
                "forecast_gaul1_subset": "/gaul1/data/forecast/subset",
                "forecast_gaul2_subset": "/gaul2/data/forecast/subset",
                
                # Historical HDI-MAP endpoints by level
                "historical_pg_hdi_map": "/pg/analysis/historical/hdi-map",
                "historical_country_hdi_map": "/country/analysis/historical/hdi-map",
                "historical_gaul0_hdi_map": "/gaul0/analysis/historical/hdi-map",
                "historical_gaul1_hdi_map": "/gaul1/analysis/historical/hdi-map",
                "historical_gaul2_hdi_map": "/gaul2/analysis/historical/hdi-map",
                
                # Forecast HDI-MAP endpoints by level
                "forecast_pg_hdi_map": "/pg/analysis/forecast/hdi-map",
                "forecast_country_hdi_map": "/country/analysis/forecast/hdi-map",
                "forecast_gaul0_hdi_map": "/gaul0/analysis/forecast/hdi-map",
                "forecast_gaul1_hdi_map": "/gaul1/analysis/forecast/hdi-map",
                "forecast_gaul2_hdi_map": "/gaul2/analysis/forecast/hdi-map",
                
                # File management endpoints
                "files": "/files/{bucket_id}",
                "file_download": "/files/{bucket_id}/{file_id}/download",
                "file_cached": "/files/{bucket_id}/{file_id}/cached",
                "file_info": "/files/{bucket_id}/{file_id}/info",
                "cache_stats": "/cache/stats",
                "clear_cache": "/cache (DELETE)",
                "provenance": "/provenance/{category}",
                "health": "/health",
                "liveness_ping": "/ping",
                "version": "/version"
            }
        }

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
        @self.app.get("/data/historical/latest")
        async def get_latest_historical_dataframe(
            force_refresh: bool = Query(False, description="Force refresh the cache"),
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: PredictionStoreManager = Depends(self._get_prediction_manager)
        ):
            """Get the latest historical dataframe from the prediction bucket."""
            try:
                df = self._get_latest_dataframe(manager, x_api_key, "historical", force_refresh)
                api_key_hash = self._get_api_key_hash(x_api_key)
                cache = self._dataframe_cache[api_key_hash]["historical"]
                
                result = {
                    "success": True,
                    "data": {
                        "dataframe": dataframe_to_dict(df),
                        "shape": df.shape,
                        "columns": df.columns.tolist(),
                        "file_id": cache["file_id"],
                        "timestamp": cache["timestamp"],
                        "category": "historical"
                    }
                }
                
                return result
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
        
        # Forecast data endpoints
        @self.app.get("/data/forecast/latest")
        async def get_latest_forecast_dataframe(
            force_refresh: bool = Query(False, description="Force refresh the cache"),
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: PredictionStoreManager = Depends(self._get_prediction_manager)
        ):
            """Get the latest forecast dataframe from the prediction bucket."""
            try:
                df = self._get_latest_dataframe(manager, x_api_key, "forecast", force_refresh)
                api_key_hash = self._get_api_key_hash(x_api_key)
                cache = self._dataframe_cache[api_key_hash]["forecast"]
                
                result = {
                    "success": True,
                    "data": {
                        "dataframe": dataframe_to_dict(df),
                        "shape": df.shape,
                        "columns": df.columns.tolist(),
                        "file_id": cache["file_id"],
                        "timestamp": cache["timestamp"],
                        "category": "forecast"
                    }
                }
                
                return result
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/data/forecast/bulk")
        async def get_forecast_bulk_parquet(
            force_refresh: bool = Query(False, description="Force refresh the cache"),
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: PredictionStoreManager = Depends(self._get_prediction_manager),
        ):
            """ADR-025 admin-1 bulk artifact (#222/S6): the full forecast table — MAP + 50/90/95
            HDIs + `severe_scenario` + historical `actual` — as one wide parquet, one row per
            `(month_id, admin1_code)`. The same numbers as the JSON API, packaged for a single
            load. Historical is best-effort: if no historical artifact is available, `s_actual`
            is served as NaN rather than failing the download."""
            try:
                forecast_ds = self._get_latest_dataset(manager, x_api_key, "forecast", force_refresh)
                try:
                    historical_ds = self._get_latest_dataset(
                        manager, x_api_key, "historical", force_refresh
                    )
                except HTTPException:
                    historical_ds = None
                    logger.warning("bulk parquet: no historical artifact — s_actual served as NaN")
                tmpdir = tempfile.mkdtemp()
                out = Path(tmpdir) / "unfao_forecast_bulk.parquet"
                write_bulk_parquet(out, forecast_ds, historical_ds)
                # Clean the per-request temp dir once the file has been streamed (no disk leak).
                return FileResponse(
                    out,
                    media_type="application/octet-stream",
                    filename="unfao_forecast_bulk.parquet",
                    background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
                )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # Provenance endpoint (C-86): expose the lineage of the currently-served artifact
        # without fetching the (heavy) dataframe — answers "which file, from which upstream
        # source/pipeline, is live right now?" so a silent source switch is auditable.
        @self.app.get("/provenance/{category}")
        async def get_provenance(
            category: str,
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: PredictionStoreManager = Depends(self._get_prediction_manager)
        ):
            """Return the provenance/lineage record for the latest artifact of `category`."""
            if category not in ("forecast", "historical"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid category: {category}. Expected 'forecast' or 'historical'."
                )
            try:
                provenance = manager.get_latest_provenance(filters={"category": category})
                if provenance is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"No {category} prediction files found in the bucket: {self._prediction_bucket_id}"
                    )
                data = provenance.to_dict()
                # S7 (#252, ADR-033 observability): expose the full *served* decision for forecasts,
                # not just the store's newest record: {artifact_id, mode, freshness, status,
                # refusal_reason?}. The served provenance (what is actually live) is authoritative;
                # it may differ from the store's newest artifact, so prefer it when present.
                if category == "forecast":
                    served = self._dataset_service.served_forecast_provenance()
                    if served:
                        data["artifact_id"] = served.get("file_id")
                        data["mode"] = served.get("mode")          # "wire" | "legacy"
                        data["status"] = served.get("status")      # producer-declared maturity
                        data["source"] = served.get("source", data.get("source"))
                        # #290: the served run is authoritative — overlay its identity/time labels
                        # too, so the record is internally consistent. Without this, the store's
                        # newest LEGACY record's name/filename/created_at bleed through and read as
                        # "still serving orange_ensemble" while a wire run is in fact live.
                        for _k in ("name", "filename", "created_at", "run_id"):
                            if served.get(_k) is not None:
                                data[_k] = served[_k]
                        # #290 hardening: a wire run assembled by a pre-#290 build stored no
                        # name/filename in its cached provenance (the disk cache survives restart,
                        # C-66), so after a deploy those keys stay absent from `served` until a
                        # re-ingest. Reconstruct them from the run's own source/run_id so a stale
                        # legacy label can never survive a wire serve — self-heals on the next
                        # serve, no post-deploy force_refresh required.
                        if served.get("mode") == "wire":
                            if served.get("name") is None and served.get("source"):
                                data["name"] = served["source"]
                            if served.get("filename") is None and served.get("run_id"):
                                data["filename"] = served["run_id"]
                            # #290 full reconcile: the served wire run owns the WHOLE record —
                            # replace the store's newest LEGACY descriptive fields so nothing from a
                            # superseded artifact (its file_id, single-file hash, test description,
                            # or pred_ln_* targets) bleeds through. `targets` is None for a run
                            # cached by a pre-#290 build (not recoverable without a re-ingest) —
                            # honest-absent beats wrong.
                            if served.get("file_id"):
                                data["file_id"] = served["file_id"]
                            data["targets"] = served.get("targets")
                            data["file_hash"] = None  # a wire run is a manifest of many hashed shards, not one file
                            if served.get("run_id"):
                                data["description"] = f"Sampled-forecast wire-contract run {served['run_id']}"
                        created_at = served.get("created_at") or data.get("created_at")
                    else:
                        data["artifact_id"] = data.get("file_id")
                        data["mode"] = None
                        created_at = data.get("created_at")
                    # S3 (#246, ADR-033 §4): the freshness verdict of the served artifact.
                    data["freshness"] = freshness.forecast_freshness(
                        created_at, self._forecast_freshness_sla_days
                    )
                    # S4 (#249, ADR-033 §6): the bounded grace-fallback state. `degraded: True`
                    # means the newest run was refused and a last-good run is being served (or was
                    # refused past the SLA); its `reason` is surfaced as `refusal_reason`.
                    serving_state = self._dataset_service.forecast_serving_state()
                    if serving_state is not None:
                        data["serving_state"] = serving_state
                        if serving_state.get("degraded") and serving_state.get("reason"):
                            data["refusal_reason"] = serving_state["reason"]
                return {"success": True, "data": data}
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # Helper function for subset endpoints
        def create_subset_endpoint(category: str, level: Optional[str] = None):
            async def subset_endpoint(
                time_ids: Optional[str] = Query(None, description="Time IDs to include (comma-separated)"),
                features: Optional[str] = Query(None, description="Feature names to include (comma-separated)"),
                sample_idx: Optional[str] = Query(None, description="Sample indices to include (comma-separated)"),
                entity_ids: Optional[str] = Query(None, description="Entity IDs to include (comma-separated)"),
                with_metadata: bool = Query(True, description="Include geographic metadata"),
                aggregate: bool = Query(False, description="Aggregate to specified level"),
                force_refresh: bool = Query(False, description="Force refresh the cache"),
                x_api_key: str = Header(..., description="Appwrite API Key"),
                manager: PredictionStoreManager = Depends(self._get_prediction_manager)
            ):
                """Get a subset of the latest dataframe at the specified level."""
                try:
                    parsed_time_ids = parse_list_param(time_ids)
                    parsed_features = parse_string_list_param(features)
                    parsed_sample_idx = parse_list_param(sample_idx)
                    
                    # For country level, parse as strings; for others, parse as integers
                    if level == "country":
                        parsed_entity_ids = parse_string_list_param(entity_ids)
                    else:
                        parsed_entity_ids = parse_list_param(entity_ids)
                    
                    dataset = self._get_latest_dataset(manager, x_api_key, category, force_refresh)
                    
                    subset_df = dataset.get_subset_dataframe(
                        time_ids=parsed_time_ids,
                        features=parsed_features,
                        sample_idx=parsed_sample_idx,
                        entity_ids=parsed_entity_ids,
                        with_metadata=with_metadata,
                        level=level,
                        aggregate=aggregate
                    )
                    
                    result = {
                        "success": True,
                        "data": {
                            "dataframe": dataframe_to_dict(subset_df),
                            "shape": subset_df.shape,
                            "columns": subset_df.columns.tolist(),
                            "category": category,
                            "level": level or "pg",
                            "parameters": {
                                "time_ids": parsed_time_ids,
                                "features": parsed_features,
                                "sample_idx": parsed_sample_idx,
                                "entity_ids": parsed_entity_ids,
                                "with_metadata": with_metadata,
                                "aggregate": aggregate
                            }
                        }
                    }
                    
                    return result
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(status_code=500, detail=str(e))
            
            return subset_endpoint

        # Helper function for HDI-MAP endpoints
        def create_hdi_map_endpoint(category: str, level: Optional[str] = None):
            async def hdi_map_endpoint(
                alpha: float = Query(0.9, description="Credibility level for HDI (e.g., 0.9 for 90% HDI)"),
                time_ids: Optional[str] = Query(None, description="Time IDs to include (comma-separated)"),
                features: Optional[str] = Query(None, description="Feature names to include (comma-separated)"),
                sample_idx: Optional[str] = Query(None, description="Sample indices to include (comma-separated)"),
                entity_ids: Optional[str] = Query(None, description="Entity IDs to include (comma-separated)"),
                enforce_non_negative: bool = Query(False, description="Force MAP estimates to be non-negative"),
                with_metadata: bool = Query(True, description="Include geographic metadata"),
                aggregate: bool = Query(False, description="Aggregate to specified level"),
                force_refresh: bool = Query(False, description="Force refresh the cache"),
                x_api_key: str = Header(..., description="Appwrite API Key"),
                manager: PredictionStoreManager = Depends(self._get_prediction_manager)
            ):
                """Calculate HDI and MAP estimates at the specified level."""
                try:
                    parsed_time_ids = parse_list_param(time_ids)
                    parsed_features = parse_string_list_param(features)
                    parsed_sample_idx = parse_list_param(sample_idx)
                    
                    # For country level, parse as strings; for others, parse as integers
                    if level == "country":
                        parsed_entity_ids = parse_string_list_param(entity_ids)
                    else:
                        parsed_entity_ids = parse_list_param(entity_ids)
                    
                    dataset = self._get_latest_dataset(manager, x_api_key, category, force_refresh)
                    
                    hdi_map_df = dataset.calculate_hdi_map(
                        alpha=alpha,
                        time_ids=parsed_time_ids,
                        features=parsed_features,
                        sample_idx=parsed_sample_idx,
                        entity_ids=parsed_entity_ids,
                        enforce_non_negative=enforce_non_negative,
                        with_metadata=with_metadata,
                        level=level,
                        aggregate=aggregate
                    )

                    # ADR-025 consumer naming (#222/S4): the reduction emits internal var-keyed
                    # quantity columns; the sb/ns/os rename is applied ONCE here, on the forecast
                    # response — the single boundary where real forecast vars are guaranteed to
                    # conform (historical keeps its own columns).
                    if category == "forecast":
                        hdi_map_df = to_consumer_columns(hdi_map_df)

                    result = {
                        "success": True,
                        "data": {
                            "hdi_map": dataframe_to_dict(hdi_map_df),
                            "shape": hdi_map_df.shape,
                            "columns": hdi_map_df.columns.tolist(),
                            "category": category,
                            "level": level or "pg",
                            "parameters": {
                                "alpha": alpha,
                                "time_ids": parsed_time_ids,
                                "features": parsed_features,
                                "sample_idx": parsed_sample_idx,
                                "entity_ids": parsed_entity_ids,
                                "enforce_non_negative": enforce_non_negative,
                                "with_metadata": with_metadata,
                                "aggregate": aggregate
                            }
                        }
                    }
                    
                    return result
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(status_code=500, detail=str(e))
            
            return hdi_map_endpoint

        # Register subset endpoints for each level
        levels = [None, "country", "gaul0", "gaul1", "gaul2"]
        level_routes = {
            None: "pg",
            "country": "country",
            "gaul0": "gaul0",
            "gaul1": "gaul1",
            "gaul2": "gaul2"
        }
        
        for level in levels:
            route_prefix = level_routes[level]
            
            # Historical subset
            self.app.add_api_route(
                f"/{route_prefix}/data/historical/subset",
                create_subset_endpoint("historical", level),
                methods=["GET"],
                name=f"get_historical_subset_{route_prefix}"
            )
            
            # Forecast subset
            self.app.add_api_route(
                f"/{route_prefix}/data/forecast/subset",
                create_subset_endpoint("forecast", level),
                methods=["GET"],
                name=f"get_forecast_subset_{route_prefix}"
            )
            
            # Historical HDI-MAP
            self.app.add_api_route(
                f"/{route_prefix}/analysis/historical/hdi-map",
                create_hdi_map_endpoint("historical", level),
                methods=["GET"],
                name=f"calculate_historical_hdi_map_{route_prefix}"
            )
            
            # Forecast HDI-MAP
            self.app.add_api_route(
                f"/{route_prefix}/analysis/forecast/hdi-map",
                create_hdi_map_endpoint("forecast", level),
                methods=["GET"],
                name=f"calculate_forecast_hdi_map_{route_prefix}"
            )

        # File management endpoints (unchanged)
        @self.app.get("/files/{bucket_id}")
        async def list_files(
            bucket_id: str,
            limit: int = Query(100, ge=1, le=1000),
            offset: int = Query(0, ge=0),
            search: Optional[str] = None,
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: AppWriteFileManager = Depends(self._get_appwrite_manager)
        ):
            """List all files in a bucket with pagination."""
            try:
                queries = []
                if search:
                    queries.append(f"search('name','{search}')")
                
                result = manager.list_files(
                    bucket_id=bucket_id,
                    queries=queries,
                    limit=limit,
                    offset=offset
                )
                
                if not result.success:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Error listing files: {result.error or 'Unknown error'}"
                    )
                
                data = result.data if isinstance(result.data, dict) else {}
                return {
                    "bucket_id": bucket_id,
                    "files": data.get("files", []),
                    "total": data.get("total", 0),
                    "pagination": {
                        "limit": limit,
                        "offset": offset
                    }
                }
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/files/{bucket_id}/{file_id}/info")
        async def get_file_info(
            bucket_id: str,
            file_id: str,
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: AppWriteFileManager = Depends(self._get_appwrite_manager)
        ):
            """Get file metadata without downloading the file."""
            try:
                result = manager.get_file(bucket_id, file_id)
                
                if not result.success:
                    raise HTTPException(
                        status_code=404,
                        detail=f"File not found: {result.error or 'Unknown error'}"
                    )
                
                return {
                    "file_id": file_id,
                    "bucket_id": bucket_id,
                    "metadata": result.data
                }
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/files/{bucket_id}/{file_id}/download")
        async def download_file(
            bucket_id: str,
            file_id: str,
            use_cache: bool = Query(True, description="Use cached version if available"),
            download: bool = Query(False, description="Force file download with attachment header"),
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: AppWriteFileManager = Depends(self._get_appwrite_manager)
        ):
            """Download a file from Appwrite storage."""
            try:
                file_info = manager.get_file(bucket_id, file_id)
                if not file_info.success:
                    raise HTTPException(
                        status_code=404,
                        detail=f"File not found: {file_info.error or 'Unknown error'}"
                    )
                
                metadata = file_info.data if isinstance(file_info.data, dict) else {}
                filename = metadata.get('name', file_id)
                
                result = manager.download_file(
                    bucket_id=bucket_id,
                    file_id=file_id,
                    use_cache=use_cache
                )
                
                if not result.success:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Download failed: {result.error or 'Unknown error'}"
                    )
                
                media_type = "application/octet-stream"
                file_extension = os.path.splitext(filename)[1].lower()
                extension_to_media_type = {
                    '.pdf': 'application/pdf',
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.png': 'image/png',
                    '.gif': 'image/gif',
                    '.txt': 'text/plain',
                    '.json': 'application/json',
                    '.zip': 'application/zip',
                }
                media_type = extension_to_media_type.get(file_extension, "application/octet-stream")
                
                dl_data = result.data if isinstance(result.data, dict) else {}
                file_bytes = dl_data.get("file_bytes")

                if download:
                    return StreamingResponse(
                        io.BytesIO(file_bytes),
                        media_type=media_type,
                        headers={"Content-Disposition": f"attachment; filename={filename}"}
                    )
                else:
                    return StreamingResponse(
                        io.BytesIO(file_bytes),
                        media_type=media_type,
                        headers={"Content-Disposition": f"inline; filename={filename}"}
                    )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/files/{bucket_id}/{file_id}/cached")
        async def get_cached_file(
            bucket_id: str,
            file_id: str,
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: AppWriteFileManager = Depends(self._get_appwrite_manager)
        ):
            """Get file from cache if available (fastest option)."""
            try:
                result = manager.cache_manager.get_cached_file_path(bucket_id, file_id)
                
                if not result.success:
                    raise HTTPException(
                        status_code=404,
                        detail=f"File not in cache: {result.error or 'Unknown error'}"
                    )
                
                file_info = manager.get_file(bucket_id, file_id)
                if file_info.success:
                    fi_data = file_info.data if isinstance(file_info.data, dict) else {}
                    filename = fi_data.get('name', file_id)
                else:
                    filename = file_id

                cache_data = result.data if isinstance(result.data, dict) else {}
                cache_path = cache_data.get("cache_path")
                if not cache_path:
                    raise HTTPException(status_code=500, detail="Cache path not found in result")
                return FileResponse(
                    path=cache_path,
                    filename=filename,
                    media_type='application/octet-stream'
                )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/cache/stats")
        async def get_cache_stats(
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: AppWriteFileManager = Depends(self._get_appwrite_manager)
        ):
            """Get cache statistics for the current API key."""
            try:
                stats = manager.get_cache_stats()
                return stats
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.delete("/cache")
        async def clear_cache(
            bucket_id: Optional[str] = None,
            older_than_hours: Optional[int] = None,
            x_api_key: str = Header(..., description="Appwrite API Key"),
            manager: AppWriteFileManager = Depends(self._get_appwrite_manager)
        ):
            """Clear file cache for the current API key."""
            try:
                result = manager.clear_cache(
                    bucket_id=bucket_id,
                    older_than_hours=older_than_hours
                )
                return result.to_dict()
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

            HTTP status stays about *service* health (503 iff Appwrite is unreachable). *Data*
            freshness is reported in the body: a served forecast older than the SLA sets
            ``status="degraded"`` (service is up, the forecast is stale) so an external monitor
            can page on the body without conflating it with a service outage.
            """
            try:
                result = manager.list_buckets(limit=1)
                health = {
                    "status": "healthy",
                    "appwrite_connected": result.success,
                    "cache_stats": manager.get_cache_stats(),
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
                        except Exception:
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
                except Exception as e:  # freshness is best-effort; never fail the health check on it
                    logger.warning("Forecast freshness check failed (non-blocking): %s", e)
                    health["forecast_freshness"] = {"error": str(e)}
                # S4 (#249, ADR-033 §6): a bounded grace fallback (the newest manifested run was
                # refused, a last-good run is being served) is a degraded state — surface it and flip
                # status, even when the served last-good is itself still within the freshness SLA.
                serving_state = self._dataset_service.forecast_serving_state()
                if serving_state is not None:
                    health["forecast_serving_state"] = serving_state
                    if serving_state.get("degraded"):
                        health["status"] = "degraded"
                        logger.warning(
                            "Forecast serving DEGRADED: newest run refused (reason=%s); "
                            "grace-fallback available=%s",
                            serving_state.get("reason"), serving_state.get("fallback_available"),
                        )
                return health
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"Service unhealthy: {str(e)}"
                )

        

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
        model_path = APIPathManager("un_crafd", validate=False)
        _fao_manager = CrafdApiManager(model_path=model_path)
        app = _fao_manager.app
    return app


