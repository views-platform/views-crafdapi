"""Post-deploy smoke test for the LIVE CRAF'd API (C-173).

Our 899 unit tests are all mocked/in-process — none hit the deployed service, so a slow / broken /
scope-regressed deploy reached the maintainer only in Jupyter. This is the live check that runs right
after a deploy. It verifies the service: process up, correct build, the store reachable, the
expected run served, and the data right (months, summaries, totals adding up across levels). It
warms nothing: since ADR-044 the service loads the forecast by itself, never in a request.

Run with a reader key from the `crafdapi` project in `CRAFD_KEY` (the name the partner guide
uses), typed at a hidden prompt, never on the command line:

    read -rsp "key: " CRAFD_KEY; echo; export CRAFD_KEY
    .venv/bin/python scripts/smoke.py --expect-tag vX.Y.Z --expect-run RUN_ID --expect-draws 1024

It also checks that the data is right (`views_crafdapi.data_checks`): the months, the summaries,
and that expected and recorded deaths add up from grid cells to admin-1 regions to countries.

Exits non-zero if any check fails. Warms only the key it runs with (per-key cache, ADR-027 §2).
"""
from __future__ import annotations

import argparse
import os
import tempfile
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable, List, Optional

import pandas as pd
import requests

from views_crafdapi.client import CrafdApiClient
from views_crafdapi.data_checks import (
    DataCheckError, check_forecast_summaries, check_levels_add_up, check_months,
)

_DEFAULT_URL = "https://crafdapi.viewsforecasting.org"
_KEY_ENV = "CRAFD_KEY"


@dataclass
class Result:
    name: str
    ok: bool
    detail: str


def _get(base: str, path: str, timeout: float, headers: Optional[dict] = None):
    """``requests.get`` wrapper → ``(status_code|None, json|None, error|None)``."""
    try:
        r = requests.get(f"{base}{path}", headers=headers or {}, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return None, None, str(e)
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body, None


def check_ping(base: str, timeout: float = 15.0) -> Result:
    code, body, err = _get(base, "/ping", timeout)
    if err:
        return Result("ping", False, f"unreachable: {err}")
    if code == 200 and isinstance(body, dict) and body.get("status") == "ok":
        return Result("ping", True, "status ok")
    return Result("ping", False, f"HTTP {code} body={body}")


def check_version(base: str, expect_tag: Optional[str] = None, timeout: float = 15.0) -> Result:
    # An EMPTY expect_tag is an operator error, not "no assertion requested".
    #
    # `if expect_tag and ...` below treats "" as absent, so `--expect-tag ""` used to skip the
    # comparison and report PASS. The release runbook made that reachable on every deploy: it
    # said to run `--expect-tag "$TAG"` after `sudo -iu views-crafdapi-deploy`, and `sudo -i`
    # starts a fresh login shell in which `TAG` — set by the previous user, never exported — is
    # empty by construction. So the assertion ran against nothing while printing `version PASS`.
    #
    # Refusing loudly is the only safe reading: someone who passes the flag is asking for the
    # check, and a check that silently inspects nothing is worse than no check, because it gets
    # credited as evidence. Omitting the flag entirely is still fine — that is `None`.
    if expect_tag is not None and not expect_tag.strip():
        raise ValueError(
            "--expect-tag was given an empty value. This usually means an unset shell variable "
            "(e.g. `--expect-tag \"$TAG\"` after `sudo -iu`, which starts a fresh login shell). "
            "Pass the tag literally, e.g. --expect-tag v0.7.0, or omit the flag."
        )
    code, body, err = _get(base, "/version", timeout)
    if err or code != 200 or not isinstance(body, dict):
        return Result("version", False, f"HTTP {code} err={err}")
    ver, tag, contract = body.get("version"), body.get("deployed_tag"), body.get("served_contract_version")
    detail = f"version={ver} deployed_tag={tag} contract={contract}"
    # The deploy gate guarantees `v{version} == tag`; a disagreement means /version lags the deployed
    # code (the C-167/#296 editable-metadata drift) — surface it.
    if ver and tag and f"v{ver}" != tag:
        return Result("version", False, f"{detail} — version/tag disagree (v{ver} != {tag})")
    if expect_tag and tag != expect_tag:
        return Result("version", False, f"{detail} — expected deployed_tag={expect_tag}")
    return Result("version", True, detail)


def check_health(client: CrafdApiClient) -> Result:
    try:
        h = client.health()
    except Exception as e:  # RuntimeError (non-200) or a connection error
        return Result("health", False, f"unreachable: {e}")
    status = h.get("status")
    fresh = h.get("forecast_freshness") or {}
    detail = (f"status={status} appwrite={h.get('appwrite_connected')} "
              f"freshness_stale={fresh.get('is_stale')}")
    return Result("health", status == "healthy", detail)


def check_served_run(client: CrafdApiClient, expect_run: Optional[str]) -> Result:
    """`/health` names the forecast run partners get; it must be the one expected."""
    if expect_run is not None and not expect_run.strip():
        # An empty value is an unset shell variable (e.g. `--expect-run "$RUN"` after `sudo -iu`),
        # not "no check": refuse, as check_version does for --expect-tag.
        raise ValueError("--expect-run was given an empty value; pass the run id literally, or omit the flag.")
    try:
        run_id = client.health().get("served_run")
    except Exception as e:
        return Result("served run", False, f"unreachable: {e}")
    if run_id and expect_run and run_id != expect_run:
        return Result("served run", False, f"serving {run_id!r}, expected {expect_run!r}")
    if not run_id:   # an older release does not report it; only a failure when one was expected
        return Result("served run", not expect_run,
                      "not reported" + (f", expected {expect_run!r}" if expect_run else " by this release"))
    return Result("served run", True, f"serving {run_id!r}")


def _month_before(month: str) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    return f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"


_LEVEL_COLUMNS = {"grid": ["month", "admin1_code", "country_code"], "admin1": ["month"], "country": ["month"]}


def _read_bulk(base: str, key: str, level: str, timeout: float, months=None, columns=None):
    """One bulk file of state-based deaths, as a partner downloads it, saved to a temporary file
    (the grid file is about 130 MB) and read back with only `months` and `columns`; no slice size
    limit applies. One retry on a timeout, a dropped connection, 502 or 504. Returns the table and
    the month the forecast was made in."""
    for attempt in (1, 2):
        try:
            r = requests.get(f"{base}/bulk/{level}/sb", headers={"X-API-Key": key}, timeout=timeout, stream=True)
            if r.status_code in (502, 504) and attempt == 1:
                r.close()
                time.sleep(10)
                continue
            r.raise_for_status()
            with tempfile.NamedTemporaryFile(suffix=".parquet") as out:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    out.write(chunk)
                out.flush()
                filters = [("month", "in", list(months))] if months else None
                return pd.read_parquet(out.name, columns=columns, filters=filters), r.headers["X-Forecast-Made-In"]
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError):
            if attempt == 2:
                raise
            time.sleep(10)
    raise AssertionError("not reached: the second attempt returns or raises")


def check_data(base: str, key: str, expect_draws: Optional[int], timeout: float) -> List[Result]:
    """The data is right: the country bulk file's months and summaries, and the first forecast
    month and the last recorded month adding up across the three levels (from the bulk files)."""
    try:
        country, made_in = _read_bulk(base, key, "country", timeout)
    except Exception as e:
        return [Result("data", False, f"country bulk file: {e!r}")]
    results = []
    for name, check in (
        ("data: months", lambda: check_months(country, made_in)),
        ("data: summaries", lambda: check_forecast_summaries(country, "sb", made_in, expect_draws)),
    ):
        try:
            results.append(Result(name, True, check()))
        except DataCheckError as e:
            results.append(Result(name, False, str(e)))
        except Exception as e:   # a malformed table is a failed check, not a crash
            results.append(Result(name, False, f"error: {e!r}"))
    months = [made_in, _month_before(made_in)]
    try:
        tables = {level: _read_bulk(base, key, level, timeout, months,
                                    _LEVEL_COLUMNS[level] + ["sb_mean", "sb_actual"])[0]
                  for level in ("grid", "admin1", "country")}
    except Exception as e:
        return results + [Result("data: levels", False, f"bulk files: {e!r}")]
    for month, column in zip(months, ("sb_mean", "sb_actual")):
        name = f"data: {column} adds up"
        try:
            by_level = {level: t[t["month"] == month] for level, t in tables.items()}
            results.append(Result(name, True, check_levels_add_up(by_level, column)))
        except DataCheckError as e:
            results.append(Result(name, False, str(e)))
        except Exception as e:   # a malformed table is a failed check, not a crash
            results.append(Result(name, False, f"error: {e!r}"))
    return results


def fetch_retry_once(fetch: Callable, on_retry: Callable[[], None] = lambda: None):
    """Run ``fetch()``; on a cold-server timeout, warm-and-retry once. uvicorn finishes the dataset
    load server-side even after the client disconnects, so the retry hits a warm cache."""
    try:
        return fetch()
    except requests.exceptions.Timeout:
        on_retry()
        return fetch()


def check_coverage(client: CrafdApiClient, month: str, country: str, column: str) -> Result:
    """One month of admin-1 rows for state-based violence: the country asked for must be in
    it, with a value in `column` (`sb_mean` for a forecast month, `sb_actual` for a past one)."""
    name = f"{column} coverage[{country}]"
    try:
        df = fetch_retry_once(
            lambda: client.slice("admin1", "sb", months=[month], columns=[column]),
            on_retry=lambda: print(f"  [{name}] server cache was cold — warmed, retrying once…", flush=True),
        )
    except Exception as e:
        return Result(name, False, f"error: {e}")
    in_country = df[df["country_iso3"].astype(str) == country] if "country_iso3" in df.columns else df.iloc[0:0]
    valued = int(in_country[column].notna().sum()) if column in in_country.columns else 0
    return Result(name, valued > 0,
                  f"{len(df):,} admin-1 rows, {country}: {len(in_country)} rows, {valued} with a value")


def check_warm(client: CrafdApiClient, month: str, country: str, threshold_s: float = 15.0) -> Result:
    """A second call should be fast (warm) — proves the warm-up actually took."""
    t0 = time.monotonic()
    try:
        client.slice("admin1", "sb", months=[month], columns=["sb_mean"])
    except Exception as e:
        return Result("warm check", False, f"error: {e}")
    dt = time.monotonic() - t0
    return Result("warm check", dt <= threshold_s, f"second forecast call {dt:.1f}s (<= {threshold_s}s)")


def run(base: str, key: str, *, expect_tag: Optional[str], forecast_month: str, hist_month: str,
        country: str = "IDN", timeout: float = 600.0, expect_run: Optional[str] = None,
        expect_draws: Optional[int] = None) -> List[Result]:
    results = [check_ping(base), check_version(base, expect_tag)]
    if not key:
        results.append(Result("auth", False, f"{_KEY_ENV} not set — cannot run authed checks"))
        return results
    client = CrafdApiClient(base, key, timeout=timeout)
    results.append(check_health(client))
    # The first coverage call is also the cold-load warm-up (it loads forecast and history).
    results.append(check_coverage(client, forecast_month, country, "sb_mean"))
    results.append(check_coverage(client, hist_month, country, "sb_actual"))
    results.append(check_warm(client, forecast_month, country))
    results.append(check_served_run(client, expect_run))
    results.extend(check_data(base, key, expect_draws, timeout))
    return results


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Post-deploy smoke test + cache warmer for the live CRAF'd API.")
    p.add_argument("--url", default=os.environ.get("VIEWS_API_URL", _DEFAULT_URL))
    p.add_argument("--expect-tag", default=None,
                   help="assert GET /version deployed_tag == this (e.g. v1.3.9)")
    p.add_argument("--forecast-month", default=datetime.now(timezone.utc).strftime("%Y-%m"),
                   help="a month inside the served forecast, year-month (default: this month, "
                        "which a monthly forecast covers; a month outside it fails with 422)")
    p.add_argument("--hist-month", default="2024-12", help="a past month, year-month")
    p.add_argument("--country", default="IDN", help="a non-African ISO3 to prove global coverage")
    p.add_argument("--expect-run", default=None,
                   help="assert /health served_run == this (e.g. chrome_box_forecasting_20261112_210150)")
    p.add_argument("--expect-draws", type=int, default=None,
                   help="assert every forecast row comes from this many draws (e.g. 1024)")
    p.add_argument("--timeout", type=float, default=600.0,
                   help="per-request timeout (s); the first cold load can take minutes")
    args = p.parse_args(argv)
    if args.expect_run is not None and not args.expect_run.strip():
        p.error("--expect-run was given an empty value; pass the run id literally, or omit the flag.")

    base = args.url.rstrip("/")
    key = os.environ.get(_KEY_ENV, "").strip().strip('"')
    print(f"Smoke test → {base}  (country={args.country}, fc_month={args.forecast_month}, "
          f"hist_month={args.hist_month}, timeout={args.timeout:.0f}s)", flush=True)
    results = run(base, key, expect_tag=args.expect_tag, forecast_month=args.forecast_month,
                  hist_month=args.hist_month, country=args.country, timeout=args.timeout,
                  expect_run=args.expect_run, expect_draws=args.expect_draws)
    print("\n  RESULT                          STATUS  DETAIL")
    for r in results:
        print(f"  {r.name:31s} {'PASS' if r.ok else 'FAIL':6s}  {r.detail}", flush=True)
    failed = [r for r in results if not r.ok]
    print(f"\n{'ALL PASS' if not failed else str(len(failed)) + ' CHECK(S) FAILED'} — {base}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
