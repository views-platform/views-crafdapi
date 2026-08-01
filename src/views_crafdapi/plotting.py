"""
Visualization utilities for VIEWS PRIO-GRID data.

Renders pixelated (lon/lat) grids with optional geographic borders,
shared color normalization for multi-panel plots, and subplot helpers.
"""

import math
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import colors
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.image import AxesImage


_NATURAL_EARTH_URL = (
    "https://naturalearth.s3.amazonaws.com/{res}_cultural/ne_{res}_admin_0_countries.zip"
)
_BORDER_RESOLUTIONS = ("110m", "50m", "10m")
# Default to the DETAILED (10m) outline. The coarse 110m tier sits ~25–58 km off the true
# coastline, which reads as the data pixels "drifting" from the borders at country zoom — the
# pixels themselves are correctly placed at 0.5° PRIO-GRID cell centres.
_DEFAULT_BORDER_RESOLUTION = "10m"

# Loaded border GeoDataFrames are cached per source so a repeated plot does not re-download the
# (up to ~24 MB at 10m) Natural Earth file on every call.
_BORDERS_CACHE: dict = {}

# Sentinel: the `borders` default means "resolve from `border_resolution`" (vs None = skip).
_USE_RESOLUTION = object()


def _borders_url(resolution: str) -> str:
    """Natural Earth admin-0 countries URL for a resolution tier ('110m'/'50m'/'10m')."""
    if resolution not in _BORDER_RESOLUTIONS:
        raise ValueError(
            f"border_resolution must be one of {_BORDER_RESOLUTIONS}; got {resolution!r}"
        )
    return _NATURAL_EARTH_URL.format(res=resolution)


def _load_borders(source) -> gpd.GeoDataFrame:
    """Resolve + load country borders, cached per source.

    ``source`` may be a Natural Earth resolution ('110m'/'50m'/'10m'), a path/URL to any vector
    file, or an already-loaded ``GeoDataFrame`` (returned as-is). The cache means each resolution
    is downloaded at most once per process.
    """
    if isinstance(source, gpd.GeoDataFrame):
        return source
    key = _borders_url(source) if source in _BORDER_RESOLUTIONS else source
    if key not in _BORDERS_CACHE:
        _BORDERS_CACHE[key] = gpd.read_file(key)
    return _BORDERS_CACHE[key]


def reduce_list_values(x, agg: str = "mean"):
    """Reduce a list/array/Series cell to a scalar."""
    if isinstance(x, (list, np.ndarray, pd.Series)):
        if len(x) == 0:
            return np.nan
        arr = np.asarray(x)
        if agg == "first":
            return arr.ravel()[0]
        if agg == "median":
            return float(np.nanmedian(arr.astype(float)))
        return float(np.nanmean(arr.astype(float)))
    return x


def prepare_values(
    series: pd.Series, *, agg: str = "mean", transform: str = "log1p"
) -> np.ndarray:
    """Reduce list-valued cells to scalars and apply a transform."""
    arr = np.array([reduce_list_values(v, agg=agg) for v in series], dtype=float)
    if transform == "log1p":
        arr = np.log1p(arr)
    return arr


def _derive_value_col(df: pd.DataFrame, *, exclude: tuple = ()) -> str:
    """Derive the single value column to plot from a DataFrame's columns.

    The target column name is **not** hardcoded (ADR-013 / views-models #154 name
    agnosticism: config/metadata is the source of truth for a target's name).
    Callers should pass ``value_col`` explicitly — e.g. ``PredictionMetadata.targets[0]``.
    When omitted, derive it iff exactly one candidate column remains after excluding
    the coordinate columns; otherwise fail loud rather than guessing.
    """
    candidates = [c for c in df.columns if c not in set(exclude)]
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(
        f"value_col could not be derived unambiguously from columns {list(df.columns)} "
        f"(candidates after excluding {list(exclude)}: {candidates}). "
        "Pass value_col explicitly, e.g. from PredictionMetadata.targets."
    )


def shared_norm_from_dfs(
    dfs: list[pd.DataFrame],
    value_col: Optional[str] = None,
    *,
    agg: str = "mean",
    transform: str = "log1p",
) -> colors.Normalize:
    """Compute a shared color normalization across multiple DataFrames.

    ``value_col`` is derived from the first DataFrame when not given (ADR-013).
    """
    if value_col is None:
        value_col = _derive_value_col(dfs[0])
    all_vals = np.concatenate(
        [prepare_values(df[value_col], agg=agg, transform=transform) for df in dfs]
    )
    vmin, vmax = np.nanmin(all_vals), np.nanmax(all_vals)
    if vmin == vmax:
        vmax = vmin + 1e-9
    return colors.Normalize(vmin=vmin, vmax=vmax)


def make_subplot_grid(
    n: int, cols: int = 3, figsize_per_plot: tuple = (6, 6)
) -> tuple[Figure, np.ndarray]:
    """Create a grid of subplots, removing unused axes."""
    rows = math.ceil(n / cols)
    fig, axes = plt.subplots(
        rows, cols,
        figsize=(cols * figsize_per_plot[0], rows * figsize_per_plot[1]),
        squeeze=False,
        constrained_layout=True,
    )
    flat = axes.flatten()
    for ax in flat[n:]:
        ax.remove()
    return fig, flat[:n]


def add_shared_colorbar(
    fig: Figure,
    axes: list[Axes],
    norm: colors.Normalize,
    cmap: str,
    *,
    label: str = "",
    orientation: str = "horizontal",
    fraction: float = 0.05,
    pad: float = 0.04,
):
    """Add one colorbar spanning multiple axes."""
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cb = fig.colorbar(sm, ax=axes, orientation=orientation, fraction=fraction, pad=pad)
    if label:
        cb.set_label(label)
    return cb


def plot_pixel_grid(
    df: pd.DataFrame,
    *,
    lon_col: str = "pg_xcoord",
    lat_col: str = "pg_ycoord",
    value_col: Optional[str] = None,
    ax: Axes | None = None,
    agg: str = "mean",
    transform: str = "log1p",
    cmap: str = "viridis",
    norm: colors.Normalize | None = None,
    borders: "str | gpd.GeoDataFrame | None" = _USE_RESOLUTION,
    border_resolution: str = _DEFAULT_BORDER_RESOLUTION,
    borders_linewidth: float = 0.6,
    margin_frac: float = 0.10,
    min_margin_steps: float = 1.0,
    figsize: tuple = (6, 10),
    colorbar: bool = True,
    colorbar_kwargs: dict | None = None,
) -> tuple[Figure, Axes, AxesImage]:
    """Plot a pixelated grid from lon/lat centroids, colored by value.

    Parameters
    ----------
    df : DataFrame with longitude, latitude, and value columns.
    lon_col, lat_col : Column names for coordinates.
    value_col : Column name for the value to plot. Derived from the non-coordinate
        column when omitted (ADR-013); pass it (e.g. from PredictionMetadata.targets)
        when the frame carries more than one value column.
    agg : How to reduce list-valued cells ('mean', 'median', 'first').
    transform : Value transform ('log1p' or 'none').
    cmap : Matplotlib colormap name.
    norm : Shared color normalization for multi-panel plots.
    borders : Country-border overlay. Default: draw from ``border_resolution``. Pass a path/URL
        or a GeoDataFrame to override the source; pass ``None`` to skip the overlay entirely.
    border_resolution : Natural Earth tier for the default overlay — ``"10m"`` (default, most
        accurate; the coarse ``"110m"`` sits ~25–58 km off true coastlines and drifts from the
        pixels at country zoom), ``"50m"`` (lighter), or ``"110m"`` (fastest, global-scale only).
    figsize : Figure size (used only when ax is None).
    colorbar : Whether to add a colorbar.

    Returns
    -------
    (fig, ax, im) : Figure, Axes, and AxesImage handles.
    """

    if value_col is None:
        value_col = _derive_value_col(df, exclude=(lon_col, lat_col))

    def _transform(v: np.ndarray):
        return np.log1p(v) if transform == "log1p" else v

    # --- Data preparation ---
    work = df[[lon_col, lat_col, value_col]].copy()
    for c in (lon_col, lat_col, value_col):
        work[c] = work[c].apply(reduce_list_values, agg=agg)
    work[[lon_col, lat_col, value_col]] = work[[lon_col, lat_col, value_col]].apply(
        pd.to_numeric, errors="coerce"
    )
    work = work.dropna(subset=[lon_col, lat_col, value_col])

    # --- Build raster on a COMPLETE regular grid ---
    # `pg_xcoord`/`pg_ycoord` are 0.5° cell CENTRES. Index each cell by its ABSOLUTE position on
    # the regular grid (round((coord - min) / step)) rather than by rank among the present
    # coordinates: missing cells (oceans / absent grid cells) then become transparent NaN holes
    # and every present cell stays at its true coordinate. Rank-indexing collapsed those gaps,
    # shrinking the raster below its true span so `imshow`/`extent` stretched the pixels and slid
    # them off the country borders (register plotting drift).
    xs = np.sort(work[lon_col].unique().astype(float))
    ys = np.sort(work[lat_col].unique().astype(float))
    dx = np.median(np.diff(xs)) if len(xs) > 1 else 0.1
    dy = np.median(np.diff(ys)) if len(ys) > 1 else 0.1
    xmin, xmax, ymin, ymax = xs.min(), xs.max(), ys.min(), ys.max()
    ncols = int(round((xmax - xmin) / dx)) + 1
    nrows = int(round((ymax - ymin) / dy)) + 1

    raster = np.full((nrows, ncols), np.nan, dtype=float)
    vals = _transform(work[value_col].to_numpy(dtype=float))
    cols = np.rint((work[lon_col].to_numpy(dtype=float) - xmin) / dx).astype(int)
    rows = np.rint((work[lat_col].to_numpy(dtype=float) - ymin) / dy).astype(int)
    raster[rows, cols] = vals

    extent = [xmin - dx / 2, xmax + dx / 2, ymin - dy / 2, ymax + dy / 2]

    pad_x = max(margin_frac * (xmax - xmin), min_margin_steps * dx)
    pad_y = max(margin_frac * (ymax - ymin), min_margin_steps * dy)

    # --- Plot ---
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
        owns_fig = True
    else:
        fig = ax.figure
        owns_fig = False

    if norm is None:
        vmin, vmax = np.nanmin(raster), np.nanmax(raster)
        if vmin == vmax:
            vmax = vmin + 1e-9
        norm = colors.Normalize(vmin=vmin, vmax=vmax)

    im = ax.imshow(
        raster,
        extent=extent,
        origin="lower",
        cmap=cmap,
        norm=norm,
        aspect="equal",
        interpolation="nearest",
    )

    # --- Borders ---
    # `borders=None` skips the overlay; the default sentinel resolves to `border_resolution`
    # (detailed 10m by default); an explicit path/URL/GeoDataFrame overrides both.
    border_source = border_resolution if borders is _USE_RESOLUTION else borders
    if border_source is not None:
        gdf = _load_borders(border_source)
        gdf.boundary.plot(ax=ax, color="black", linewidth=borders_linewidth)

    # --- View limits ---
    ax.set_xlim(extent[0] - pad_x, extent[1] + pad_x)
    ax.set_ylim(extent[2] - pad_y, extent[3] + pad_y)

    # --- Colorbar ---
    if colorbar:
        cb_kwargs = {
            "label": f"{value_col} ({'log1p' if transform == 'log1p' else 'linear'})",
            "fraction": 0.06,
            "pad": 0.02,
        }
        if colorbar_kwargs:
            cb_kwargs.update(colorbar_kwargs)
        fig.colorbar(im, ax=ax, **cb_kwargs)

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title("Pixel grid with country borders")

    if owns_fig:
        fig.tight_layout()

    return fig, ax, im
