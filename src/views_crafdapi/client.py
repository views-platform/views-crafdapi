"""
Lightweight client for the VIEWS FAO Prediction API.

Convenience wrappers for fetching data subsets and converting responses
to pandas DataFrames. Intended for notebooks, scripts, and external consumers.
"""

import pandas as pd
import requests


class FaoApiClient:
    """Stateless client for the VIEWS FAO Prediction API."""

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

    def fetch_subset(
        self,
        level: str,
        time_ids: list[int],
        entity_ids: list[int] | list[str] | str | None = None,
        *,
        data_type: str = "historical",
        features: list[str] | None = None,
        with_metadata: bool = True,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetch a data subset and return it as a DataFrame.

        Parameters
        ----------
        level : str
            Geographic level: "pg", "country", "gaul0", "gaul1", or "gaul2".
        time_ids : list[int]
            VIEWS month IDs to fetch.
        entity_ids : list, str, or None
            Entity identifiers (PRIO-GRID ints, ISO3 strings, or GAUL codes). ``None`` (the
            default) omits the filter, returning ALL entities for the month — the full global
            footprint (used by the coverage notebooks).
        data_type : str
            "historical" or "forecast" (default: "historical").
        features : list[str], optional
            Which features to include (default: all).
        with_metadata : bool
            Include geographic context columns (default: True).
        force_refresh : bool
            Bypass server cache (default: False).
        """
        if data_type not in ("historical", "forecast"):
            raise ValueError(f"data_type must be 'historical' or 'forecast', got {data_type!r}")

        params: dict = {
            "time_ids": ",".join(map(str, time_ids)),
            "with_metadata": with_metadata,
            "force_refresh": force_refresh,
        }
        # entity_ids is optional: when omitted (None) the server returns ALL entities for the
        # month — i.e. the full global footprint — which the coverage notebooks rely on.
        if entity_ids is not None:
            if isinstance(entity_ids, str):
                params["entity_ids"] = entity_ids
            else:
                params["entity_ids"] = ",".join(map(str, entity_ids))
        if features:
            params["features"] = ",".join(features)

        endpoint = f"{level}/data/{data_type}/subset"
        response = requests.get(
            f"{self._base_url}/{endpoint}",
            headers=self._headers,
            params=params,
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"API error {response.status_code} on {endpoint}: "
                f"{self._extract_detail(response)}"
            )

        payload = response.json()
        data = payload["data"]
        df = pd.DataFrame.from_dict(data["dataframe"])
        return df

    def hdi_map(
        self,
        level: str,
        time_ids: list[int] | None = None,
        entity_ids: list[int] | list[str] | str | None = None,
        *,
        data_type: str = "forecast",
        features: list[str] | None = None,
        alpha: float = 0.9,
        aggregate: bool = False,
        enforce_non_negative: bool = False,
        sample_idx: list[int] | None = None,
        with_metadata: bool = True,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetch collapsed HDI + MAP estimates as a DataFrame — the headline forecast product.

        Wraps ``GET /{level}/analysis/{data_type}/hdi-map`` so consumers don't hand-roll HTTP.
        For a forecast, each row carries, per violence series, the point estimate and nested
        credible intervals — ``sb_map`` and ``sb_hdi{50,90,95}_{lower,upper}`` (and ``ns_``/``os_``),
        plus ``sb_severe_scenario`` and ``sb_bimodality_flag``. Values are **raw fatality counts**
        (ADR-024); see ``docs/api/data_dictionary.md``.

        Parameters mirror :meth:`fetch_subset`, plus:
        alpha : Primary HDI mass (0.9 → 90%); the response always includes the signed-off 50/90/95
            levels regardless.
        aggregate : Sum aligned posterior draws to ``level`` before collapsing (honest joint
            uncertainty — the HDI of the sum, not the sum of HDIs).
        enforce_non_negative : Clip MAP estimates at 0 (posterior draws can be negative).
        """
        if data_type not in ("historical", "forecast"):
            raise ValueError(f"data_type must be 'historical' or 'forecast', got {data_type!r}")

        params: dict = {
            "alpha": alpha,
            "aggregate": aggregate,
            "enforce_non_negative": enforce_non_negative,
            "with_metadata": with_metadata,
            "force_refresh": force_refresh,
        }
        if time_ids is not None:
            params["time_ids"] = ",".join(map(str, time_ids))
        if entity_ids is not None:
            params["entity_ids"] = (
                entity_ids if isinstance(entity_ids, str) else ",".join(map(str, entity_ids))
            )
        if features:
            params["features"] = ",".join(features)
        if sample_idx is not None:
            params["sample_idx"] = ",".join(map(str, sample_idx))

        payload = self._get_json(f"{level}/analysis/{data_type}/hdi-map", params=params)
        return pd.DataFrame.from_records(payload["data"]["hdi_map"])

    def provenance(self, category: str = "forecast") -> dict:
        """Return the provenance/lineage of the latest *served* artifact for ``category``.

        Wraps ``GET /provenance/{category}``: the run id / filename, creation time, methodology
        version, freshness verdict, and (for forecasts) the served mode and serving state —
        everything needed to record *which forecast run produced a number or figure*.
        """
        if category not in ("historical", "forecast"):
            raise ValueError(f"category must be 'historical' or 'forecast', got {category!r}")
        return self._get_json(f"provenance/{category}")["data"]
