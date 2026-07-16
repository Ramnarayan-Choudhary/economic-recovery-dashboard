"""Tests for keyless FRED CSV parsing, fallback, and TTL caching."""

import tempfile
import unittest
from pathlib import Path

import httpx

from app.fred_client import FredAPIError, FredClient


class FredClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_missing_values_and_caches_live_csv(self) -> None:
        request_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            self.assertEqual(request.url.params["id"], "UNRATE")
            self.assertEqual(request.url.params["cosd"], "2018-01-01")
            return httpx.Response(
                200,
                text=(
                    "observation_date,UNRATE\n"
                    "2020-01-01,3.5\n"
                    "2020-02-01,.\n"
                    "bad-date,4.0\n"
                    "2020-03-01,nan\n"
                ),
            )

        client = FredClient(
            transport=httpx.MockTransport(handler),
            snapshot_dir=None,
        )

        first = await client.fetch_series("UNRATE")
        second = await client.fetch_series("UNRATE")

        self.assertEqual(len(first.observations), 1)
        self.assertEqual(first.observations[0].value, 3.5)
        self.assertEqual(first.source, "live")
        self.assertIs(first, second)
        self.assertEqual(request_count, 1)

    async def test_uses_downloaded_snapshot_when_fred_is_unavailable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="temporarily unavailable")

        with tempfile.TemporaryDirectory() as directory:
            snapshot_dir = Path(directory)
            (snapshot_dir / "UNRATE.csv").write_text(
                "observation_date,UNRATE\n2020-02-01,3.5\n",
                encoding="utf-8",
            )
            client = FredClient(
                transport=httpx.MockTransport(handler),
                snapshot_dir=snapshot_dir,
            )

            result = await client.fetch_series("UNRATE")

        self.assertEqual(len(result.observations), 1)
        self.assertEqual(result.observations[0].value, 3.5)
        self.assertEqual(result.source, "snapshot")

    async def test_reports_error_when_live_and_snapshot_sources_fail(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="temporarily unavailable")

        client = FredClient(
            transport=httpx.MockTransport(handler),
            snapshot_dir=None,
        )

        with self.assertRaisesRegex(FredAPIError, "HTTP 503"):
            await client.fetch_series("UNRATE")


if __name__ == "__main__":
    unittest.main()
