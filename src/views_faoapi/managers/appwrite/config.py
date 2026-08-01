"""Appwrite connection configuration + the auth-method enum.

Extracted from the appwrite god-module (epic #325 S9).
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Union

from views_faoapi.managers.model import ModelPathManager

from .constants import DEFAULT_CACHE_TTL_HOURS, DEFAULT_TIMEOUT_SECONDS


class AuthMethod(Enum):
    API_KEY = "api_key"
    # SESSION retired — þing-01 #274 / PLATFORM-001: the serving identity model is single-mode.


@dataclass(frozen=True)
class AppwriteConfig:
    # Core connection settings
    endpoint: str
    project_id: str
    # þing-01 #277 / PLATFORM-001 redaction clause: the credential must NEVER reach a log line in
    # ANY carrier. `repr=False` keeps the key out of the dataclass repr, so an accidental
    # `logger.…(config)` or an exception traceback showing the config cannot leak it.
    credentials: Union[str, Dict[str, str]] = field(repr=False)

    # Authentication settings
    auth_method: AuthMethod = AuthMethod.API_KEY
    allow_metadata_only_updates: bool = True

    # Cache settings
    cache_dir: Optional[str] = None
    cache_ttl_hours: int = DEFAULT_CACHE_TTL_HOURS

    # Storage settings
    bucket_id: str = "production_forecasts"
    bucket_name: Optional[str] = (
        None  # Will default to bucket_id with spaces if not provided
    )

    # Metadata settings
    collection_name: str = "Pipeline Forecasts"
    collection_id: Optional[str] = "pipeline_forecasts"  # Custom collection ID override
    database_name: Optional[str] = (
        "File Metadata"  # Will default to bucket_name + " Metadata" if not provided
    )
    database_id: Optional[str] = "file_metadata"  # Custom database ID override

    # Path manager
    path_manager: ModelPathManager = None

    # Network timeout (seconds) for all Appwrite SDK calls
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self):
        if isinstance(self.auth_method, str):
            object.__setattr__(self, "auth_method", AuthMethod(self.auth_method))

        if not self.bucket_name:
            object.__setattr__(self, "bucket_name", self.bucket_id.replace("_", " ").title())

        if not self.database_name:
            object.__setattr__(self, "database_name", f"{self.bucket_name} Metadata")
