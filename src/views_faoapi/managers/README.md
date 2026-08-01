# Managers Module

This module contains service managers for the FAO API including API lifecycle management, cloud storage integration, prediction handling, and logging configuration.

## Module Structure

```
managers/
├── __init__.py
├── api.py           # FAO API manager and route registration
├── appwrite.py      # Appwrite cloud storage client
├── model.py         # Path management base classes
├── prediction.py    # Prediction file storage manager
├── log.py           # Logging configuration
└── README.md
```

---

## api.py

The main API manager implementing FastAPI routes and lifecycle management.

### `FAOApiManager`

Central manager for the FAO Forecast API. Handles authentication, caching, route registration, and request processing.

#### Class Hierarchy

```
ModelPathManager
    │
    └── APIPathManager
            │
            └── APIManager
                    │
                    └── FAOApiManager
```

#### Constructor

```python
def __init__(
    self,
    api_key: Optional[str] = None,
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
    workers: int = 1,
    **kwargs
)
```

**Parameters:**
- `api_key`: Appwrite API key for authentication
- `host`: Server bind address
- `port`: Server port
- `reload`: Enable auto-reload (development)
- `workers`: Number of uvicorn workers

#### Key Methods

##### `run`

```python
def run(self) -> None
```

Starts the API server with uvicorn.

```python
manager = FAOApiManager(api_key="...", port=8000, workers=4)
manager.run()
```

##### `_get_latest_dataset`

```python
async def _get_latest_dataset(
    self,
    category: str,
    api_key: str,
    force_refresh: bool = False
) -> FAO_PGMDataset
```

Fetches and caches the latest prediction dataset.

**Caching Behavior:**
- Results cached by (api_key_hash, category)
- Cache expires after 1 hour
- `force_refresh=True` bypasses cache

##### `_register_routes`

```python
def _register_routes(self) -> None
```

Registers all API routes dynamically based on configured levels.

---

### Utility Functions

#### `parse_list_param`

```python
def parse_list_param(
    param: Optional[str],
    element_type: type = int
) -> Optional[List]
```

Parses comma-separated query parameters.

```python
parse_list_param("410,411,412", int)  # [410, 411, 412]
parse_list_param("SOM,ETH,KEN", str)  # ["SOM", "ETH", "KEN"]
```

#### `convert_numpy_types`

```python
def convert_numpy_types(obj: Any) -> Any
```

Recursively converts NumPy types to JSON-serializable Python types.

**Handles:**
- `np.integer` → `int`
- `np.floating` → `float` (NaN/Inf → `None`)
- `np.ndarray` → `list`
- `np.bool_` → `bool`

#### `flatten_numeric_list_columns`

```python
def flatten_numeric_list_columns(df: pd.DataFrame) -> pd.DataFrame
```

Flattens single-element lists/arrays in numeric columns.

```python
# Input: [1.5] → Output: 1.5
# Input: [1.5, 2.0] → Output: [1.5, 2.0] (unchanged)
```

#### `dataframe_to_dict`

```python
def dataframe_to_dict(df: pd.DataFrame) -> dict
```

Converts DataFrame to JSON-serializable dictionary.

**Process:**
1. Reset index
2. Flatten single-element arrays
3. Convert NumPy types
4. Return as dict with "records" orientation

#### `create_app`

```python
def create_app() -> FastAPI
```

Factory function for creating the FastAPI application instance.

**Used for multi-worker deployment:**
```bash
uvicorn views_faoapi.managers.api:app --workers 4
```

---

### Route Registration

Routes are dynamically registered for each geographic level:

```python
LEVELS = ["pg", "country", "gaul0", "gaul1", "gaul2"]
CATEGORIES = ["historical", "forecast"]
```

**Generated Routes:**

| Template | Example |
|----------|---------|
| `/{level}/data/{category}/subset` | `/country/data/forecast/subset` |
| `/{level}/analysis/{category}/hdi-map` | `/gaul1/analysis/historical/hdi-map` |

---

## appwrite.py

Appwrite SDK wrapper for cloud storage operations.

### `AppwriteConfig`

Configuration dataclass for Appwrite connections.

```python
@dataclass
class AppwriteConfig:
    endpoint: str
    project_id: str
    bucket_id: str
    database_id: Optional[str] = None
    collection_id: Optional[str] = None
```

### `AuthMethod`

Authentication method. **Single-mode: API key only** — the session-auth path was retired
(þing-01 #274 / PLATFORM-001; no serving path ever used it, and the identity model is now
single-mode in fact).

```python
class AuthMethod(Enum):
    API_KEY = "api_key"
```

### `OperationResult`

Standardized result container for Appwrite operations.

```python
@dataclass
class OperationResult:
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
```

### `AppWriteFileManager`

High-level file operations manager for Appwrite storage.

#### Constructor

```python
def __init__(
    self,
    config: AppwriteConfig,
    auth_method: AuthMethod = AuthMethod.API_KEY,
    api_key: Optional[str] = None
)
```

#### Methods

##### `list_files`

```python
def list_files(
    self,
    limit: int = 100,
    offset: int = 0,
    search: Optional[str] = None
) -> OperationResult
```

Lists files in the configured bucket.

```python
result = manager.list_files(limit=50, search="forecast")
if result.success:
    for file in result.data:
        print(file['name'])
```

##### `download_file`

```python
def download_file(self, file_id: str) -> OperationResult
```

Downloads file content as bytes.

##### `get_file_info`

```python
def get_file_info(self, file_id: str) -> OperationResult
```

Retrieves file metadata.

##### `upload_file`

```python
def upload_file(
    self,
    file_path: str,
    file_name: Optional[str] = None,
    permissions: Optional[List[str]] = None
) -> OperationResult
```

Uploads a file to the bucket.

##### `delete_file`

```python
def delete_file(self, file_id: str) -> OperationResult
```

Deletes a file from storage.

---

## model.py

Base classes for path and model management.

### `ModelPathManager`

Manages file system paths and model discovery.

#### Methods

##### `find_project_root`

```python
@staticmethod
def find_project_root(marker: str = "pyproject.toml") -> Path
```

Finds the project root by searching for a marker file.

##### `validate_model_name`

```python
def validate_model_name(self, model_name: str) -> bool
```

Validates that a model name exists in the configuration.

##### `generate_hash`

```python
@staticmethod
def generate_hash(data: str, length: int = 8) -> str
```

Generates a truncated SHA-256 hash.

```python
hash_id = ModelPathManager.generate_hash("api_key_value")
# Returns: "a1b2c3d4"
```

### `APIPathManager`

Extends `ModelPathManager` with API-specific path handling.

#### Methods

##### `get_api_path`

```python
def get_api_path(self, *parts: str) -> Path
```

Constructs paths relative to the API root.

### `APIManager`

Base class for API management with lifecycle methods.

#### Methods

##### `initialize`

```python
def initialize(self) -> None
```

Initializes API resources (override in subclass).

##### `shutdown`

```python
def shutdown(self) -> None
```

Cleans up API resources (override in subclass).

##### `run`

```python
def run(self) -> None
```

Starts the API server.

---

## prediction.py

Manages prediction file storage and retrieval via Appwrite.

### `PredictionMetadata`

Metadata model for prediction files.

```python
@dataclass
class PredictionMetadata:
    file_id: str
    filename: str
    category: str              # "historical" or "forecast"
    model_name: str
    timestamp: float
    file_size: int
    checksum: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None
```

### `PredictionStoreManager`

High-level manager for prediction file operations.

#### Constructor

```python
def __init__(
    self,
    file_manager: AppWriteFileManager,
    cache_dir: Optional[Path] = None
)
```

#### Methods

##### `upload_predictions`

```python
def upload_predictions(
    self,
    file_path: Path,
    category: str,
    model_name: str,
    extra_metadata: Optional[Dict[str, Any]] = None
) -> OperationResult
```

Uploads a prediction file with metadata.

```python
result = manager.upload_predictions(
    file_path=Path("predictions.parquet"),
    category="forecast",
    model_name="hydranet",
    extra_metadata={"version": "2.0"}
)
```

##### `get_predictions_by_metadata`

```python
def get_predictions_by_metadata(
    self,
    category: Optional[str] = None,
    model_name: Optional[str] = None,
    after_timestamp: Optional[float] = None
) -> List[PredictionMetadata]
```

Queries predictions by metadata filters.

```python
forecasts = manager.get_predictions_by_metadata(
    category="forecast",
    model_name="hydranet"
)
```

##### `download_prediction`

```python
def download_prediction(
    self,
    file_id: str,
    use_cache: bool = True
) -> OperationResult
```

Downloads a prediction file, optionally using local cache.

##### `get_latest_prediction`

```python
def get_latest_prediction(
    self,
    category: str,
    model_name: Optional[str] = None
) -> Optional[PredictionMetadata]
```

Retrieves the most recent prediction for a category.

---

## log.py

Logging configuration management.

### `LoggingModule`

Configures application logging from YAML configuration.

#### Constructor

```python
def __init__(
    self,
    config_path: Optional[Path] = None,
    default_level: int = logging.INFO
)
```

#### Methods

##### `get_logger`

```python
def get_logger(self, name: str) -> logging.Logger
```

Returns a configured logger instance.

```python
logging_module = LoggingModule()
logger = logging_module.get_logger("views_faoapi.api")
logger.info("API started")
```

#### Configuration File

Default location: `configs/logging.yaml`

```yaml
version: 1
disable_existing_loggers: false
formatters:
  standard:
    format: '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
handlers:
  console:
    class: logging.StreamHandler
    level: DEBUG
    formatter: standard
    stream: ext://sys.stdout
loggers:
  views_faoapi:
    level: INFO
    handlers: [console]
    propagate: false
root:
  level: WARNING
  handlers: [console]
```

---

## Caching Architecture

The API uses a multi-level caching strategy:

### Manager Cache

```python
_manager_cache: Dict[str, Tuple[FAOApiManager, float]]
```

- Keyed by hashed API key
- TTL: 1 hour
- Stores initialized managers with Appwrite clients

### DataFrame Cache

```python
_dataframe_cache: Dict[str, Tuple[FAO_PGMDataset, float]]
```

- Keyed by `(api_key_hash, category)`
- TTL: 1 hour
- Stores parsed prediction datasets

### File Cache

```python
_file_cache: Dict[str, bytes]
```

- Keyed by Appwrite file ID
- No TTL (cleared via `/cache` endpoint)
- Stores raw file bytes

---

## Usage Examples

### Creating an API Instance

```python
from views_faoapi.managers.api import FAOApiManager

# Development mode
api = FAOApiManager(
    api_key="your_appwrite_key",
    port=8000,
    reload=True,
    workers=1
)
api.run()
```

### Production Deployment

```bash
# Using uvicorn directly with multiple workers
uvicorn views_faoapi.managers.api:app --host 0.0.0.0 --port 8000 --workers 4
```

### Using AppWriteFileManager

```python
from views_faoapi.managers.appwrite import (
    AppWriteFileManager, AppwriteConfig, AuthMethod
)

config = AppwriteConfig(
    endpoint="https://cloud.appwrite.io/v1",
    project_id="your_project",
    bucket_id="predictions"
)

manager = AppWriteFileManager(
    config=config,
    auth_method=AuthMethod.API_KEY,
    api_key="your_api_key"
)

# List files
result = manager.list_files(search="forecast")
if result.success:
    for f in result.data:
        print(f"{f['name']} - {f['sizeOriginal']} bytes")
```

### Using PredictionStoreManager

```python
from views_faoapi.managers.prediction import PredictionStoreManager
from views_faoapi.managers.appwrite import AppWriteFileManager

file_manager = AppWriteFileManager(...)
prediction_manager = PredictionStoreManager(file_manager)

# Get latest forecast
latest = prediction_manager.get_latest_prediction(
    category="forecast",
    model_name="hydranet"
)

if latest:
    result = prediction_manager.download_prediction(latest.file_id)
    data = result.data  # bytes
```

---

## Error Handling

All managers use `OperationResult` for consistent error handling:

```python
result = manager.download_file(file_id)

if result.success:
    data = result.data
    metadata = result.metadata
else:
    error_message = result.error
    logging.error(f"Operation failed: {error_message}")
```

### Common Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `401 Unauthorized` | Invalid API key | Verify Appwrite API key |
| `404 Not Found` | File/bucket missing | Check bucket and file IDs |
| `Connection Error` | Network issue | Verify Appwrite endpoint |
| `Parse Error` | Invalid file format | Ensure files are valid parquet |

---

## See Also

- [Main README](../../README.md) - API overview and endpoints
- [Data README](../data/README.md) - Dataset handlers and statistics
- [WandB README](../wandb/README.md) - Monitoring utilities
