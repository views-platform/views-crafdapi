"""The bulk file (#203): written a few months at a time, read back as the one table."""

import pandas as pd
import pytest

from tests.conftest import make_fao_df
from tests.forecast.test_outcome_table import _relabel_months
from views_crafdapi.data.handlers import ForecastDataset
from views_crafdapi.forecast.serialize import bulk_file, outcome_table

pytestmark = pytest.mark.layer2_data

_TARGETS = ("pred_lr_ged_sb", "pred_lr_ged_ns", "pred_lr_ged_os")


def _datasets():
    """Five recorded months (595-599) before a two-month forecast (600-601)."""
    forecast = ForecastDataset(make_fao_df(seed=1, targets=_TARGETS))
    history = ForecastDataset(_relabel_months(
        make_fao_df(n_months=6, seed=2, targets=_TARGETS), [595, 596, 597, 598, 599, 600]
    ))
    return forecast, history


@pytest.mark.parametrize("level", ["grid", "admin1", "country"])
def test_the_file_written_in_parts_is_the_whole_table(tmp_path, monkeypatch, level):
    """Two months per part, so the seven months take four parts and one part mixes recorded and
    forecast months. Read back, the file is the table built in one go, types included."""
    monkeypatch.setattr(bulk_file, "MONTHS_PER_PART", 2)
    forecast, history = _datasets()
    path = bulk_file.write_bulk_file(tmp_path / "f.parquet", forecast, history, level, "sb")
    back = pd.read_parquet(path)
    whole = outcome_table.build_outcome_table(forecast, history, level, "sb")
    assert sorted(back["month_id"].unique()) == list(range(595, 602))
    pd.testing.assert_frame_equal(back, whole.reset_index(drop=True), check_dtype=True)


def test_a_forecast_without_months_is_refused_not_written_empty(tmp_path, monkeypatch):
    """No months would write no table at all; served, that is an unreadable file."""
    monkeypatch.setattr(bulk_file, "bulk_months", lambda *a: [])
    forecast, history = _datasets()
    with pytest.raises(ValueError, match="no months"):
        bulk_file.write_bulk_file(tmp_path / "f.parquet", forecast, history, "admin1", "sb")
