"""Notebook 05's measuring cells, run as they are stored, on small tables whose answers are known.

Notebook 05 states each trap with a number measured on the data served. These tests feed its cells
four small countries, built so that every number is known in advance, and
require the cells to print those numbers: expected deaths adding up (and a stop when they do not),
a country below its highest cell counted rather than hidden, chances summing past 1, and severe
cases overstated by summing. No server, no download.
"""
import ast
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from views_crafdapi import data_checks

NOTEBOOK = Path(__file__).parent.parent / "notebooks" / "05_totals.ipynb"
MONTH = "2026-07"
NGA, OTHER, THIRD, FOURTH, FIFTH, SIXTH = 566, 999, 777, 444, 333, 222


def _cells():
    return ["".join(c["source"]) for c in json.loads(NOTEBOOK.read_text())["cells"] if c["cell_type"] == "code"]


def _cell(marker):
    found = [c for c in _cells() if marker in c]
    assert len(found) == 1, f"notebook 05 must have exactly one cell containing {marker!r}"
    return found[0]


def _tables(country_mean_nga=6.0, country_mean_other=1.5, region_means=(3.0, 3.0)):
    # NGA: three cells in two regions. Its chance of any death (0.9) is above its highest cell's
    # (0.6); the cells average 0.4.
    # OTHER is served BELOW its highest cell (0.2 < 0.3): the ordering break the notebook must
    #   count, not hide. Its cells' chances of more than 25 add up to 1.8, more than Nigeria's 1.5,
    #   so the example must be OTHER (the highest sum), not the first country in code order.
    # THIRD is one cell: its chance of more than 25 is exactly 1, which is not more than 1, and its
    #   severe case equals its cell's.
    # FOURTH has a cell with no severe case (fewer than 20 draws); summing the other would show a
    #   sum below the country's (5 < 6), so the country must be left out, and counted.
    # FIFTH and SIXTH are served but have no grid cells: they cannot be compared and must be counted
    #   as left out, not dropped. FIFTH's severe case is above zero, SIXTH's is 0.
    # NGA has a fourth cell with no chances given and nothing expected; the average and the cell
    #   count use only the cells with a chance. FOURTH's second cell has a chance of any death but
    #   none of more than 25: two cells without the one, one without the other.
    # Severe-case ratios of the countries kept: 2.0, 1.0, 1.0 (median 1.0, mean 1.33).
    grid = pd.DataFrame({
        "month": MONTH, "priogrid_id": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        "admin1_code": [10, 10, 11, 20, 20, 20, 30, 40, 40, 11],
        "country_code": [NGA, NGA, NGA, OTHER, OTHER, OTHER, THIRD, FOURTH, FOURTH, NGA],
        "sb_mean": [1.0, 2.0, 3.0, 0.5, 0.5, 0.5, 2.0, 1.0, 1.0, 0.0],
        "sb_p_gt0": [0.6, 0.4, 0.2, 0.3, 0.1, 0.1, 0.5, 0.2, 0.2, float("nan")],
        "sb_p_gt25": [0.6, 0.5, 0.4, 0.9, 0.9, 0.0, 1.0, 0.0, float("nan"), float("nan")],
        "sb_severe_scenario": [40.0, 30.0, 30.0, 10.0, 0.0, 0.0, 9.0, 5.0, float("nan"), 0.0],
    })
    admin1 = pd.DataFrame({"month": MONTH, "admin1_code": [10, 11, 20, 30, 40], "admin1_name": list("abcde"),
                           "country_code": [NGA, NGA, OTHER, THIRD, FOURTH],
                           "sb_mean": [*region_means, 1.5, 2.0, 2.0],
                           "sb_p_gt0": [0.7, 0.2, 0.35, 0.5, 0.3], "sb_p_gt25": [0.7, 0.4, 0.9, 1.0, 0.0],
                           "sb_severe_scenario": [50.0, 30.0, 10.0, 9.0, 6.0]})
    country = pd.DataFrame({"month": MONTH, "country_code": [NGA, OTHER, THIRD, FOURTH, FIFTH, SIXTH],
                            "country_name": ["Nigeria", "Other", "Third", "Fourth", "Fifth", "Sixth"],
                            "country_iso3": ["NGA", "OTH", "THR", "FOU", "FIF", "SIX"],
                            "sb_mean": [country_mean_nga, country_mean_other, 2.0, 2.0, 0.0, 0.0],
                            "sb_p_gt0": [0.9, 0.2, 0.5, 0.3, 0.1, 0.0], "sb_p_gt25": [0.8, 0.9, 1.0, 0.0, 0.05, 0.0],
                            "sb_severe_scenario": [50.0, 10.0, 9.0, 6.0, 3.0, 0.0]})
    return grid, admin1, country


def _run(*markers, **tables):
    grid, admin1, country = _tables(**tables)
    namespace = {"pd": pd, "grid": grid, "admin1": admin1, "country": country, "NIGERIA": NGA,
                 "check_levels_add_up": data_checks.check_levels_add_up, "DataCheckError": data_checks.DataCheckError}
    for marker in markers:
        exec(compile(_cell(marker), str(NOTEBOOK), "exec"), namespace)
    return namespace


def test_expected_deaths_add_up(capsys):
    _run("check_levels_add_up({")
    out = capsys.readouterr().out
    assert "adds up" in out and "Nigeria: 6.0 expected deaths; its 4 cells add up to 6.0" in out
    assert ("countries: 4 compared with their own cells; not adding up: 0; "
            "left out (no value served, or no cell with a value): 2") in out
    assert ("regions: 5 compared with their own cells; not adding up: 0; "
            "left out (no value served, or no cell with a value): 0") in out


def test_a_small_error_that_cancels_out_still_stops_the_notebook():
    """0.1% apart, far above float32 rounding, and hidden in the world total."""
    with pytest.raises(SystemExit, match="Expected deaths do not add up for country 566"):
        _run("check_levels_add_up({", country_mean_nga=6.006, country_mean_other=1.494)


def test_a_country_served_below_its_cells_stops_the_notebook():
    """The mirror of the case above: Nigeria 0.1% below its cells, Other above."""
    with pytest.raises(SystemExit, match="Expected deaths do not add up for country 566"):
        _run("check_levels_add_up({", country_mean_nga=5.994, country_mean_other=1.506)


def test_two_regions_whose_errors_cancel_out_stop_the_notebook():
    """Nigeria and the world add up; its two regions do not."""
    with pytest.raises(SystemExit, match="Expected deaths do not add up for region 10"):
        _run("check_levels_add_up({", region_means=(3.5, 2.5))


def test_two_countries_whose_errors_cancel_out_stop_the_notebook():
    """The world total still adds up (7.0 + 0.5 = 6.0 + 1.5); each country does not."""
    with pytest.raises(SystemExit, match="Expected deaths do not add up for country"):
        _run("check_levels_add_up({", country_mean_nga=7.0, country_mean_other=0.5)


def test_expected_deaths_that_do_not_add_up_stop_the_notebook():
    with pytest.raises(SystemExit, match="The check of expected deaths failed: sb_mean does not add up"):
        _run("check_levels_add_up({", country_mean_nga=7.0)


def test_the_chance_of_any_death_is_compared_with_the_highest_and_the_average_cell(capsys):
    namespace = _run("def compare_chances")
    out = capsys.readouterr().out
    assert "Nigeria: chance of any death 0.90; highest cell 0.60; average of its 3 cells 0.400" in out
    # Gaps (served minus average of cells): countries 0.50, 0.03, 0.00, 0.10;
    # regions 0.20, 0.00, 0.18, 0.00, 0.10.
    assert ("country : 4 places; the average of cells understates the served chance by up to 0.50 "
            "(median 0.07); places below their highest cell: 1; left out (no value served, or no cell with a value): 2") in out
    assert ("admin1  : 5 places; the average of cells understates the served chance by up to 0.20 "
            "(median 0.10); places below their highest cell: 0; left out (no value served, or no cell with a value): 0") in out
    assert list(namespace["chances"]["country"].query("breaks_the_ordering").index) == [OTHER]


def test_a_place_with_no_chance_served_is_counted_not_dropped(capsys):
    """A place whose summaries are empty (see #283) is left out of the comparison, and said so."""
    grid, admin1, country = _tables()
    country.loc[country["country_code"] == THIRD, "sb_p_gt0"] = float("nan")
    namespace = {"pd": pd, "grid": grid, "admin1": admin1, "country": country, "NIGERIA": NGA}
    exec(compile(_cell("def compare_chances"), str(NOTEBOOK), "exec"), namespace)
    out = capsys.readouterr().out
    assert "country : 3 places;" in out and "left out (no value served, or no cell with a value): 3" in out


def test_chances_summed_over_cells_pass_one(capsys):
    _run("summed = grid.groupby")
    out = capsys.readouterr().out
    assert "Grid cells with no chance of more than 25 deaths given (left out of the sums): 2" in out
    assert "add up to more than 1: 2" in out   # Third's add up to exactly 1, which does not count
    assert "For example Other: the cells add up to 1.80; the country's chance is 0.90" in out


def test_the_severe_case_summed_over_cells_overstates_it(capsys):
    _run("severe = pd.DataFrame")
    out = capsys.readouterr().out
    # Above zero: NGA, OTHER, THIRD, FOURTH (a cell has none), FIFTH (no cells); SIXTH is 0.
    assert ("Countries with no severe case served: 0; with one above zero: 5; "
            "left out of those (a cell has none, or no cells): 2") in out
    assert ("Countries compared: 3; the sum of the cells' severe cases is 1.0 times "
            "the country's (median), from 1.0 to 2.0 times") in out
    assert "Countries where the sum is below the country's severe case: 0" in out
    assert "Nigeria: severe case 50 deaths; its cells' severe cases add up to 100" in out


def test_a_severe_case_above_the_sum_of_its_cells_is_counted(capsys):
    """It should not happen; if the data ever shows it, the notebook says so."""
    namespace = {"pd": pd, "NIGERIA": NGA}
    grid, admin1, country = _tables()
    country.loc[country["country_code"] == THIRD, "sb_severe_scenario"] = 20.0
    namespace.update(grid=grid, country=country)
    exec(compile(_cell("severe = pd.DataFrame"), str(NOTEBOOK), "exec"), namespace)
    assert "Countries where the sum is below the country's severe case: 1" in capsys.readouterr().out


def test_the_notebook_writes_down_no_month_run_or_count():
    tree = ast.parse("\n".join(_cells()))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            assert node.value not in (202, 2432, 64742, 128, 1024), f"notebook 05 writes down {node.value!r}"
            if isinstance(node.value, str):
                assert not re.search(r"\b20\d\d-\d\d\b|chrome_box|rusty_bucket", node.value), \
                    f"notebook 05 writes down {node.value!r}"
