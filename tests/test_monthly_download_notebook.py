"""Notebook 04's check cell, run as it is stored, against small good and deliberately broken files.

The cell reads the downloaded files from disk; these tests write tiny ones (two places, two
recorded and two forecast months, per level and outcome) and require each defect to stop the cell
with the failing file named. No server, no download.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from views_crafdapi import data_checks

NOTEBOOK = Path(__file__).parent.parent / "notebooks" / "04_monthly_download.ipynb"
MADE_IN = "2026-07"
MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08")


def _check_cell() -> str:
    cells = json.loads(NOTEBOOK.read_text())["cells"]
    found = ["".join(c["source"]) for c in cells if c["cell_type"] == "code" and "results, summaries" in "".join(c["source"])]
    assert len(found) == 1, "notebook 04 must have exactly one check cell"
    return found[0]


def _file(level, outcome, months=MONTHS, places=(1, 2), draws=1024):
    rows = []
    for m in months:
        for p in places:
            forecast = m >= MADE_IN
            row = {"month": m, data_checks.PLACE_ID[level]: p, "n_draws": draws if forecast else np.nan,
                   f"{outcome}_actual": np.nan if forecast else 2.0, f"{outcome}_mean": 1.5 if forecast else np.nan}
            for c, chance in zip((0, 25, 100, 500, 1000), (0.6, 0.3, 0.1, 0.01, 0.0)):
                row[f"{outcome}_p_gt{c}"] = chance if forecast else np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def _run(tmp_path, change=None, made_in_of=None, expected_draws=None, missing=(), corrupt=(), every=None, truncated=()):
    paths, made_in = {}, {}
    for level in ("grid", "admin1", "country"):
        for outcome in ("sb", "ns", "os"):
            table = _file(level, outcome)
            if change and change[0] == (level, outcome):
                table = change[1](table)
            if every:
                table = every(table)
            paths[level, outcome] = tmp_path / f"{level}_{outcome}.parquet"
            table.to_parquet(paths[level, outcome])
            made_in[level, outcome] = (made_in_of or {}).get((level, outcome), MADE_IN)
            if (level, outcome) in corrupt:
                paths[level, outcome].write_text("<html>502 Bad Gateway</html>")
            if (level, outcome) in truncated:   # keep the footer (schema), damage the data pages
                raw = bytearray(paths[level, outcome].read_bytes())
                raw[4:40] = b"\xff" * 36   # the first data pages; the footer (schema) stays readable
                paths[level, outcome].write_bytes(bytes(raw))
            if (level, outcome) in missing:
                del paths[level, outcome], made_in[level, outcome]
    namespace = {name: getattr(data_checks, name) for name in (
        "PLACE_ID", "DataCheckError", "check_bulk_file_complete", "check_files_agree", "check_forecast_summaries")}
    namespace.update(pd=pd, pq=__import__("pyarrow.parquet", fromlist=["parquet"]), paths=paths, made_in=made_in,
                     EXPECTED_DRAWS=expected_draws, DATA_DIR=tmp_path,
                     LEVELS=("grid", "admin1", "country"), OUTCOMES=("sb", "ns", "os"))
    exec(compile(_check_cell(), str(NOTEBOOK), "exec"), namespace)


def test_good_files_pass(tmp_path, capsys):
    _run(tmp_path, expected_draws=1024)
    out = capsys.readouterr().out
    assert out.count("PASS") == 19 and "FAIL" not in out and "The download is complete" in out


def _empty_last_month(t):
    t.loc[t["month"] == "2026-08", "n_draws"] = 0
    t.loc[t["month"] == "2026-08", [c for c in t.columns if c.startswith("ns_p_gt") or c == "ns_mean"]] = np.nan
    return t


@pytest.mark.parametrize("defect, kwargs, failing", [
    ("the last forecast month of one grid file carries nothing",
     {"change": (("grid", "ns"), _empty_last_month)}, "grid     ns  grid/ns: forecast months with no usable draws"),
    ("a recorded month without recorded deaths",
     {"change": (("admin1", "os"), lambda t: t.assign(os_actual=np.nan))}, "admin1   os  admin1/os: recorded months with no recorded deaths"),
    ("a place missing in one month",
     {"change": (("country", "sb"), lambda t: t.iloc[1:])}, "country  sb  country/sb: months have different numbers of places"),
    ("an empty chance in a forecast row",
     {"change": (("admin1", "sb"), lambda t: t.assign(sb_p_gt25=t["sb_p_gt25"].where(t.index != 5)))}, "admin1   sb  empty values in forecast months"),
    ("the wrong number of draws",
     {"expected_draws": 128}, "grid     sb  1024 draws per row, expected 128"),
    ("an empty file",
     {"change": (("admin1", "ns"), lambda t: t.iloc[0:0])}, "admin1   ns  admin1/ns: the file has no rows"),
    ("a file without one of the columns the checks read",
     {"change": (("country", "ns"), lambda t: t.drop(columns=["n_draws"]))}, "country  ns  country/ns: columns missing from the file: n_draws"),
    ("a place repeated in a month, another missing",
     {"change": (("grid", "sb"), lambda t: t.assign(priogrid_id=t["priogrid_id"].where(t.index != 7, 1)))},
     "grid     sb  grid/sb: 1 rows repeat a place in a month"),
    ("the download stopped before the last three files",
     {"missing": [("country", "sb"), ("country", "ns"), ("country", "os")]},
     "country  os  not downloaded"),
    ("a file that is not Parquet (an error page saved as the file)",
     {"corrupt": [("admin1", "ns")]}, "admin1   ns  the file cannot be read"),
    ("a file whose header reads but whose contents do not",
     {"truncated": [("grid", "os")]}, "grid     os  the file cannot be read"),
    ("one file from another forecast",
     {"made_in_of": {("country", "os"): "2026-08"}}, "all          the files come from different forecasts"),
    ("one file covering other months",
     {"change": (("grid", "os"), lambda t: _file("grid", "os", months=("2026-06", "2026-07", "2026-08")))},
     "all          the files cover different months"),
])
def test_each_defect_stops_the_download(tmp_path, capsys, defect, kwargs, failing):
    with pytest.raises(SystemExit, match="The download is not complete"):
        _run(tmp_path, **kwargs)
    out = capsys.readouterr().out
    assert f"FAIL  {failing}" in out, f"{defect}: expected a FAIL starting {failing!r}\n{out}"


def test_the_notebook_writes_down_no_draw_count_run_month_or_place_count():
    import ast
    import re
    cells = json.loads(NOTEBOOK.read_text())["cells"]
    tree = ast.parse("\n".join("".join(c["source"]) for c in cells if c["cell_type"] == "code"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant):
            assert node.value not in (36, 128, 1024, 202, 2432, 64742), f"notebook 04 writes down {node.value!r}"
            if isinstance(node.value, str):
                assert not re.search(r"\b20\d\d-\d\d\b|chrome_box|rusty_bucket", node.value), \
                    f"notebook 04 writes down {node.value!r}"


def test_the_damaged_file_reads_its_header_but_not_its_data(tmp_path):
    """So the defect above reaches the later reads, not the header read before them."""
    import pyarrow.parquet as pq
    path = tmp_path / "f.parquet"
    _file("grid", "os").to_parquet(path)
    raw = bytearray(path.read_bytes())
    raw[4:40] = b"\xff" * 36
    path.write_bytes(bytes(raw))
    assert pq.ParquetFile(path).schema_arrow.names
    with pytest.raises((OSError, ValueError)):
        pd.read_parquet(path)


def test_every_file_empty_stops_with_failures_not_a_crash(tmp_path, capsys):
    with pytest.raises(SystemExit, match="The download is not complete"):
        _run(tmp_path, every=lambda t: t.iloc[0:0])
    out = capsys.readouterr().out
    assert "FAIL  all          files with no months" in out and out.count("the file has no rows") == 9
