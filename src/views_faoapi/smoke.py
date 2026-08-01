"""Post-deploy smoke test + cache warmer for the LIVE FAO API (C-173).

Our 899 unit tests are all mocked/in-process — none hit the deployed service, so a slow / broken /
scope-regressed deploy reached the maintainer only in Jupyter. This is the live check that runs right
after a deploy. It (1) VERIFIES the service — process up, correct build, Appwrite reachable, and BOTH
historical & forecast serve with GLOBAL coverage (a non-African country returns cells — the exact
regional-historical regression that bit us) — and (2) WARMS the server caches: a single-country subset
still forces the full server-side dataset load (`api.py` `_get_latest_dataset`), so the first real
consumer call afterwards is fast instead of timing out.

Run from the deploy shell (with a caller/read-scoped key exported):

    APPWRITE_DATASTORE_API_KEY=<caller key> .venv/bin/python scripts/smoke.py --expect-tag vX.Y.Z
    # or:  python -m views_faoapi.smoke --expect-tag vX.Y.Z

Exits non-zero if any check fails. Warms only the key it runs with (per-key cache, ADR-027 §2).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import requests

from views_faoapi.client import FaoApiClient
from views_faoapi.time import date_to_month_id

_DEFAULT_URL = "https://faoapi.viewsforecasting.org"
_KEY_ENV = "APPWRITE_DATASTORE_API_KEY"


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


def check_health(client: FaoApiClient) -> Result:
    try:
        h = client.health()
    except Exception as e:  # RuntimeError (non-200) or a connection error
        return Result("health", False, f"unreachable: {e}")
    status = h.get("status")
    fresh = h.get("forecast_freshness") or {}
    detail = (f"status={status} appwrite={h.get('appwrite_connected')} "
              f"freshness_stale={fresh.get('is_stale')}")
    return Result("health", status == "healthy", detail)


def fetch_retry_once(fetch: Callable, on_retry: Callable[[], None] = lambda: None):
    """Run ``fetch()``; on a cold-server timeout, warm-and-retry once. uvicorn finishes the dataset
    load server-side even after the client disconnects, so the retry hits a warm cache."""
    try:
        return fetch()
    except requests.exceptions.Timeout:
        on_retry()
        return fetch()


def check_coverage(client: FaoApiClient, level: str, month: int, country: str,
                   data_type: str, features: Optional[List[str]] = None) -> Result:
    name = f"{data_type} coverage[{country}]"
    try:
        df = fetch_retry_once(
            lambda: client.fetch_subset(level, [month], country, data_type=data_type, features=features),
            on_retry=lambda: print(f"  [{name}] server cache was cold — warmed, retrying once…", flush=True),
        )
    except Exception as e:
        return Result(name, False, f"error: {e}")
    n = len(df)
    present = country in set(df["country_iso_a3"].astype(str)) if "country_iso_a3" in df.columns else False
    return Result(name, n > 0 and present, f"{n:,} cells, {country} present={present}")


def check_warm(client: FaoApiClient, level: str, month: int, country: str,
               data_type: str, features: Optional[List[str]] = None, threshold_s: float = 15.0) -> Result:
    """A second call should be fast (warm) — proves the warm-up actually took."""
    t0 = time.monotonic()
    try:
        client.fetch_subset(level, [month], country, data_type=data_type, features=features)
    except Exception as e:
        return Result("warm check", False, f"error: {e}")
    dt = time.monotonic() - t0
    return Result("warm check", dt <= threshold_s, f"second {data_type} call {dt:.1f}s (<= {threshold_s}s)")


def run(base: str, key: str, *, expect_tag: Optional[str], forecast_month: int, hist_month: int,
        country: str = "IDN", timeout: float = 600.0) -> List[Result]:
    results = [check_ping(base), check_version(base, expect_tag)]
    if not key:
        results.append(Result("auth", False, f"{_KEY_ENV} not set — cannot run authed checks"))
        return results
    client = FaoApiClient(base, key, timeout=timeout)
    results.append(check_health(client))
    # The two coverage calls are also the cold-load warm-up (forecast, then historical).
    results.append(check_coverage(client, "country", forecast_month, country, "forecast", ["pred_lr_ged_sb"]))
    results.append(check_coverage(client, "country", hist_month, country, "historical"))
    results.append(check_warm(client, "country", forecast_month, country, "forecast", ["pred_lr_ged_sb"]))
    return results


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Post-deploy smoke test + cache warmer for the live FAO API.")
    p.add_argument("--url", default=os.environ.get("VIEWS_API_URL", _DEFAULT_URL))
    p.add_argument("--expect-tag", default=None,
                   help="assert GET /version deployed_tag == this (e.g. v1.3.9)")
    p.add_argument("--forecast-month", type=int, default=date_to_month_id(2026, 7),
                   help="in-window forecast month_id (default 559 = 2026-07; advances per run)")
    p.add_argument("--hist-month", type=int, default=date_to_month_id(2024, 12))
    p.add_argument("--country", default="IDN", help="a non-African ISO3 to prove global coverage")
    p.add_argument("--timeout", type=float, default=600.0,
                   help="per-request timeout (s); the first cold load can take minutes")
    args = p.parse_args(argv)

    base = args.url.rstrip("/")
    key = os.environ.get(_KEY_ENV, "").strip().strip('"')
    print(f"Smoke test → {base}  (country={args.country}, fc_month={args.forecast_month}, "
          f"hist_month={args.hist_month}, timeout={args.timeout:.0f}s)", flush=True)
    results = run(base, key, expect_tag=args.expect_tag, forecast_month=args.forecast_month,
                  hist_month=args.hist_month, country=args.country, timeout=args.timeout)
    print("\n  RESULT                          STATUS  DETAIL")
    for r in results:
        print(f"  {r.name:31s} {'PASS' if r.ok else 'FAIL':6s}  {r.detail}", flush=True)
    failed = [r for r in results if not r.ok]
    print(f"\n{'ALL PASS' if not failed else str(len(failed)) + ' CHECK(S) FAILED'} — {base}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
