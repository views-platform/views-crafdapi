"""Notebook 07's check cell, run as it is stored, against good and deliberately defective answers.

A notebook that only ever passes is worth nothing. These tests take the real check cell out of
`notebooks/07_is_the_data_current.ipynb`, give it an answer from `/health` and a country table, and
require each defect to fail the right check, by name. No server, no download.
"""
import ast
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from views_crafdapi import data_checks

NOTEBOOK = Path(__file__).parent.parent / "notebooks" / "07_is_the_data_current.ipynb"
MADE_IN = "2026-11"
TODAY = date(2026, 11, 25)


def _check_cell() -> str:
    cells = json.loads(NOTEBOOK.read_text())["cells"]
    found = [("".join(c["source"])) for c in cells if c["cell_type"] == "code" and "checks = [" in "".join(c["source"])]
    assert len(found) == 1, "notebook 07 must have exactly one check cell"
    return found[0]


def _health(**override):
    health = {"status": "healthy", "forecast_loaded": True, "served_run": "run_a",
              "forecast_freshness": {"sla_days": 45.0, "age_days": 13.0, "is_stale": False,
                                     "created_at": "2026-11-12T21:01:50+00:00"}}
    health.update(override)
    return health


def _country(months_recorded=("2026-09", "2026-10"), months_forecast=("2026-11", "2026-12", "2027-01")):
    rows = [{"month": m, "sb_actual": 5.0, "sb_mean": None} for m in months_recorded]
    rows += [{"month": m, "sb_actual": None, "sb_mean": 4.0} for m in months_forecast]
    return pd.DataFrame(rows)


def _run(health=None, country=None, made_in=MADE_IN, today=TODAY, expected_run=None, expected_months=None):
    namespace = {name: getattr(data_checks, name) for name in (
        "DataCheckError", "check_forecast_age", "check_forecast_month_fits_its_date", "check_horizon",
        "check_months", "check_newest_delivery_served")}
    namespace.update(health=health or _health(), country=_country() if country is None else country,
                     MADE_IN=made_in, TODAY=today, EXPECTED_RUN=expected_run, EXPECTED_MONTHS=expected_months)
    exec(compile(_check_cell(), str(NOTEBOOK), "exec"), namespace)


def test_good_data_passes_every_check(capsys):
    _run(expected_run="run_a", expected_months=3)
    out = capsys.readouterr().out
    assert out.count("PASS") == 6 and "FAIL" not in out and "The data is current." in out


def test_a_delivery_the_month_after_the_forecast_starts_passes(capsys):
    """The usual case: a forecast made late in a month and delivered early in the next."""
    _run(health=_health(forecast_freshness={"sla_days": 45.0, "age_days": 3.0, "is_stale": False,
                                            "created_at": "2026-12-02T10:00:00Z"}))
    assert "FAIL" not in capsys.readouterr().out


def test_an_unverified_copy_passes_once_the_store_answers_again(capsys):
    """As on /health: not an alarm once the store can be asked again (no false failure)."""
    _run(health=_health(appwrite_connected=True,
                        forecast_serving_state={"degraded": True, "serving": "cached_unverified"}))
    assert "FAIL" not in capsys.readouterr().out


@pytest.mark.parametrize("defect, kwargs, failing_check", [
    ("a newer delivery failed to load",
     {"health": _health(forecast_loading="the newest delivery could not be loaded; the previous one is served")},
     "1 · the newest delivery is served"),
    ("an older forecast is served in place of the newest",
     {"health": _health(forecast_serving_state={"degraded": True, "reason": "newest run refused"})},
     "1 · the newest delivery is served"),
    ("another run than the one expected",
     {"expected_run": "run_b"},
     "1 · the run is the one expected"),
    ("the run is not named, but one is expected",
     {"health": _health(served_run=None), "expected_run": "run_a"},
     "1 · the run is the one expected"),
    ("a 55-day-old forecast",
     {"made_in": "2026-10", "country": _country(("2026-08", "2026-09"), ("2026-10", "2026-11")),
      "health": _health(forecast_freshness={"sla_days": 45.0, "age_days": 55.0, "is_stale": True,
                                            "created_at": "2026-10-01T09:00:00Z"})},
     "2 · the forecast's age"),
    ("an unknown age",
     {"health": _health(forecast_freshness={"sla_days": 45.0, "age_days": None, "is_stale": None,
                                            "created_at": "2026-11-12T21:01:50+00:00"})},
     "2 · the forecast's age"),
    ("an old run delivered again, two months after its first month",
     {"made_in": "2026-09", "country": _country(("2026-07", "2026-08"), ("2026-09", "2026-10")),
      "health": _health(forecast_freshness={"sla_days": 45.0, "age_days": 1.0, "is_stale": False,
                                            "created_at": "2026-11-02T06:00:00Z"})},
     "3 · the forecast's first month fits its date"),
    ("the forecast's date is not reported, though its age is",
     {"health": _health(forecast_freshness={"sla_days": 45.0, "age_days": 13.0, "is_stale": False})},
     "3 · the forecast's first month fits its date"),
    ("the service cannot reach its store, though nothing else looks wrong",
     {"health": _health(appwrite_connected=False)},
     "1 · the newest delivery is served"),
    ("the service cannot reach its store and serves its own unverified copy",
     {"health": _health(appwrite_connected=False,
                        forecast_serving_state={"degraded": True, "serving": "cached_unverified"})},
     "1 · the newest delivery is served"),
    ("a gap between the records and the forecast",
     {"country": _country(("2026-08", "2026-09"), ("2026-11", "2026-12"))},
     "4 · the forecast starts after the recorded months"),
    ("a gap inside the forecast",
     {"country": _country(("2026-09", "2026-10"), ("2026-11", "2027-01"))},
     "4 · the forecast starts after the recorded months"),
    ("a horizon cut short",
     {"expected_months": 36},
     "5 · the number of forecast months"),
])
def test_each_defect_fails_its_own_check(capsys, defect, kwargs, failing_check):
    with pytest.raises(SystemExit, match="The data is not current"):
        _run(**kwargs)
    out = capsys.readouterr().out
    assert f"FAIL  {failing_check}:" in out, f"{defect}: expected {failing_check!r} to fail\n{out}"
    others = [line for line in out.splitlines() if line.startswith("FAIL") and failing_check not in line]
    assert not others, f"{defect}: other checks failed too: {others}"


def test_the_notebook_writes_down_no_limit_run_or_month():
    """The limit comes from /health; the run and the number of months only from what the user sets.
    So no number that is a limit, a horizon or a draw count, and no run or month, in any code cell
    (a figure's label rotation is drawing, not data)."""
    cells = json.loads(NOTEBOOK.read_text())["cells"]
    tree = ast.parse("\n".join("".join(c["source"]) for c in cells if c["cell_type"] == "code"))
    drawing = {id(kw.value) for kw in ast.walk(tree) if isinstance(kw, ast.keyword) and kw.arg == "rotation"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in drawing:
            continue
        assert node.value not in (36, 45, 128, 1024), f"notebook 07 writes down {node.value!r}"
        if isinstance(node.value, str):
            assert not re.search(r"\b20\d\d-\d\d\b|chrome_box|rusty_bucket", node.value), \
                f"notebook 07 writes down {node.value!r}"
