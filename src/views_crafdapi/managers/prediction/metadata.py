"""Prediction value objects: the metadata a producer stamps (PredictionMetadata),
the file identity/timestamps (PredictionFileMetadata), and the served-artifact
lineage record (PredictionProvenance, C-86)."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from views_crafdapi.methodology import METHODOLOGY_VERSION


@dataclass
class PredictionFileMetadata:
    file_id: str
    created_at: str
    updated_at: str


@dataclass
class PredictionProvenance:
    """C-86: a lineage record for the served forecast artifact. Captures *which* artifact
    is live and *which upstream pipeline* declared it. `source` reads a `source`/`pipeline`
    metadata field if the producer stamps one, else `"unknown"` — making a silent
    viewser→datafactory source switch visible instead of invisible."""

    file_id: str
    source: str
    created_at: str
    filename: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    targets: Optional[List[str]] = None
    description: Optional[str] = None
    file_hash: Optional[str] = None
    # ADR-023 / C-86: the crafdapi methodology that computes the published HDI/MAP from this
    # artifact. Bumped when a re-baselining change ships to production.
    methodology_version: str = METHODOLOGY_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_id": self.file_id,
            "source": self.source,
            "created_at": self.created_at,
            "filename": self.filename,
            "name": self.name,
            "category": self.category,
            "targets": self.targets,
            "description": self.description,
            "file_hash": self.file_hash,
            "methodology_version": self.methodology_version,
        }


class PredictionMetadata:
    def __init__(
        self,
        loa: str,
        name: str,
        type: str,
        targets: List[str],
        category: str,
        description: Optional[str] = None,
    ):
        if not isinstance(loa, str):
            raise TypeError("loa must be a string")
        if not isinstance(name, str):
            raise TypeError("name must be a string")
        if not isinstance(type, str):
            raise TypeError("type must be a string")
        if not isinstance(targets, list) or not all(
            isinstance(t, str) for t in targets
        ):
            raise TypeError("targets must be a list of strings")
        if description is not None and not isinstance(description, str):
            raise TypeError("description must be a string or None")
        if category not in ["forecast", "historical"]:
            raise ValueError(f"category must be either 'forecast' or 'historical'. Got: {category}")

        self.loa = loa
        self.name = name
        self.type = type
        self.targets = targets
        self.description = description
        self.category = category

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "loa": self.loa,
            "name": self.name,
            "type": self.type,
            "targets": self.targets,
            "category": self.category,
        }
        if self.description:
            data["description"] = self.description
        return data
