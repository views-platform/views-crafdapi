"""`DatasetService` uses the streamed historical ingest, and survives it not applying (C-263/#98).

The unit tests in `tests/forecast/test_historical_stream.py` prove the streamed value-dir equals
the in-memory one. These prove the service actually *reaches* it, and — the part that matters
operationally — that an artifact it cannot stream still serves, through the path that was
already there.

A memory optimisation that can fail a request is worse than the memory problem it solves, so
the fallback is tested for three separate causes: a precondition the loader refuses (sparse
grid), and an unexpected error raised from inside it.
"""
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.managers.api import CrafdApiManager
from views_crafdapi.managers.prediction.metadata import PredictionProvenance

pytestmark = pytest.mark.layer2_data

TARGETS = ["lr_ged_sb", "lr_ged_ns", "lr_ged_os"]


def _dense_bytes(tmp_path, n_months=4, n_cells=6, drop_row=None):
    rng = np.random.default_rng(7)
    months = np.repeat(np.arange(600, 600 + n_months), n_cells)
    cells = np.tile(np.arange(100, 100 + n_cells), n_months)
    rows = n_months * n_cells
    data = {
        "month_id": months, "priogrid_id": cells,
        "pg_xcoord": np.tile(np.linspace(-10, 10, n_cells), n_months),
        "pg_ycoord": np.tile(np.linspace(-5, 5, n_cells), n_months),
        "country_iso_a3": np.array(["AAA", "BBB"] * (rows // 2 + 1))[:rows],
        "admin1_gaul1_code": np.tile(np.arange(n_cells, dtype=float), n_months),
        "admin1_gaul1_name": np.array([f"R{i}" for i in range(n_cells)] * n_months),
        "admin1_gaul0_code": np.tile(np.arange(n_cells, dtype=float) * 10, n_months),
        "admin1_gaul0_name": np.array([f"P{i}" for i in range(n_cells)] * n_months),
        "admin2_gaul2_code": np.tile(np.arange(n_cells, dtype=float) * 100, n_months),
        "admin2_gaul2_name": np.array([f"D{i}" for i in range(n_cells)] * n_months),
    }
    for t in TARGETS:
        data[t] = rng.gamma(2.0, 30.0, rows)
    df = pd.DataFrame(data)
    if drop_row is not None:
        df = df.drop(index=drop_row).reset_index(drop=True)
    path = Path(tmp_path) / "art.parquet"
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
    return path.read_bytes()


def _service(tmp_path):
    mgr = CrafdApiManager.from_config({}, cache_dir=Path(tmp_path) / "datasets")
    return mgr._dataset_service


def _provenance():
    return PredictionProvenance(
        file_id="fid-1", source="test-source", created_at="2026-08-18T00:00:00+00:00",
        filename="hist.parquet", name="un_crafd", category="historical",
        targets=TARGETS, description=None, file_hash=None,
    )


def _cache_slot():
    return {"data": None, "file_id": None, "timestamp": None, "dataset": None, "source_kind": None}


class TestStreamingEngages:
    def test_dense_artifact_is_served_from_the_streamed_value_dir(self, tmp_path):
        svc = _service(tmp_path)
        cache = _cache_slot()
        out = svc._try_stream_historical(
            "keyhash", "historical", _dense_bytes(tmp_path), "fid-1",
            _provenance(), cache, 1_000_000.0,
        )
        assert out is not None, "the streamed path did not engage on a dense artifact"
        assert cache["dataset"] is not None
        ds = cache["dataset"]
        assert set(ds.targets) == set(TARGETS)
        assert not ds.is_prediction
        for t in TARGETS:
            assert np.asarray(ds._feature_store[t]).dtype == np.float64, "C-258: float64"
        # geography survived the round trip with the 9-column contract intact
        assert list(ds.geo_metadata.columns) == list(ForecastDataset._METADATA_COLS)

    def test_the_raw_bytes_are_dropped_from_the_file_cache(self, tmp_path):
        """The bytes are on disk by then; keeping them resident is what `del` failed to fix."""
        svc = _service(tmp_path)
        svc._file_cache["fid-1"] = {"data": b"x" * 1024, "timestamp": 0.0}
        svc._try_stream_historical(
            "keyhash", "historical", _dense_bytes(tmp_path), "fid-1",
            _provenance(), _cache_slot(), 1_000_000.0,
        )
        assert "fid-1" not in svc._file_cache


class TestFallsBackRatherThanFailing:
    def test_sparse_artifact_falls_back(self, tmp_path):
        """A hole is real work for the in-memory path — streaming must decline, not guess."""
        svc = _service(tmp_path)
        out = svc._try_stream_historical(
            "keyhash", "historical", _dense_bytes(tmp_path, drop_row=5), "fid-1",
            _provenance(), _cache_slot(), 1_000_000.0,
        )
        assert out is None

    def test_unexpected_error_falls_back(self, tmp_path, monkeypatch):
        svc = _service(tmp_path)
        monkeypatch.setattr(
            "views_crafdapi.managers.dataset_service.historical_stream.stream_to_value",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        out = svc._try_stream_historical(
            "keyhash", "historical", _dense_bytes(tmp_path), "fid-1",
            _provenance(), _cache_slot(), 1_000_000.0,
        )
        assert out is None

    def test_a_failed_attempt_leaves_no_staging_dir_behind(self, tmp_path, monkeypatch):
        """A leaked staging dir holds a full-N preallocated block — the §4.6 discard rule."""
        svc = _service(tmp_path)
        monkeypatch.setattr(
            "views_crafdapi.managers.dataset_service.historical_stream.stream_to_value",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        svc._try_stream_historical(
            "keyhash", "historical", _dense_bytes(tmp_path), "fid-1",
            _provenance(), _cache_slot(), 1_000_000.0,
        )
        leftovers = list((Path(tmp_path) / "datasets").glob("*.tmp"))
        assert leftovers == [], f"staging dir leaked: {leftovers}"


class TestPlausibilityFailsLoudRatherThanFallingBack:
    """CR-1, service half. A C-72 violation is a data fault, not a streaming fault.

    Before the fix it was caught by the broad fallback catch, so: the bad value-dir had already
    been adopted into the cache slot (deleting the last good one), the request 500'd from the
    in-memory path after paying the full peak, and the NEXT request took the disk-cache branch —
    which does not re-validate — and served the implausible geography to CRAF'd.
    """

    def _bad_bytes(self, tmp_path):
        df = pd.DataFrame({
            "month_id": np.repeat([600, 601], 4), "priogrid_id": np.tile([100, 101, 102, 103], 2),
            "pg_xcoord": np.zeros(8), "pg_ycoord": [0, 0, 0, 999.0, 0, 0, 0, 0],  # 999 > 90
            "country_iso_a3": ["AAA"] * 8, "admin1_gaul1_code": np.zeros(8),
            "admin1_gaul1_name": ["R"] * 8, "admin1_gaul0_code": np.zeros(8),
            "admin1_gaul0_name": ["P"] * 8, "admin2_gaul2_code": np.zeros(8),
            "admin2_gaul2_name": ["D"] * 8,
            **{t: np.random.default_rng(0).random(8) for t in TARGETS},
        })
        path = Path(tmp_path) / "bad.parquet"
        pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
        return path.read_bytes()

    def test_implausible_geography_raises_500_instead_of_falling_back(self, tmp_path):
        from fastapi import HTTPException
        svc = _service(tmp_path)
        with pytest.raises(HTTPException) as exc:
            svc._try_stream_historical(
                "keyhash", "historical", self._bad_bytes(tmp_path), "fid-bad",
                _provenance(), _cache_slot(), 1_000_000.0,
            )
        assert exc.value.status_code == 500

    def test_a_good_cached_entry_survives_a_later_bad_artifact(self, tmp_path):
        """The eviction half: the previous good value-dir must still be there afterwards."""
        from fastapi import HTTPException
        svc = _service(tmp_path)
        svc._try_stream_historical(
            "keyhash", "historical", _dense_bytes(tmp_path), "fid-good",
            _provenance(), _cache_slot(), 1_000_000.0,
        )
        good = svc._disk_cache.read("keyhash", "historical")
        assert good is not None and good["file_id"] == "fid-good"

        with pytest.raises(HTTPException):
            svc._try_stream_historical(
                "keyhash", "historical", self._bad_bytes(tmp_path), "fid-bad",
                _provenance(), _cache_slot(), 1_000_000.0,
            )

        after = svc._disk_cache.read("keyhash", "historical")
        assert after is not None, "the good entry was evicted by a refused artifact"
        assert after["file_id"] == "fid-good", f"cache slot poisoned: {after['file_id']}"
        assert after["dataset"].geo_metadata["pg_ycoord"].max() <= 90
