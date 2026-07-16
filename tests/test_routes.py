"""HTTP-level tests for the dashboard shell and API contracts."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.fred_client import FredAPIError, SeriesData
from app.indicators import INDICATORS
from app.main import _fetch_all_series, app
from tests.test_recovery import sample_series

SAMPLE_DATA_STATUS = {
    "mode": "public_csv",
    "loaded_at_utc": "2026-07-16T08:00:00Z",
    "cache_ttl_hours": 12,
    "series_sources": {
        "UNRATE": "public_csv",
        "INDPRO": "public_csv",
        "PCEC96": "public_csv",
        "ICSA": "public_csv",
    },
}


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_dashboard_and_health_are_available_without_fred(self) -> None:
        dashboard = await self.client.get("/")
        health = await self.client.get("/health")

        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Economic Recovery Dashboard", dashboard.text)
        self.assertIn("Data connection", dashboard.text)
        self.assertIn('label: "Feb 2020 baseline"', dashboard.text)
        self.assertEqual(health.json(), {"status": "ok"})

    async def test_data_endpoints_return_expected_contracts(self) -> None:
        with patch(
            "app.main._fetch_all_series",
            new=AsyncMock(return_value=(sample_series(), SAMPLE_DATA_STATUS)),
        ):
            indicators = await self.client.get("/api/indicators")
            recovery = await self.client.get("/api/recovery-index")

        self.assertEqual(indicators.status_code, 200)
        self.assertEqual(len(indicators.json()["indicators"]), 4)
        self.assertEqual(indicators.json()["data_status"]["mode"], "public_csv")
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(recovery.json()["current"]["value"], 157.5)
        self.assertEqual(len(recovery.json()["contributions"]), 4)
        self.assertEqual(recovery.json()["data_status"]["cache_ttl_hours"], 12)

    async def test_fetch_all_series_reports_mixed_provenance(self) -> None:
        samples = sample_series()
        results = [
            SeriesData(
                observations=samples[config.series_id],
                source="snapshot" if config.series_id == "ICSA" else "rest_api",
                loaded_at_utc=f"2026-07-16T08:00:0{index}Z",
            )
            for index, config in enumerate(INDICATORS)
        ]

        with patch(
            "app.main.fred_client.fetch_series",
            new=AsyncMock(side_effect=results),
        ):
            series, status = await _fetch_all_series()

        self.assertEqual(set(series), {config.series_id for config in INDICATORS})
        self.assertEqual(status["mode"], "mixed")
        self.assertEqual(status["series_sources"]["ICSA"], "snapshot")
        self.assertEqual(status["loaded_at_utc"], "2026-07-16T08:00:03Z")

    async def test_source_failure_returns_clear_service_error(self) -> None:
        with patch(
            "app.main._fetch_all_series",
            new=AsyncMock(
                side_effect=FredAPIError("FRED data is temporarily unavailable.")
            ),
        ):
            response = await self.client.get("/api/indicators")

        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
