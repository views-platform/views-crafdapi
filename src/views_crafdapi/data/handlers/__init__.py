"""Data handlers package — the grid/forecast dataset classes.

Split from a single ``handlers.py`` (epic #325, story S11), one main class per
module, mirroring the ``appwrite/`` (S9) and ``prediction/`` (S10) packages:

* :mod:`.grid_dataset`     — ``_GridDataset``, the geo-less ``(N, S)`` compute-core base
* :mod:`.forecast_dataset` — ``ForecastDataset``, the faoapi forecast facade

The persisted VALUE-format constants moved out to :mod:`views_crafdapi.data.value_format`
(a stable leaf) so ``managers/disk_cache`` and the wire reader no longer import a
private symbol from this volatile module (the SDP-inversion fix, C-138).

The public surface is re-exported here so ``from views_crafdapi.data.handlers import X``
keeps working unchanged.
"""
from views_crafdapi.data.value_format import _VALUE_MANIFEST_SCALARS, _VALUE_SCHEMA_VERSION

from .forecast_dataset import ForecastDataset
from .grid_dataset import _GridDataset

__all__ = [
    "_GridDataset",
    "ForecastDataset",
    "_VALUE_SCHEMA_VERSION",
    "_VALUE_MANIFEST_SCALARS",
]
