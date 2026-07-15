"""HTTP-level tests for the dashboard shell and API contracts."""

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.fred_client import FredAPIError
from app.main import app
from tests.test_recovery import sample_series


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
        self.assertEqual(health.json(), {"status": "ok"})

    async def test_data_endpoints_return_expected_contracts(self) -> None:
        with patch(
            "app.main._fetch_all_series",
            new=AsyncMock(return_value=sample_series()),
        ):
            indicators = await self.client.get("/api/indicators")
            recovery = await self.client.get("/api/recovery-index")

        self.assertEqual(indicators.status_code, 200)
        self.assertEqual(len(indicators.json()["indicators"]), 4)
        self.assertEqual(recovery.status_code, 200)
        self.assertEqual(recovery.json()["current"]["value"], 157.5)
        self.assertEqual(len(recovery.json()["contributions"]), 4)

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
