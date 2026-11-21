"""The built bulk files, kept on disk until the forecast or the history they came from changes.

The specification says each bulk file is built once per forecast (#203). Building the grid file
takes about 40 seconds (measured 2026-11-21 on the delivered run), so it is built on the first
request for it and then served from disk.

A file is named by what it was built from: the store's file id of the forecast and of the history
(`source_file_id`, stamped on a dataset where it is loaded: `CrafdDiskCacheManager.read` and the
history's direct download). Every key and the last-good fallback load the same delivery under the
same id, so they share one file. A dataset without an id is never matched to a kept file: its file
is built for that one request and removed after it is sent.

The loader (`forecast_loader.py`) builds the files of a new delivery before it hands that delivery
to the routes, then removes the files of every other delivery, including any left by an earlier
process (`remove_other_deliveries`). A download of an old file that has already started keeps
reading it (on Linux an open file stays readable after it is removed). The routes only read files,
or build a missing one; they never remove any.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

from views_crafdapi.forecast.serialize import outcome_table, schema

logger = logging.getLogger(__name__)

# The level and outcome codes a file name may contain, mapped to themselves. A name is built from
# the value found here, never from the text a request sent, so no request can steer a path.
_LEVEL_NAMES = {level: level for level in outcome_table.LEVELS}
_OUTCOME_NAMES = {outcome: outcome for outcome in schema.SERIES}

# Store ids are letters, digits and a few marks; anything else is not used in a file name.
_SAFE_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def source_id(dataset) -> Optional[str]:
    """The store's file id the dataset was loaded from, or None when it is not known."""
    value = getattr(dataset, "source_file_id", None)
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None


class BulkFiles:
    """One file per level and outcome, for the forecast and history deliveries they came from."""

    def __init__(self, directory: Path):
        self._directory = Path(directory)

    def get(self, forecast_ds, historical_ds, level: str, outcome: str,
            build: Callable[[Path], None]) -> tuple[Path, bool]:
        """The file for `level` and `outcome`, built with `build(path)` unless it already exists.

        Returns the path and whether it is kept. A file that is not kept (a dataset without a
        store id) is the caller's to remove once it has been sent.
        """
        level_name, outcome_name = _LEVEL_NAMES.get(level), _OUTCOME_NAMES.get(outcome)
        if level_name is None or outcome_name is None:
            raise ValueError(f"unknown level or outcome: {level!r}, {outcome!r}")
        ids = (source_id(forecast_ds), source_id(historical_ds))
        self._directory.mkdir(parents=True, exist_ok=True)
        if None in ids:
            logger.warning("bulk file for %s %s built for one request: no store id", level_name, outcome_name)
            handle, scratch = tempfile.mkstemp(dir=self._directory, suffix=".once.parquet")
            os.close(handle)
            try:
                build(Path(scratch))
            except BaseException:
                Path(scratch).unlink(missing_ok=True)
                raise
            return Path(scratch), False

        delivery = f"{ids[0]}_{ids[1]}"
        path = self._directory / f"{outcome_name}_{level_name}__{delivery}.parquet"
        if path.exists():
            return path, True
        # Written under a temporary name and renamed when complete, so a failed build never
        # leaves a part-written file under the served name.
        handle, scratch = tempfile.mkstemp(dir=self._directory, suffix=".parquet.tmp")
        os.close(handle)
        try:
            build(Path(scratch))
            os.replace(scratch, path)
        except BaseException:
            Path(scratch).unlink(missing_ok=True)
            raise
        logger.info("bulk file built: %s", path.name)
        return path, True

    def remove_other_deliveries(self, keep) -> None:
        """Remove the kept files of every delivery except those in `keep`, a list of
        (forecast, history) dataset pairs. Files built for one request are left to it."""
        names = set()
        for forecast_ds, historical_ds in keep:
            ids = (source_id(forecast_ds), source_id(historical_ds))
            if None not in ids:
                names.add(f"__{ids[0]}_{ids[1]}.parquet")
        if not names or not self._directory.exists():
            return
        for old in self._directory.glob("*.parquet"):
            if old.name.endswith(".once.parquet") or any(old.name.endswith(n) for n in names):
                continue
            old.unlink(missing_ok=True)

    def clear(self) -> None:
        """Remove every kept file (used by tests)."""
        for old in self._directory.glob("*.parquet*"):
            old.unlink(missing_ok=True)
