"""Keyless FRED CSV client with process-local caching and local fallback."""

from __future__ import annotations

import csv
import io
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import httpx

from app.indicators import CACHE_TTL_SECONDS, OBSERVATION_START

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
DEFAULT_SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "data"
SourceMode = Literal["live", "snapshot"]


class FredClientError(RuntimeError):
    """Base exception for transport and source-data failures."""


class FredAPIError(FredClientError):
    """Raised when neither live FRED nor a local snapshot is usable."""


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
    """Fetch public FRED CSV observations and cache them for a bounded period.

    Live downloads require no account or API key. The cache removes repeated external
    calls during normal use; checked-in CSV snapshots keep the local prototype useful
    during a temporary FRED/network outage without adding a database dependency.
    """

    def __init__(
        self,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        snapshot_dir: Path | None = DEFAULT_SNAPSHOT_DIR,
    ) -> None:
        self._cache_ttl_seconds = cache_ttl_seconds
        self._transport = transport
        self._snapshot_dir = snapshot_dir
        self._cache: dict[tuple[str, str], _CacheEntry] = {}

    async def fetch_series(
        self,
        series_id: str,
        observation_start: date = OBSERVATION_START,
    ) -> SeriesData:
        """Return clean observations, preferring live FRED over a local snapshot."""

        cache_key = (series_id, observation_start.isoformat())
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at > now:
            return cached.series_data

        params = {
            "id": series_id,
            "cosd": observation_start.isoformat(),
        }

        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                transport=self._transport,
                follow_redirects=True,
            ) as client:
                response = await client.get(FRED_CSV_URL, params=params)
        except httpx.RequestError as exc:
            observations = self._load_snapshot(series_id)
            source: SourceMode = "snapshot"
            if not observations:
                raise FredAPIError(
                    f"Unable to reach FRED and no local snapshot is available for {series_id}."
                ) from exc
        else:
            if response.is_error:
                observations = self._load_snapshot(series_id)
                source = "snapshot"
                if not observations:
                    raise FredAPIError(
                        f"FRED returned HTTP {response.status_code} for {series_id}, "
                        "and no local snapshot is available."
                    )
            else:
                observations = _parse_csv(response.text, series_id)
                source = "live"
                if not observations:
                    observations = self._load_snapshot(series_id)
                    source = "snapshot"

        if not observations:
            raise FredAPIError(
                f"FRED and the local snapshot returned no usable observations for {series_id}."
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
