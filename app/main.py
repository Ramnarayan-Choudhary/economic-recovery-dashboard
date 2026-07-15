"""FastAPI entry point and dashboard/API routes."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.fred_client import FredClient, FredClientError, Observation
from app.indicators import INDICATORS
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

    return build_indicator_payload(await _fetch_all_series())


@app.get("/api/recovery-index")
async def recovery_index() -> dict[str, object]:
    """Return the common-month composite, contributions, and naive trend."""

    return build_recovery_payload(await _fetch_all_series())


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness endpoint independent of FRED availability."""

    return {"status": "ok"}


async def _fetch_all_series() -> dict[str, tuple[Observation, ...]]:
    """Fetch independent FRED series concurrently; cache absorbs repeat loads."""

    observations = await asyncio.gather(
        *(fred_client.fetch_series(config.series_id) for config in INDICATORS)
    )
    return {
        config.series_id: series
        for config, series in zip(INDICATORS, observations, strict=True)
    }
