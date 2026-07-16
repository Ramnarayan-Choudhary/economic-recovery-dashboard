"""Tests for FRED REST/CSV priority, fallback, parsing, and TTL caching."""

import tempfile
import unittest
from pathlib import Path

import httpx

from app.fred_client import FredAPIError, FredClient


class FredClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_rest_api_when_key_is_configured(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/fred/series/observations")
            self.assertEqual(request.url.params["series_id"], "UNRATE")
            self.assertEqual(request.url.params["api_key"], "test-key")
            self.assertEqual(request.url.params["file_type"], "json")
            self.assertEqual(request.url.params["observation_start"], "2018-01-01")
            return httpx.Response(
                200,
                json={
                    "observations": [
                        {"date": "2020-01-01", "value": "3.5"},
                        {"date": "2020-02-01", "value": "."},
                        {"date": "bad-date", "value": "4.0"},
                        {"date": "2020-03-01", "value": "nan"},
                    ]
                },
            )

        client = FredClient(
            api_key="test-key",
            transport=httpx.MockTransport(handler),
            snapshot_dir=None,
        )

        result = await client.fetch_series("UNRATE")

        self.assertEqual(len(result.observations), 1)
        self.assertEqual(result.observations[0].value, 3.5)
        self.assertEqual(result.source, "rest_api")

    async def test_filters_missing_values_and_caches_public_csv(self) -> None:
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
            api_key="your_key_here",
            transport=httpx.MockTransport(handler),
            snapshot_dir=None,
        )

        first = await client.fetch_series("UNRATE")
        second = await client.fetch_series("UNRATE")

        self.assertEqual(len(first.observations), 1)
        self.assertEqual(first.observations[0].value, 3.5)
        self.assertEqual(first.source, "public_csv")
        self.assertIs(first, second)
        self.assertEqual(request_count, 1)

    async def test_falls_back_to_public_csv_when_rest_key_is_rejected(self) -> None:
        requested_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested_paths.append(request.url.path)
            if request.url.path == "/fred/series/observations":
                return httpx.Response(403, json={"error_message": "Invalid key"})
            return httpx.Response(
                200,
                text="observation_date,UNRATE\n2020-02-01,3.5\n",
            )

        client = FredClient(
            api_key="rejected-key",
            transport=httpx.MockTransport(handler),
            snapshot_dir=None,
        )

        result = await client.fetch_series("UNRATE")

        self.assertEqual(
            requested_paths,
            ["/fred/series/observations", "/graph/fredgraph.csv"],
        )
        self.assertEqual(result.source, "public_csv")

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
                api_key="",
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
            api_key="",
            transport=httpx.MockTransport(handler),
            snapshot_dir=None,
        )

        with self.assertRaisesRegex(FredAPIError, "No usable FRED data"):
            await client.fetch_series("UNRATE")

    async def test_error_does_not_expose_api_key(self) -> None:
        secret = "do-not-leak-this-key"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text=f"failed request containing {secret}")

        client = FredClient(
            api_key=secret,
            transport=httpx.MockTransport(handler),
            snapshot_dir=None,
        )

        with self.assertRaises(FredAPIError) as raised:
            await client.fetch_series("UNRATE")

        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
