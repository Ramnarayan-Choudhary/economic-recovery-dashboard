"""FRED client with optional REST authentication and resilient fallbacks."""

from __future__ import annotations

import csv
import io
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

from app.indicators import CACHE_TTL_SECONDS, OBSERVATION_START

FRED_REST_URL = "https://api.stlouisfed.org/fred/series/observations"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
DEFAULT_SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "data"
SourceMode = Literal["rest_api", "public_csv", "snapshot"]


class FredClientError(RuntimeError):
    """Base exception for transport and source-data failures."""


class FredAPIError(FredClientError):
    """Raised when no configured FRED source or local snapshot is usable."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One numeric observation in a FRED time series."""

    date: date
    value: float


@dataclass(frozen=True, slots=True)
class SeriesData:
    """Observations plus enough provenance for users to judge freshness."""

    observations: tuple[Observation, ...]
    source: SourceMode
    loaded_at_utc: str


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    series_data: SeriesData


class FredClient:
    """Fetch FRED observations and cache them for a bounded period.

    A configured API key enables the official REST endpoint. Without one—or if that
    request fails—the client uses FRED's public graph CSV download. Checked-in CSV
    snapshots are the final fallback, keeping the local prototype demonstrable during
    a network outage without adding a database dependency.
    """

    def __init__(
        self,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        snapshot_dir: Path | None = DEFAULT_SNAPSHOT_DIR,
        api_key: str | None = None,
    ) -> None:
        self._cache_ttl_seconds = cache_ttl_seconds
        self._transport = transport
        self._snapshot_dir = snapshot_dir
        configured_key = api_key if api_key is not None else os.getenv("FRED_API_KEY", "")
        configured_key = configured_key.strip()
        self._api_key = "" if configured_key == "your_key_here" else configured_key
        self._cache: dict[tuple[str, str], _CacheEntry] = {}

    async def fetch_series(
        self,
        series_id: str,
        observation_start: date = OBSERVATION_START,
    ) -> SeriesData:
        """Return clean observations using REST → public CSV → snapshot priority."""

        cache_key = (series_id, observation_start.isoformat())
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at > now:
            return cached.series_data

        observations: list[Observation] = []
        source: SourceMode = "snapshot"
        async with httpx.AsyncClient(
            timeout=15.0,
            transport=self._transport,
            follow_redirects=True,
        ) as client:
            if self._api_key:
                observations = await self._fetch_rest(
                    client, series_id, observation_start
                )
                if observations:
                    source = "rest_api"

            if not observations:
                observations = await self._fetch_public_csv(
                    client, series_id, observation_start
                )
                if observations:
                    source = "public_csv"

        if not observations:
            observations = self._load_snapshot(series_id)
            source = "snapshot"

        if not observations:
            raise FredAPIError(
                f"No usable FRED data or local snapshot is available for {series_id}."
            )

        series_data = SeriesData(
            observations=tuple(sorted(observations, key=lambda item: item.date)),
            source=source,
            loaded_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        self._cache[cache_key] = _CacheEntry(
            expires_at=now + self._cache_ttl_seconds,
            series_data=series_data,
        )
        return series_data

    def clear_cache(self) -> None:
        """Clear cached observations, primarily for tests and manual refreshes."""

        self._cache.clear()

    async def _fetch_rest(
        self,
        client: httpx.AsyncClient,
        series_id: str,
        observation_start: date,
    ) -> list[Observation]:
        """Try the authenticated FRED REST API without leaking key-bearing errors."""

        try:
            response = await client.get(
                FRED_REST_URL,
                params={
                    "series_id": series_id,
                    "api_key": self._api_key,
                    "file_type": "json",
                    "observation_start": observation_start.isoformat(),
                },
            )
            if response.is_error:
                return []
            return _parse_rest_json(response.json())
        except (httpx.RequestError, ValueError):
            return []

    async def _fetch_public_csv(
        self,
        client: httpx.AsyncClient,
        series_id: str,
        observation_start: date,
    ) -> list[Observation]:
        """Try FRED's keyless graph CSV download."""

        try:
            response = await client.get(
                FRED_CSV_URL,
                params={
                    "id": series_id,
                    "cosd": observation_start.isoformat(),
                },
            )
            if response.is_error:
                return []
            return _parse_csv(response.text, series_id)
        except httpx.RequestError:
            return []

    def _load_snapshot(self, series_id: str) -> list[Observation]:
        """Read a downloaded series when the live source is unavailable."""

        if self._snapshot_dir is None:
            return []
        snapshot = self._snapshot_dir / f"{series_id}.csv"
        if not snapshot.is_file():
            return []
        try:
            content = snapshot.read_text(encoding="utf-8")
        except OSError:
            return []
        return _parse_csv(content, series_id)


def _parse_csv(content: str, series_id: str) -> list[Observation]:
    """Parse FRED's public CSV format, ignoring missing or malformed values."""

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return []
    if "observation_date" not in reader.fieldnames or series_id not in reader.fieldnames:
        return []

    parsed: list[Observation] = []
    for row in reader:
        raw_value = row.get(series_id)
        if raw_value in (None, "", "."):
            continue
        try:
            observation_date = date.fromisoformat(row["observation_date"])
            value = float(raw_value)
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            parsed.append(Observation(date=observation_date, value=value))
    return parsed


def _parse_rest_json(payload: Any) -> list[Observation]:
    """Parse FRED REST observations, filtering its ``.`` missing marker."""

    if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
        return []

    parsed: list[Observation] = []
    for item in payload["observations"]:
        if not isinstance(item, dict) or item.get("value") in (None, "", "."):
            continue
        try:
            observation_date = date.fromisoformat(item["date"])
            value = float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            parsed.append(Observation(date=observation_date, value=value))
    return parsed
