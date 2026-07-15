"""Unit tests for frequency alignment and recovery-index mathematics."""

import unittest
from datetime import date

from app.fred_client import Observation
from app.recovery import (
    adjusted_score,
    build_indicator_payload,
    build_recovery_payload,
    calculate_trend,
    resample_monthly,
)


def observation(date_string: str, value: float) -> Observation:
    return Observation(date=date.fromisoformat(date_string), value=value)


def sample_series() -> dict[str, tuple[Observation, ...]]:
    return {
        "UNRATE": (
            observation("2020-01-01", 5.0),
            observation("2020-02-01", 4.0),
            observation("2020-03-01", 2.0),
        ),
        "INDPRO": (
            observation("2020-01-01", 95.0),
            observation("2020-02-01", 100.0),
            observation("2020-03-01", 110.0),
        ),
        "PCEC96": (
            observation("2020-01-01", 900.0),
            observation("2020-02-01", 1_000.0),
            observation("2020-03-01", 1_200.0),
        ),
        "ICSA": (
            observation("2020-02-08", 180.0),
            observation("2020-02-15", 220.0),
            observation("2020-03-07", 90.0),
            observation("2020-03-14", 110.0),
        ),
    }


class RecoveryMathTests(unittest.TestCase):
    def test_direction_adjustment_makes_improvement_increase_score(self) -> None:
        self.assertEqual(adjusted_score(120.0, 100.0, "higher"), 120.0)
        self.assertEqual(adjusted_score(50.0, 100.0, "lower"), 200.0)

    def test_weekly_values_are_averaged_by_calendar_month(self) -> None:
        monthly = resample_monthly(sample_series()["ICSA"])

        self.assertEqual(
            monthly,
            (
                observation("2020-02-01", 200.0),
                observation("2020-03-01", 100.0),
            ),
        )

    def test_composite_uses_common_months_and_equal_weights(self) -> None:
        payload = build_recovery_payload(sample_series())

        self.assertEqual(payload["current"], {"date": "2020-03-01", "value": 157.5})
        self.assertEqual(payload["series"][0]["value"], 100.0)
        contributions = {
            item["series_id"]: item for item in payload["contributions"]
        }
        self.assertEqual(contributions["ICSA"]["adjusted_score"], 200.0)
        self.assertEqual(contributions["ICSA"]["weighted_contribution"], 50.0)

    def test_indicator_payload_keeps_native_weekly_claims(self) -> None:
        payload = build_indicator_payload(sample_series())
        claims = next(
            item for item in payload["indicators"] if item["series_id"] == "ICSA"
        )

        self.assertEqual(len(claims["series"]), 4)
        self.assertEqual(claims["baseline"]["value"], 200.0)
        self.assertEqual(claims["baseline"]["basis"], "calendar-month average")
        self.assertEqual(claims["percent_of_baseline"], 55.0)

    def test_linear_trend_reports_direction_and_next_point(self) -> None:
        trend = calculate_trend([95.0, 96.0, 97.0, 98.0, 99.0, 100.0])

        self.assertEqual(trend["direction"], "improving")
        self.assertAlmostEqual(trend["slope_per_month"], 1.0)
        self.assertAlmostEqual(trend["projected_next_value"], 101.0)


if __name__ == "__main__":
    unittest.main()
