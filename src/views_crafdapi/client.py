"""
Lightweight client for the VIEWS CRAF'd Prediction API.

Convenience wrappers for fetching slices and converting responses to pandas
DataFrames. Intended for notebooks, scripts, and external consumers.
"""

import pandas as pd
import requests


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

        **Not every column is a count.** The ``_p_gt{c}`` columns are probabilities in [0, 1],
        and ``_bimodality_flag`` is 0 or 1. See ``docs/api/data_dictionary.md``.

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
