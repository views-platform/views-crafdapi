import numpy as np
import pandas as pd
import pytest
from matplotlib import colors

from views_faoapi.plotting import (
    reduce_list_values,
    prepare_values,
    shared_norm_from_dfs,
    make_subplot_grid,
    plot_pixel_grid,
)

import matplotlib
matplotlib.use("Agg")


class TestReduceListValues:
    def test_scalar_passthrough(self):
        assert reduce_list_values(42.0) == 42.0

    def test_list_mean(self):
        result = reduce_list_values([2.0, 4.0, 6.0], agg="mean")
        assert result == pytest.approx(4.0)

    def test_list_median(self):
        result = reduce_list_values([1.0, 2.0, 100.0], agg="median")
        assert result == pytest.approx(2.0)

    def test_list_first(self):
        result = reduce_list_values([10.0, 20.0, 30.0], agg="first")
        assert result == pytest.approx(10.0)

    def test_empty_list(self):
        result = reduce_list_values([])
        assert np.isnan(result)

    def test_numpy_array(self):
        result = reduce_list_values(np.array([3.0, 5.0]), agg="mean")
        assert result == pytest.approx(4.0)

    def test_pandas_series(self):
        result = reduce_list_values(pd.Series([1.0, 3.0]), agg="mean")
        assert result == pytest.approx(2.0)

    def test_nan_handling(self):
        result = reduce_list_values([1.0, np.nan, 3.0], agg="mean")
        assert result == pytest.approx(2.0)


class TestPrepareValues:
    def test_log1p_transform(self):
        series = pd.Series([0.0, 1.0, np.e - 1])
        result = prepare_values(series, transform="log1p")
        expected = np.log1p([0.0, 1.0, np.e - 1])
        np.testing.assert_allclose(result, expected)

    def test_none_transform(self):
        series = pd.Series([5.0, 10.0])
        result = prepare_values(series, transform="none")
        np.testing.assert_allclose(result, [5.0, 10.0])

    def test_list_valued_cells(self):
        series = pd.Series([[1.0, 3.0], [2.0, 4.0]])
        result = prepare_values(series, agg="mean", transform="none")
        np.testing.assert_allclose(result, [2.0, 3.0])


class TestSharedNormFromDfs:
    def test_basic(self):
        df1 = pd.DataFrame({"lr_ged_sb": [0.0, 1.0]})
        df2 = pd.DataFrame({"lr_ged_sb": [2.0, 3.0]})
        norm = shared_norm_from_dfs([df1, df2], transform="none")
        assert isinstance(norm, colors.Normalize)
        assert norm.vmin == pytest.approx(0.0)
        assert norm.vmax == pytest.approx(3.0)

    def test_equal_values(self):
        df = pd.DataFrame({"lr_ged_sb": [5.0, 5.0]})
        norm = shared_norm_from_dfs([df], transform="none")
        assert norm.vmax > norm.vmin

    def test_log1p(self):
        df = pd.DataFrame({"lr_ged_sb": [0.0, 100.0]})
        norm = shared_norm_from_dfs([df], transform="log1p")
        assert norm.vmin == pytest.approx(np.log1p(0.0))
        assert norm.vmax == pytest.approx(np.log1p(100.0))


class TestMakeSubplotGrid:
    def test_returns_correct_count(self):
        fig, axes = make_subplot_grid(3, cols=3)
        assert len(axes) == 3
        import matplotlib.pyplot as plt
        plt.close(fig)

    def test_fewer_than_cols(self):
        fig, axes = make_subplot_grid(2, cols=4)
        assert len(axes) == 2
        import matplotlib.pyplot as plt
        plt.close(fig)

    def test_multi_row(self):
        fig, axes = make_subplot_grid(5, cols=3)
        assert len(axes) == 5
        import matplotlib.pyplot as plt
        plt.close(fig)

    def test_single_plot(self):
        fig, axes = make_subplot_grid(1, cols=3)
        assert len(axes) == 1
        import matplotlib.pyplot as plt
        plt.close(fig)


class TestPlotPixelGrid:
    @pytest.fixture
    def sample_df(self):
        return pd.DataFrame({
            "pg_xcoord": [10.0, 10.5, 11.0, 10.0, 10.5, 11.0],
            "pg_ycoord": [5.0, 5.0, 5.0, 5.5, 5.5, 5.5],
            "lr_ged_sb": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
        })

    def test_cells_are_absolutely_georeferenced_across_gaps(self):
        """Regression: a missing grid column/row must become a transparent NaN hole, NOT collapse
        the raster. Rank-indexing shrank the raster below its true span so `imshow` stretched the
        pixels and slid them off the country borders. Each present cell must land at its true
        centre and be exactly one grid step wide."""
        import matplotlib.pyplot as plt

        step = 0.5
        rows = []
        # 10.25 .. 12.25 in 0.5° steps, but 11.25 is MISSING (an ocean/absent column).
        for x in [10.25, 10.75, 11.75, 12.25]:
            for y in [5.25, 5.75, 6.25]:
                rows.append((x, y, 1.0))
        df = pd.DataFrame(rows, columns=["pg_xcoord", "pg_ycoord", "lr_ged_sb"])
        df.loc[(df.pg_xcoord == 12.25) & (df.pg_ycoord == 5.75), "lr_ged_sb"] = 100.0

        fig, ax, im = plot_pixel_grid(df, borders=None, transform="none", colorbar=False)
        arr = np.asarray(im.get_array().filled(np.nan))
        ext = im.get_extent()
        ncols = arr.shape[1]
        cell_w = (ext[1] - ext[0]) / ncols
        cell_h = (ext[3] - ext[2]) / arr.shape[0]

        # raster spans the FULL grid (5 columns 10.25..12.25), not just the 4 present
        assert ncols == 5
        assert cell_w == pytest.approx(step) and cell_h == pytest.approx(step)
        # the missing 11.25 column is a transparent NaN hole
        gap_col = int(round((11.25 - 10.25) / step))
        assert np.all(np.isnan(arr[:, gap_col]))
        # the tracked cell is drawn at its TRUE centre — zero drift
        r, c = [tuple(p) for p in np.argwhere(np.isclose(arr, 100.0))][0]
        drawn_x = ext[0] + (c + 0.5) * cell_w
        drawn_y = ext[2] + (r + 0.5) * cell_h
        assert drawn_x == pytest.approx(12.25) and drawn_y == pytest.approx(5.75)
        plt.close(fig)

    def test_returns_figure_axes_image(self, sample_df):
        from matplotlib.figure import Figure
        from matplotlib.axes import Axes
        from matplotlib.image import AxesImage
        import matplotlib.pyplot as plt

        fig, ax, im = plot_pixel_grid(sample_df, borders=None)
        assert isinstance(fig, Figure)
        assert isinstance(ax, Axes)
        assert isinstance(im, AxesImage)
        plt.close(fig)

    def test_no_colorbar(self, sample_df):
        import matplotlib.pyplot as plt

        fig, ax, im = plot_pixel_grid(sample_df, borders=None, colorbar=False)
        assert len(fig.axes) == 1
        plt.close(fig)

    def test_with_shared_norm(self, sample_df):
        import matplotlib.pyplot as plt

        norm = colors.Normalize(vmin=0, vmax=10)
        fig, ax, im = plot_pixel_grid(sample_df, borders=None, norm=norm)
        assert im.norm.vmax == 10
        plt.close(fig)

    def test_no_transform(self, sample_df):
        import matplotlib.pyplot as plt

        fig, ax, im = plot_pixel_grid(sample_df, borders=None, transform="none")
        assert fig is not None
        plt.close(fig)

    def test_provided_ax(self, sample_df):
        import matplotlib.pyplot as plt

        ext_fig, ext_ax = plt.subplots()
        fig, ax, im = plot_pixel_grid(sample_df, ax=ext_ax, borders=None)
        assert ax is ext_ax
        assert fig is ext_fig
        plt.close(ext_fig)


class TestValueColDerivation:
    """ADR-013: value_col is derived when omitted, and fails loud when ambiguous
    (no hardcoded target name). Issue #99 / epic #144 S4."""

    def test_shared_norm_derives_single_column(self):
        # one value column -> derived without passing value_col
        dfs = [pd.DataFrame({"any_target_name": [0.0, 1.0, 2.0]})]
        norm = shared_norm_from_dfs(dfs, transform="none")
        assert isinstance(norm, colors.Normalize)

    def test_shared_norm_ambiguous_fails_loud(self):
        dfs = [pd.DataFrame({"a": [1.0], "b": [2.0]})]
        with pytest.raises(ValueError, match="value_col could not be derived"):
            shared_norm_from_dfs(dfs)

    def test_plot_pixel_grid_derives_non_coordinate_column(self):
        df = pd.DataFrame({
            "pg_xcoord": [10.0, 11.0],
            "pg_ycoord": [20.0, 21.0],
            "sb_map": [1.0, 2.0],   # arbitrary target name, derived
        })
        fig, ax, im = plot_pixel_grid(df, borders=None, colorbar=False)
        assert im is not None

    def test_plot_pixel_grid_ambiguous_fails_loud(self):
        df = pd.DataFrame({
            "pg_xcoord": [10.0], "pg_ycoord": [20.0],
            "sb_map": [1.0], "ns_map": [2.0],  # two candidates -> must not guess
        })
        with pytest.raises(ValueError, match="value_col could not be derived"):
            plot_pixel_grid(df, borders=None, colorbar=False)


class TestBorderResolution:
    """The country-border overlay must use a DETAILED outline. The old default (Natural Earth
    110m, the coarsest tier) sits ~25–58 km off the true coastline, which reads as the data
    pixels 'drifting' from the borders at country zoom — the data pixels themselves are correctly
    placed at 0.5° PRIO-GRID cell centres (proven upstream in
    views-postprocessing/.../unfao/gaul_schema.py: xcoord = -180 + col*0.5 + 0.25)."""

    def test_default_resolution_is_detailed_not_coarse(self):
        from views_faoapi import plotting
        # guard against regressing the default back to the coarse 110m outline
        assert plotting._DEFAULT_BORDER_RESOLUTION == "10m"

    def test_borders_url_builds_and_validates(self):
        from views_faoapi import plotting
        assert plotting._borders_url("10m").endswith("10m_cultural/ne_10m_admin_0_countries.zip")
        assert "50m" in plotting._borders_url("50m")
        with pytest.raises(ValueError):
            plotting._borders_url("1m")

    def test_border_resolution_param_routes_to_loader(self, monkeypatch):
        """Offline: `border_resolution=` is what gets resolved/loaded (no network)."""
        import geopandas as gpd
        import matplotlib.pyplot as plt
        from shapely.geometry import box
        from views_faoapi import plotting

        seen = {}

        def fake_loader(source):
            seen["source"] = source
            return gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)])

        monkeypatch.setattr(plotting, "_load_borders", fake_loader)
        df = pd.DataFrame({"pg_xcoord": [10.25, 10.75], "pg_ycoord": [5.25, 5.75],
                           "lr_ged_sb": [1.0, 2.0]})
        fig, _, _ = plotting.plot_pixel_grid(df, border_resolution="50m", colorbar=False)
        assert seen["source"] == "50m"
        plt.close(fig)

    def test_borders_none_still_skips(self, monkeypatch):
        """`borders=None` must keep meaning SKIP (unchanged contract) — never hit the loader."""
        import matplotlib.pyplot as plt
        from views_faoapi import plotting

        called = {"n": 0}
        monkeypatch.setattr(plotting, "_load_borders",
                            lambda s: called.__setitem__("n", called["n"] + 1))
        df = pd.DataFrame({"pg_xcoord": [10.25], "pg_ycoord": [5.25], "lr_ged_sb": [1.0]})
        fig, _, _ = plotting.plot_pixel_grid(df, borders=None, colorbar=False)
        assert called["n"] == 0
        plt.close(fig)

    def test_borders_are_cached(self, monkeypatch):
        """A repeated plot must not re-download the (up to ~24 MB) border file."""
        import geopandas as gpd
        from shapely.geometry import box
        from views_faoapi import plotting

        plotting._BORDERS_CACHE.clear()
        reads = {"n": 0}

        def counting_read_file(path, *a, **k):
            reads["n"] += 1
            return gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)])

        monkeypatch.setattr(plotting.gpd, "read_file", counting_read_file)
        plotting._load_borders("50m")
        plotting._load_borders("50m")
        assert reads["n"] == 1  # second call served from cache
        plotting._BORDERS_CACHE.clear()

    def test_real_geography_registration(self):
        """Network-gated (skips offline). REAL geography: correctly-placed 0.5° cells sit inside
        the true (10m) border, and the coarse 110m outline drifts materially from it — this is the
        bug the default must avoid. Reproduces the measured cause."""
        import geopandas as gpd
        import numpy as np
        from shapely.geometry import Point
        from views_faoapi import plotting

        def somalia(res):
            url = plotting._borders_url(res)
            g = gpd.read_file(url)
            return g[g["NAME"].str.contains("Somalia", case=False, na=False)].geometry.union_all()

        try:
            p10, p110 = somalia("10m"), somalia("110m")
        except Exception as e:  # offline / network blocked
            pytest.skip(f"Natural Earth not reachable: {e}")

        # data squares at TRUE PRIO-GRID centroids (.25/.75) are (almost) all inside the true border
        w, s, e, n = p10.bounds
        xs = np.arange(np.floor(w) + 0.25, np.ceil(e), 0.5)
        ys = np.arange(np.floor(s) + 0.25, np.ceil(n), 0.5)
        centres = [(x, y) for x in xs for y in ys if p10.contains(Point(x, y))]
        assert len(centres) > 50  # a real country's worth of cells
        # the coarse 110m outline drifts materially from truth (the visible 'drift'); the default
        # (10m) IS the truth.
        assert p110.boundary.hausdorff_distance(p10.boundary) > 0.3  # ~33+ km
        assert plotting._DEFAULT_BORDER_RESOLUTION == "10m"
