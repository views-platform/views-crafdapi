"""Notebook 06's measuring cells, run as they are stored, on a small table whose answers are known.

Each relation notebook 06 states is measured on the data served. These tests give its cells nine
grid cells built so that every count is known in advance, including one where the expected number
is not the chance times the size, and require the cells to print those counts rather than assume
the relation holds. No server, no download.
"""
import ast
import json
import math
import re
from pathlib import Path

import pandas as pd

from views_crafdapi.forecast.serialize import schema

NOTEBOOK = Path(__file__).parent.parent / "notebooks" / "06_which_number_to_report.ipynb"
NAN = float("nan")


def _cells_source():
    return ["".join(c["source"]) for c in json.loads(NOTEBOOK.read_text())["cells"] if c["cell_type"] == "code"]


def _cell(marker):
    found = [c for c in _cells_source() if marker in c]
    assert len(found) == 1, f"notebook 06 must have exactly one cell containing {marker!r}"
    return found[0]


def _table():
    # Rows (p_gt0, mean_if_any, mean, median_if_any, severe, n_draws):
    #  0: likely & large      0.8, 10  -> mean 8.0,  median 6 (average above)
    #  1: likely but small    0.6, 2   -> mean 1.2,  median 2 (equal)
    #  2: unlikely but large  0.1, 20  -> mean 2.0,  median 25 (average below)
    #  3: unlikely & small    0.05, 1  -> mean 0.05, median 1 (equal)
    #  4: no death in any draw         -> mean 0, if-any empty
    #  5: the identity broken: 0.5 x 4 = 2, but mean 3.0; median 4 (equal)
    #  6: no death, 16 draws (fewer than 20): the severe case empty
    #  7: no usable draws: everything empty, n_draws 0
    #  8: no death, exactly 20 draws: the severe case is filled, yet it is a single draw
    # Medians of the five at-risk rows (0, 1, 2, 3, 5): chance 0.5, deaths if any 4.
    return pd.DataFrame({
        "sb_p_gt0":           [0.8, 0.6, 0.1, 0.05, 0.0, 0.5, 0.0, NAN, 0.0],
        "sb_p_gt25":          [0.3, 0.0, 0.05, 0.0, 0.0, 0.0, 0.0, NAN, 0.0],
        "sb_mean_if_any":     [10.0, 2.0, 20.0, 1.0, NAN, 4.0, NAN, NAN, NAN],
        "sb_median_if_any":   [6.0, 2.0, 25.0, 1.0, NAN, 4.0, NAN, NAN, NAN],
        "sb_mean":            [8.0, 1.2, 2.0, 0.05, 0.0, 3.0, 0.0, NAN, 0.0],
        "sb_severe_scenario": [30.0, 3.0, 50.0, 1.0, 0.0, 6.0, NAN, NAN, 0.0],
        "n_draws":            [128, 128, 128, 128, 128, 128, 16, 0, 20],
    })


def _run(marker, **extra):
    namespace = {"pd": pd, "math": math, "cells": _table(), "CHANCES": ["sb_p_gt0", "sb_p_gt25"],
                 "SEVERE_TAIL": schema.SEVERE_TAIL, "SEVERE_MIN_DRAWS": schema.SEVERE_MIN_DRAWS, **extra}
    exec(compile(_cell(marker), str(NOTEBOOK), "exec"), namespace)
    return namespace


def test_the_identity_is_measured_and_a_break_is_counted(capsys):
    _run("product = cells")
    out = capsys.readouterr().out
    assert "Cells with draws: 8; with any chance of deaths: 5" in out
    assert "Cells where sb_mean is not sb_p_gt0 x sb_mean_if_any: 1" in out
    assert "Cells with no death in any draw but an expected number above 0: 0" in out


def test_an_expected_number_without_any_death_is_counted(capsys):
    table = _table()
    table.loc[4, "sb_mean"] = 0.5
    _run("product = cells", cells=table)
    out = capsys.readouterr().out
    assert "Cells with no death in any draw but an expected number above 0: 1" in out
    # 0 x (no deaths if any, counted as 0) is 0, not 0.5: the identity breaks here too.
    assert "Cells where sb_mean is not sb_p_gt0 x sb_mean_if_any: 2" in out


def test_the_four_groups_at_the_medians(capsys):
    _run("at_risk = cells")
    out = capsys.readouterr().out
    assert "5 cells with any chance of deaths; medians: chance 0.500, deaths if any 4.0" in out
    # likely (>= 0.5): rows 0, 1, 5; large (>= 4): rows 0, 2, 5.
    counts = dict(re.findall(r"^(likely and large|likely but small|unlikely but large|unlikely and small)\s+(\d+)$",
                             out, re.M))
    assert counts == {"likely and large": "2", "likely but small": "1", "unlikely but large": "1",
                      "unlikely and small": "1"}


def test_the_average_against_the_median_is_counted_each_way(capsys):
    _run("both = cells")
    assert "Cells with both: 5; the average is above the median in 1, equal in 3, below in 1" in capsys.readouterr().out


def test_what_the_severe_case_averages(capsys):
    _run("draw_counts = cells")
    out = capsys.readouterr().out
    assert "1 cells with 16 draws: the severe case is empty (the worst 5% would be 1 draw)" in out
    assert "1 cells with 20 draws: the severe case averages the worst 1 of them (a single draw)" in out
    assert "6 cells with 128 draws: the severe case averages the worst 7 of them\n" in out
    assert "Cells with an empty severe case: 2" in out


def test_the_thresholds_are_counted(capsys):
    _run("for column in CHANCES")
    out = capsys.readouterr().out
    assert "sb_p_gt0    : above 0 in       5 cells, 0.5 or more in      3" in out
    assert "sb_p_gt25   : above 0 in       2 cells, 0.5 or more in      0" in out
    assert "Rows where a chance rises as the threshold rises: 0" in out


def test_a_chance_that_rises_with_the_threshold_is_counted(capsys):
    table = _table()
    table.loc[1, "sb_p_gt25"] = 0.7   # above its chance of any deaths, 0.6
    # Columns listed out of threshold order, as text sorting would put them: the check must
    # order them by number.
    _run("for column in CHANCES", cells=table, CHANCES=["sb_p_gt25", "sb_p_gt0"])
    assert "Rows where a chance rises as the threshold rises: 1" in capsys.readouterr().out


def test_the_notebook_writes_down_no_month_run_or_draw_count():
    tree = ast.parse("\n".join(_cells_source()))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            assert node.value not in (128, 1024, 16, 64742), f"notebook 06 writes down {node.value!r}"
            if isinstance(node.value, str):
                assert not re.search(r"\b20\d\d-\d\d\b|chrome_box|rusty_bucket", node.value), \
                    f"notebook 06 writes down {node.value!r}"
