"""The Appwrite client package.

Split from a single ~2000-line module (epic #325 S9) into one-concept-per-file submodules. This
``__init__`` re-exports the public surface so ``from views_crafdapi.managers.appwrite import X``
resolves unchanged.

The write/provision surface is now physically separate from the read surface: ``provisioning.py``
(the opt-in-OFF gate), the ``create_*`` DDL in ``metadata.py``, and the ``upload_*``/``delete_*``
methods in ``manager.py`` are what a *read-only* clone drops; ``reader``-side download/list/get and
the value objects stay. (The reader/writer *class* severance within ``manager.py`` is a follow-up.)
"""

# SDK classes re-exported at the package root (callers and test mocks reference them here; note the
# manager instantiates its own, so mocks must patch `...appwrite.manager.<Class>`).
from appwrite.client import Client
from appwrite.services.storage import Storage
from appwrite.services.databases import Databases
from appwrite.services.users import Users

from .config import AppwriteConfig, AuthMethod
from .auth import AuthManager, ApiKeyAuth, AuthFactory
from .results import OperationResult, FileMetadata
from .provisioning import ProvisioningError, ProvisioningDisabledError, _require_provisioning
from .sdk_compat import _as_dict, _get
from .file_cache import CacheManager, CacheMetadata, CacheValidationResult
from .metadata import MetadataManager
from .manager import AppWriteFileManager

__all__ = [
    "Client", "Storage", "Databases", "Users",
    "AppwriteConfig", "AuthMethod", "AuthManager", "ApiKeyAuth", "AuthFactory",
    "OperationResult", "FileMetadata",
    "ProvisioningError", "ProvisioningDisabledError", "_require_provisioning",
    "_as_dict", "_get",
    "CacheManager", "CacheMetadata", "CacheValidationResult",
    "MetadataManager", "AppWriteFileManager",
]
