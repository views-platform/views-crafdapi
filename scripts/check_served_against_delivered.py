#!/usr/bin/env python3
"""Check that what the API serves matches the delivered draws (definition of done, item 2).

For each delivered forecast shard given (one outcome, one month, every draw of every grid cell),
recompute each cell's expected deaths (the mean of its draws), its chance of any deaths (the
share of draws above 0) and its number of draws, straight from the shard with pyarrow and pandas,
none of the service's own code, and compare them with the grid bulk file the API serves for that
outcome and month. The grid bulk file is downloaded once per outcome.

Run on the server after a delivery, with our reader key typed at a hidden prompt:

    read -rsp "reader key: " CRAFD_KEY; echo; export CRAFD_KEY
    .venv/bin/python scripts/check_served_against_delivered.py \\
        apis/un_crafd/cache/appwrite_cache/crafd_bucket/RUN__lr_ged_sb__m000NNN.arrow.parquet

Exits 0 when every shard matches, 1 otherwise. Prints no key.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import sys

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import requests

_DEFAULT_URL = "https://crafdapi.viewsforecasting.org"
_SHARD_NAME = re.compile(r"__lr_ged_(sb|ns|os)__m(\d+)\.arrow\.parquet$")
_RELATIVE_TOLERANCE = 1e-6


def month_of(month_id: int) -> str:
    """VIEWS month 1 is 1980-01."""
    return f"{1980 + (month_id - 1) // 12}-{(month_id - 1) % 12 + 1:02d}"


def from_shard(path: str, month_id: int) -> pd.DataFrame:
    """Per grid cell: mean of the draws, share of draws above 0, number of draws. The shard's own
    `time` column must be the month its name says."""
    table = pq.read_table(path, columns=["time", "unit", "value"]).to_pandas()
    months = set(table["time"].unique())
    if months != {month_id}:
        raise ValueError(f"the shard holds months {sorted(months)}, its name says {month_id}")
    by_cell = table.groupby("unit")["value"]
    any_deaths = (table["value"] > 0).groupby(table["unit"])
    return pd.DataFrame({"mean": by_cell.mean(), "p_gt0": any_deaths.mean(), "n_draws": by_cell.size()})


_GRID_FILES: dict = {}


def _grid_file(base: str, key: str, outcome: str, timeout: float) -> bytes:
    """The served grid bulk file for an outcome, downloaded once per run of this script."""
    if outcome not in _GRID_FILES:
        r = requests.get(f"{base}/bulk/grid/{outcome}", headers={"X-API-Key": key}, timeout=timeout)
        r.raise_for_status()
        _GRID_FILES[outcome] = r.content
    return _GRID_FILES[outcome]


def compare(path: str, base: str, key: str, timeout: float) -> tuple[bool, str]:
    match = _SHARD_NAME.search(os.path.basename(path))
    if not match:
        return False, "not a forecast shard name (…__lr_ged_<outcome>__m<month>.arrow.parquet)"
    outcome, month_id = match[1], int(match[2])
    month = month_of(month_id)
    delivered = from_shard(path, month_id)
    served = pd.read_parquet(
        io.BytesIO(_grid_file(base, key, outcome, timeout)), filters=[("month", "==", month)],
        columns=["priogrid_id", f"{outcome}_mean", f"{outcome}_p_gt0", "n_draws"],
    ).set_index("priogrid_id")
    missing = delivered.index.difference(served.index)
    extra = served.index.difference(delivered.index)
    if len(missing) or len(extra):
        return False, f"{outcome} {month}: {len(missing)} delivered cells not served, {len(extra)} served cells not delivered"
    served = served.loc[delivered.index]
    scale = np.maximum(np.abs(delivered["mean"].to_numpy()), 1.0)
    mean_gap = float(np.max(np.abs(served[f"{outcome}_mean"].to_numpy() - delivered["mean"].to_numpy()) / scale))
    p_gap = float(np.max(np.abs(served[f"{outcome}_p_gt0"].to_numpy() - delivered["p_gt0"].to_numpy())))
    draws_equal = bool((served["n_draws"].to_numpy() == delivered["n_draws"].to_numpy()).all())
    ok = mean_gap <= _RELATIVE_TOLERANCE and p_gap <= 1e-9 and draws_equal
    return ok, (f"{outcome} {month}: {len(delivered):,} cells; largest gap in expected deaths "
                f"{mean_gap:.2e} (relative), in chance of any deaths {p_gap:.2e}; "
                f"draws {'equal' if draws_equal else 'DIFFER'}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("shards", nargs="+", help="delivered forecast shard files")
    p.add_argument("--url", default=os.environ.get("VIEWS_API_URL", _DEFAULT_URL))
    p.add_argument("--timeout", type=float, default=600.0)
    args = p.parse_args(argv)
    key = os.environ.get("CRAFD_KEY", "").strip()
    if not key:
        print("CRAFD_KEY is not set (type it at a hidden prompt first)", file=sys.stderr)
        return 1
    failed = 0
    for path in args.shards:
        try:
            ok, detail = compare(path, args.url.rstrip("/"), key, args.timeout)
        except Exception as e:   # a failed download or an unreadable file is a failed check
            ok, detail = False, f"{os.path.basename(path)}: error {e!r}"
        failed += not ok
        print(f"{'MATCH' if ok else 'DIFFER'}  {detail}", flush=True)
    print("ALL MATCH" if not failed else f"{failed} SHARD(S) DIFFER")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
