"""
Lightweight client for the VIEWS CRAF'd Prediction API.

Convenience wrappers for downloading bulk files and fetching slices as pandas
DataFrames. Intended for notebooks, scripts, and external consumers.
"""

import os
import tempfile
import time
from pathlib import Path

import pandas as pd
import requests

_BUSY = (502, 503, 504)   # a busy server; 503 also while the service loads a new forecast
_DROPPED = (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError)


class CrafdApiClient:
    """Stateless client for the VIEWS CRAF'd Prediction API."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key}
        self._timeout = timeout

    @staticmethod
    def _extract_detail(response: requests.Response) -> str:
        try:
            return response.json().get("detail", response.text[:300])
        except (ValueError, AttributeError):
            return response.text[:300]

    def _get_json(self, endpoint: str, params: dict | None = None) -> dict:
        """GET ``/{endpoint}`` and return parsed JSON, raising ``RuntimeError`` on non-200."""
        response = requests.get(
            f"{self._base_url}/{endpoint}",
            headers=self._headers,
            params=params,
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"API error {response.status_code} on /{endpoint}: {self._extract_detail(response)}"
            )
        return response.json()

    def health(self) -> dict:
        response = requests.get(
            f"{self._base_url}/health", headers=self._headers, timeout=self._timeout
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"API error {response.status_code} on /health: {self._extract_detail(response)}"
            )
        return response.json()

    def slice(
        self,
        level: str,
        outcome: str,
        *,
        places: list[int] | None = None,
        months: list[str] | None = None,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """Fetch a slice of one outcome at one level as a DataFrame.

        Wraps ``GET /slice/{level}/{outcome}``. Each row is one place in one month: the place's
        id and names, ``month`` (written ``2027-01``), then the outcome's summary columns.
        Forecast months carry the summaries; past months carry the recorded deaths in
        ``{outcome}_actual``.

        **Not every column is a count of deaths.** The ``_p_gt{c}`` columns are chances, 0 to 1,
        and ``n_draws`` counts the draws. See ``docs/api/data_dictionary.md``.

        Parameters
        ----------
        level : str
            ``"grid"``, ``"admin1"`` or ``"country"``.
        outcome : str
            ``"sb"``, ``"ns"`` or ``"os"`` today.
        places : list[int], optional
            Place ids at that level: PRIO-GRID cell ids, GAUL admin-1 codes, or GAUL country
            codes. Default: all places.
        months : list[str], optional
            Months written year-month, such as ``"2027-01"``. Default: all months.
        columns : list[str], optional
            Summary column names. Default: all. The place names and ``month`` always come back.
        """
        if isinstance(places, str) or isinstance(months, str) or isinstance(columns, str):
            raise TypeError("places, months and columns are lists, such as places=[161, 160]")
        params: dict = {}
        if places is not None:
            # int(): ids read from a file often arrive as floats (1301.0); the API takes 1301.
            params["places"] = ",".join(str(int(p)) for p in places)
        if months is not None:
            params["months"] = ",".join(months)
        if columns is not None:
            params["columns"] = ",".join(columns)
        payload = self._get_json(f"slice/{level}/{outcome}", params=params)
        return pd.DataFrame.from_records(payload["rows"], columns=payload["columns"])

    def download_bulk_to(self, level: str, outcome: str, path, *, wait_seconds: float = 60) -> str:
        """Save one bulk file (``GET /bulk/{level}/{outcome}``) at ``path`` and return the month
        the forecast was made in (the ``X-Forecast-Made-In`` header, such as ``"2026-11"``).

        The file is written beside ``path`` and renamed only when complete, so an interrupted
        download never leaves a file that looks whole. A busy server (502, 503, 504) or a
        dropped connection is tried again twice, ``wait_seconds`` apart. Any other answer
        raises ``RuntimeError`` with the service's own explanation.
        """
        path = Path(path)
        partial = path.with_name(path.name + ".part")
        route = f"/bulk/{level}/{outcome}"
        try:
            for attempt in (1, 2, 3):
                try:
                    with requests.get(f"{self._base_url}{route}", headers=self._headers,
                                      timeout=600, stream=True) as response:
                        if response.status_code in _BUSY and attempt < 3:
                            print(f"The server answered {response.status_code}; "
                                  f"trying again in {wait_seconds:.0f} seconds...")
                        elif not response.ok:
                            raise RuntimeError(f"API error {response.status_code} on {route}: "
                                               f"{self._extract_detail(response)}")
                        else:
                            made_in = response.headers.get("X-Forecast-Made-In")
                            if not made_in:
                                raise RuntimeError(f"{route} did not say which month the forecast "
                                                   "was made in (no X-Forecast-Made-In header).")
                            with open(partial, "wb") as out:
                                for chunk in response.iter_content(chunk_size=1 << 20):
                                    out.write(chunk)
                            partial.replace(path)
                            return made_in
                except _DROPPED:
                    if attempt == 3:
                        raise
                    print(f"The download timed out or dropped; trying again in {wait_seconds:.0f} seconds...")
                time.sleep(wait_seconds)
        finally:
            partial.unlink(missing_ok=True)   # never leave a partial file behind

    def download_bulk(self, level: str, outcome: str, months: list[str] | None = None,
                      columns: list[str] | None = None) -> tuple[pd.DataFrame, str]:
        """One bulk file as a DataFrame, and the month the forecast was made in.

        The file is saved to a temporary file first (the grid file is about 130 MB); with
        ``months`` and ``columns``, only those are read from it. Retries as ``download_bulk_to``.
        """
        handle, name = tempfile.mkstemp(suffix=".parquet")
        os.close(handle)
        try:
            made_in = self.download_bulk_to(level, outcome, name)
            filters = [("month", "in", list(months))] if months else None
            return pd.read_parquet(name, columns=columns, filters=filters), made_in
        finally:
            os.unlink(name)
