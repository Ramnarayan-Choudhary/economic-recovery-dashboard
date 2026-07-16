"""FastAPI entry point and dashboard/API routes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.fred_client import FredClient, FredClientError, Observation, SeriesData
from app.indicators import CACHE_TTL_SECONDS, INDICATORS
from app.recovery import (
    RecoveryCalculationError,
    build_indicator_payload,
    build_recovery_payload,
)

app = FastAPI(
    title="US Economic Recovery Dashboard",
    description="Baseline-relative recovery signals from FRED.",
    version="0.1.0",
)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
fred_client = FredClient()


@app.exception_handler(FredClientError)
async def fred_error_handler(
    _request: Request, exc: FredClientError
) -> JSONResponse:
    """Return upstream and local-data failures as useful JSON."""

    return JSONResponse(status_code=503, content={"error": str(exc)})


@app.exception_handler(RecoveryCalculationError)
async def recovery_error_handler(
    _request: Request, exc: RecoveryCalculationError
) -> JSONResponse:
    """Report incomplete or incompatible source data without crashing."""

    return JSONResponse(status_code=503, content={"error": str(exc)})


@app.get("/", include_in_schema=False)
async def dashboard(request: Request):
    """Render the shell; browser JavaScript loads current JSON data."""

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"indicator_count": len(INDICATORS)},
    )


@app.get("/api/indicators")
async def indicators() -> dict[str, object]:
    """Return latest, baseline, raw ratio, and native chart series."""

    series, data_status = await _fetch_all_series()
    return {**build_indicator_payload(series), "data_status": data_status}


@app.get("/api/recovery-index")
async def recovery_index() -> dict[str, object]:
    """Return the common-month composite, contributions, and naive trend."""

    series, data_status = await _fetch_all_series()
    return {**build_recovery_payload(series), "data_status": data_status}


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness endpoint independent of FRED availability."""

    return {"status": "ok"}


async def _fetch_all_series() -> tuple[
    dict[str, tuple[Observation, ...]],
    dict[str, object],
]:
    """Fetch series concurrently and expose their source provenance."""

    results: list[SeriesData] = await asyncio.gather(
        *(fred_client.fetch_series(config.series_id) for config in INDICATORS)
    )
    series = {
        config.series_id: result.observations
        for config, result in zip(INDICATORS, results, strict=True)
    }
    source_by_series = {
        config.series_id: result.source
        for config, result in zip(INDICATORS, results, strict=True)
    }
    sources = set(source_by_series.values())
    mode = next(iter(sources)) if len(sources) == 1 else "mixed"
    data_status: dict[str, object] = {
        "mode": mode,
        "loaded_at_utc": max(result.loaded_at_utc for result in results),
        "cache_ttl_hours": CACHE_TTL_SECONDS // 3600,
        "series_sources": source_by_series,
    }
    return series, data_status
