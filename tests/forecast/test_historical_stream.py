"""The streamed historical ingest: byte-identity, and refusal when it is not safe (C-263 / #98).

Two properties, and the second is the one that keeps the first honest.

**Identity.** A value-dir assembled by streaming must equal the one the in-memory constructor
produces — same manifest, same index, same float64 feature blocks *byte for byte*, same geo
table including categorical dtype and **category order**. Category order is not cosmetic:
`groupby(observed=True)` emits groups in category order, so it fixes the served row order at
`/country/...`.

**Refusal.** Streaming is only equivalent because the artifact is already dense and already in
`sort_index()` order. Every test below that perturbs one of those preconditions asserts the
loader raises `NotStreamable` rather than writing a value-dir the in-memory path would not
have produced — a wrong row order here moves served numbers with no error anywhere.

The fixtures are small but structurally real: a dense (month x cell) grid with the 9-column
GAUL metadata contract, the three `lr_ged_*` targets, and float64 values.
"""
import json

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.ingestion.historical_stream import (
    ImplausibleArtifact,
    NotStreamable,
    stream_to_value,
)

pytestmark = pytest.mark.layer2_data

TARGETS = ["lr_ged_sb", "lr_ged_ns", "lr_ged_os"]
META = list(ForecastDataset._METADATA_COLS)
CATS = list(ForecastDataset._CATEGORICAL_METADATA_COLS)


def _dense_frame(n_months=4, n_cells=6, seed=0):
    """A dense, month-sorted, cell-sorted historical frame — the producer's real shape."""
    rng = np.random.default_rng(seed)
    months = np.repeat(np.arange(600, 600 + n_months), n_cells)
    cells = np.tile(np.arange(100, 100 + n_cells), n_months)
    rows = n_months * n_cells
    iso = np.array(["AAA", "BBB", "CCC", "-99"])[np.tile(np.arange(n_cells) % 4, n_months)]
    data = {
        "month_id": months,
        "priogrid_id": cells,
        "pg_xcoord": np.tile(np.linspace(-10, 10, n_cells), n_months),
        "pg_ycoord": np.tile(np.linspace(-5, 5, n_cells), n_months),
        "country_iso_a3": iso,
        "admin1_gaul1_code": np.tile(np.arange(n_cells, dtype=float), n_months),
        "admin1_gaul1_name": np.array([f"R{i}" for i in range(n_cells)] * n_months),
        "admin1_gaul0_code": np.tile(np.arange(n_cells, dtype=float) * 10, n_months),
        "admin1_gaul0_name": np.array([f"P{i}" for i in range(n_cells)] * n_months),
        "admin2_gaul2_code": np.tile(np.arange(n_cells, dtype=float) * 100, n_months),
        "admin2_gaul2_name": np.array([f"D{i}" for i in range(n_cells)] * n_months),
    }
    for t in TARGETS:
        data[t] = rng.gamma(2.0, 30.0, rows)
    return pd.DataFrame(data)


def _write(df, path):
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
    return path


def _reference_value_dir(df, out):
    """The value-dir the current in-memory path produces, for comparison."""
    frame = df.copy().set_index(["month_id", "priogrid_id"])
    for c in CATS:
        if frame[c].dtype == object:
            frame[c] = frame[c].astype("category")
    ForecastDataset(frame, targets=list(TARGETS)).to_value(out)
    return out


class TestByteIdentity:
    def test_streamed_value_dir_matches_the_in_memory_one(self, tmp_path):
        df = _dense_frame(n_months=5, n_cells=8)
        art = _write(df, tmp_path / "a.parquet")
        ref = _reference_value_dir(df, tmp_path / "ref")
        new = tmp_path / "new"
        rows = stream_to_value(art, new, TARGETS, META, CATS)
        assert rows == len(df)

        assert json.loads((ref / "manifest.json").read_text()) == json.loads(
            (new / "manifest.json").read_text()
        )
        a = ForecastDataset.from_value(ref)
        b = ForecastDataset.from_value(new)
        assert a.dataframe.index.equals(b.dataframe.index)
        for t in TARGETS:
            x, y = np.asarray(a._feature_store[t]), np.asarray(b._feature_store[t])
            assert x.dtype == np.float64 and y.dtype == np.float64, "C-258: float64, not float32"
            assert x.tobytes() == y.tobytes(), f"{t} differs byte-for-byte"

    def test_geo_table_including_category_order_matches(self, tmp_path):
        """Category order fixes served row order under `groupby(observed=True)`."""
        df = _dense_frame(n_months=3, n_cells=7)
        art = _write(df, tmp_path / "a.parquet")
        a = ForecastDataset.from_value(_reference_value_dir(df, tmp_path / "ref"))
        stream_to_value(art, tmp_path / "new", TARGETS, META, CATS)
        b = ForecastDataset.from_value(tmp_path / "new")

        assert list(a.geo_metadata.columns) == list(b.geo_metadata.columns)
        assert a.geo_metadata.index.equals(b.geo_metadata.index)
        for c in a.geo_metadata.columns:
            assert str(a.geo_metadata[c].dtype) == str(b.geo_metadata[c].dtype)
            if str(a.geo_metadata[c].dtype) == "category":
                assert list(a.geo_metadata[c].cat.categories) == list(
                    b.geo_metadata[c].cat.categories
                ), f"{c}: category ORDER differs — served group order would move"
            assert a.geo_metadata[c].astype(object).equals(b.geo_metadata[c].astype(object))


class TestRefusesWhatItCannotReproduce:
    def test_refuses_unsorted_cells_within_a_month(self, tmp_path):
        df = _dense_frame(n_months=3, n_cells=6)
        # reverse one month's cells: still dense, no longer sort_index() order
        block = df.index[df["month_id"] == 601]
        df.loc[block, "priogrid_id"] = df.loc[block, "priogrid_id"].to_numpy()[::-1]
        art = _write(df, tmp_path / "a.parquet")
        with pytest.raises(NotStreamable, match="ascending"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)

    def test_refuses_unsorted_months(self, tmp_path):
        df = _dense_frame(n_months=3, n_cells=6)
        df = pd.concat([df[df.month_id == 602], df[df.month_id != 602]], ignore_index=True)
        art = _write(df, tmp_path / "a.parquet")
        with pytest.raises(NotStreamable, match="ascending"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)

    def test_refuses_a_sparse_grid(self, tmp_path):
        """A hole is exactly what the in-memory path dense-fills; streaming must not skip it."""
        df = _dense_frame(n_months=3, n_cells=6).drop(index=4).reset_index(drop=True)
        art = _write(df, tmp_path / "a.parquet")
        with pytest.raises(NotStreamable, match="not dense"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)

    def test_refuses_when_an_entity_is_absent_from_the_last_step(self, tmp_path):
        """The C-87 guarantee: the in-memory path raises rather than drop the entity."""
        df = _dense_frame(n_months=3, n_cells=6)
        df = df[~((df.month_id == 602) & (df.priogrid_id == 105))].reset_index(drop=True)
        art = _write(df, tmp_path / "a.parquet")
        with pytest.raises(NotStreamable):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)

    def test_refuses_missing_geo_columns(self, tmp_path):
        df = _dense_frame().drop(columns=["admin2_gaul2_name"])
        art = _write(df, tmp_path / "a.parquet")
        with pytest.raises(NotStreamable, match="geo metadata columns absent"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)

    def test_refuses_missing_targets(self, tmp_path):
        df = _dense_frame().drop(columns=["lr_ged_os"])
        art = _write(df, tmp_path / "a.parquet")
        with pytest.raises(NotStreamable, match="targets absent"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)


class TestReviewFindings:
    """Regressions for the findings `/code-review medium` and `/review-diff` raised on PR review.

    Each of these was reproduced against the unfixed loader before the fix was written.
    """

    def test_refuses_descending_cells_across_a_row_group_boundary(self, tmp_path):
        """CR-2. The per-chunk guard compared cells only *within* a chunk and months only at the
        boundary, so a month split across two row groups with the second half descending passed
        every check — density, C-87 and all — and produced a value-dir in an order `sort_index()`
        would never emit. At 64,742 cells/month against ~1M-row row groups a month spanning two
        groups is the norm, not an edge case."""
        def rows(month, cells):
            n = len(cells)
            rng = np.random.default_rng(0)
            d = {"month_id": [month] * n, "priogrid_id": list(cells),
                 "pg_xcoord": np.zeros(n), "pg_ycoord": np.zeros(n),
                 "country_iso_a3": ["AAA"] * n, "admin1_gaul1_code": np.zeros(n),
                 "admin1_gaul1_name": ["R"] * n, "admin1_gaul0_code": np.zeros(n),
                 "admin1_gaul0_name": ["P"] * n, "admin2_gaul2_code": np.zeros(n),
                 "admin2_gaul2_name": ["D"] * n}
            for t in TARGETS:
                d[t] = rng.random(n)
            return pd.DataFrame(d)

        chunks = [rows(600, [104, 105, 106, 107]),   # month 600, upper half FIRST
                  rows(600, [100, 101, 102, 103]),   # month 600, lower half SECOND
                  rows(601, list(range(100, 108)))]
        art = tmp_path / "split.parquet"
        writer = pq.ParquetWriter(art, pa.Table.from_pandas(chunks[0], preserve_index=False).schema)
        for c in chunks:
            writer.write_table(pa.Table.from_pandas(c, preserve_index=False))
        writer.close()
        assert pq.ParquetFile(art).metadata.num_row_groups == 3

        # "ascend" matches both the within-chunk message ("ascending") and the boundary one
        # ("does not ascend from cell N"), so the test does not pin which guard fires.
        with pytest.raises(NotStreamable, match="ascend"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)

    def test_refuses_implausible_geography_before_writing_anything_servable(self, tmp_path):
        """CR-1. Plausibility (C-72) ran only after the value-dir had been adopted into the cache
        slot, replacing the previous good entry — and the disk read path does not re-validate, so
        the next request served the implausible geography. The check now runs while streaming,
        before any of it can be committed."""
        df = _dense_frame(n_months=3, n_cells=6)
        df.loc[4, "pg_ycoord"] = 999.0  # outside [-90, 90]
        art = _write(df, tmp_path / "bad.parquet")
        with pytest.raises(ImplausibleArtifact, match="pg_ycoord"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)

    def test_refuses_null_geo_metadata_the_in_memory_path_would_backfill(self, tmp_path):
        """CR-3. `ForecastDataset.__init__` overwrites every row of an entity that has ANY null geo
        column with that entity's first non-null values. Streaming writes each chunk verbatim, so
        an artifact with a partially-null cell would produce a different geo table by ingest path
        — and a different cell set at aggregation, with no error."""
        df = _dense_frame(n_months=3, n_cells=6)
        df.loc[7, "admin1_gaul1_name"] = None
        art = _write(df, tmp_path / "nulls.parquet")
        with pytest.raises(NotStreamable, match="null"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)

    def test_refuses_when_targets_omit_a_value_column_the_other_path_would_keep(self, tmp_path):
        """CR-4. The in-memory path moves EVERY remaining scalar column into `_feature_store` and
        lists it in the manifest; streaming reads only the named targets. With `historical_targets`
        configured to a subset — the only reason that config key exists — the two paths would
        produce different manifests for the same artifact."""
        df = _dense_frame(n_months=3, n_cells=6)
        art = _write(df, tmp_path / "a.parquet")
        with pytest.raises(NotStreamable, match="value column"):
            stream_to_value(art, tmp_path / "out", TARGETS[:1], META, CATS)

    def test_descending_months_are_caught_on_unsigned_index_columns(self, tmp_path):
        """CR-6. `np.diff` on an unsigned dtype wraps, so a descending sequence reads as a large
        positive step and the ordering guard passes on exactly the input it exists to reject.
        Some platform parquet schemas write the index columns unsigned."""
        df = _dense_frame(n_months=3, n_cells=6)
        df = pd.concat([df[df.month_id == 602], df[df.month_id != 602]], ignore_index=True)
        df["month_id"] = df["month_id"].astype("uint32")
        df["priogrid_id"] = df["priogrid_id"].astype("uint32")
        art = _write(df, tmp_path / "unsigned.parquet")
        with pytest.raises(NotStreamable, match="ascending"):
            stream_to_value(art, tmp_path / "out", TARGETS, META, CATS)
