"""Where the service keeps its files, relative to the checkout it is started in."""
from pathlib import Path

import pyprojroot


def find_project_root(current_path: Path = None, marker: str = ".gitignore") -> Path:
    """The folder that holds ``marker``: the project root pyprojroot finds from the working
    directory if the marker is there, otherwise the nearest folder above that has it.

    Raises:
        FileNotFoundError: no folder up to the filesystem root holds the marker.
    """
    if current_path is None:
        current_path = Path(pyprojroot.here())
        if (current_path / marker).exists():
            return current_path
    try:
        current_path = Path(current_path).resolve().parent
        while current_path != current_path.parent:
            if (current_path / marker).exists():
                return current_path
            current_path = current_path.parent
    except (OSError, ValueError) as e:
        raise FileNotFoundError(
            f"{marker} not found in the directory hierarchy. "
            f"Unable to find project root. {current_path}"
        ) from e
    raise FileNotFoundError(
        f"{marker} not found in the directory hierarchy. "
        f"Searched from {current_path} to filesystem root."
    )


class APIPathManager:
    """The service's folders. ``apis/<name>/`` is a runtime folder, ignored by git and created
    on demand; nothing has to exist before the service starts.

    Attributes:
        model_name: the store-document name the service serves; every store query filters on it.
        root: the checkout the service was started in.
        dotenv: ``<root>/.env``, read at start if it exists.
        cache: ``<root>/apis/<name>/cache``, the prepared data and the bulk files.
        logging: ``<root>/apis/<name>/logs``.
    """

    def __init__(self, name: str) -> None:
        self.model_name = name
        self.root = find_project_root()
        self.dotenv = self.root / ".env"
        self.cache = self.root / "apis" / name / "cache"
        self.logging = self.root / "apis" / name / "logs"
