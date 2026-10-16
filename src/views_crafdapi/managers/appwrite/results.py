"""Value objects returned across the Appwrite client — plain dataclasses, no SDK dependency.

Extracted from the appwrite god-module (epic #325 S9).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class OperationResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    code: Optional[str] = None
    # The HTTP status of a failed SDK call (`AppwriteException.code`): 401/403 not authorised,
    # 0 or None no response at all, 5xx server-side. `code` above is Appwrite's *type* string.
    status: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "code": self.code,
            "status": self.status,
        }


@dataclass
class FileMetadata:
    fileId: str
    bucketId: str
    filename: str
    mime_type: str = "application/octet-stream"
    uploaded_at: str = field(default_factory=lambda: datetime.now().isoformat())
    file_size: Optional[int] = None
    file_hash: Optional[str] = None
