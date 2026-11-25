"""scripts/check_served_against_delivered.py: served summaries vs the delivered draws (#257 plan step 7)."""
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_served_against_delivered.py"
_spec = importlib.util.spec_from_file_location("check_served_against_delivered", _PATH)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def test_month_ids_map_to_calendar_months():
    assert check.month_of(1) == "1980-01"
    assert check.month_of(559) == "2026-07"
    assert check.month_of(564) == "2026-12"
    assert check.month_of(565) == "2027-01"


def _shard(tmp_path, values_by_cell, name="RUN__lr_ged_sb__m000559.arrow.parquet"):
    rows = [{"time": 559, "unit": cell, "sample": i, "value": v}
            for cell, values in values_by_cell.items() for i, v in enumerate(values)]
    path = tmp_path / name
    pd.DataFrame(rows).to_parquet(path)
    return str(path)


def _serve(monkeypatch, served_rows):
    check._GRID_FILES.clear()
    buf = io.BytesIO()
    pd.DataFrame(served_rows).to_parquet(buf)
    resp = SimpleNamespace(content=buf.getvalue(), raise_for_status=lambda: None)
    monkeypatch.setattr(check.requests, "get", lambda url, headers=None, timeout=None: resp)


def _served(cell, mean, p_gt0, n, month="2026-07"):
    return {"priogrid_id": cell, "month": month, "sb_mean": mean, "sb_p_gt0": p_gt0, "n_draws": n}


def test_matching_summaries_pass(tmp_path, monkeypatch):
    path = _shard(tmp_path, {10: [0.0, 2.0, 4.0, 0.0], 11: [0.0, 0.0, 0.0, 0.0]})
    _serve(monkeypatch, [_served(10, 1.5, 0.5, 4), _served(11, 0.0, 0.0, 4), _served(10, 9.0, 1.0, 4, "2026-08")])
    ok, detail = check.compare(path, "http://x", "key", 10)
    assert ok, detail


@pytest.mark.parametrize("served, words", [
    ([_served(10, 1.6, 0.5, 4), _served(11, 0.0, 0.0, 4)], "expected deaths"),
    ([_served(10, 1.5, 0.75, 4), _served(11, 0.0, 0.0, 4)], "chance of any deaths"),
    ([_served(10, 1.5, 0.5, 8), _served(11, 0.0, 0.0, 4)], "DIFFER"),
    ([_served(10, 1.5, 0.5, 4)], "not served"),
    ([_served(10, 1.5, 0.5, 4), _served(11, 0.0, 0.0, 4), _served(12, 0.0, 0.0, 4)], "served cells not delivered"),
    ([_served(10, 1.5, 0.5, 2), _served(11, 0.0, 0.0, 4)], "DIFFER"),
])
def test_a_difference_fails(tmp_path, monkeypatch, served, words):
    path = _shard(tmp_path, {10: [0.0, 2.0, 4.0, 0.0], 11: [0.0, 0.0, 0.0, 0.0]})
    _serve(monkeypatch, served)
    ok, detail = check.compare(path, "http://x", "key", 10)
    assert not ok and words in detail


def test_a_file_that_is_not_a_shard_fails(tmp_path):
    ok, detail = check.compare(str(tmp_path / "historical.parquet"), "http://x", "key", 10)
    assert not ok and "not a forecast shard" in detail


def test_a_shard_whose_month_differs_from_its_name_fails(tmp_path, monkeypatch):
    path = _shard(tmp_path, {10: [1.0]}, name="RUN__lr_ged_sb__m000560.arrow.parquet")   # holds 559
    _serve(monkeypatch, [_served(10, 1.0, 1.0, 1, "2026-08")])
    with pytest.raises(ValueError, match="holds months"):
        check.compare(path, "http://x", "key", 10)


def test_the_grid_file_is_downloaded_once_per_outcome(tmp_path, monkeypatch):
    first = _shard(tmp_path, {10: [0.0, 2.0]})
    second = _shard(tmp_path, {10: [0.0, 2.0]}, name="OTHER__lr_ged_sb__m000559.arrow.parquet")
    _serve(monkeypatch, [_served(10, 1.0, 0.5, 2)])
    calls = []
    real_get = check.requests.get
    monkeypatch.setattr(check.requests, "get", lambda *a, **k: calls.append(a) or real_get(*a, **k))
    assert check.compare(first, "http://x", "key", 10)[0] and check.compare(second, "http://x", "key", 10)[0]
    assert len(calls) == 1
