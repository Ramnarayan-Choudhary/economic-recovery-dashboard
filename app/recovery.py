"""Recovery-index normalization, alignment, aggregation, and trend logic."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import fmean
from typing import Iterable, Mapping, Sequence

import numpy as np

from app.fred_client import Observation
from app.indicators import BASELINE_DATE, INDICATORS, IndicatorConfig


class RecoveryCalculationError(ValueError):
    """Raised when the fetched observations cannot produce a valid index."""


def month_start(value: date) -> date:
    """Return the first day of an observation's calendar month."""

    return value.replace(day=1)


def resample_monthly(
    observations: Iterable[Observation],
) -> tuple[Observation, ...]:
    """Aggregate observations to calendar-month averages.

    Monthly FRED series remain unchanged (one value per group), while weekly ICSA
    becomes the arithmetic mean of all weekly observations dated in each month.
    """

    grouped: dict[date, list[float]] = defaultdict(list)
    for observation in observations:
        grouped[month_start(observation.date)].append(observation.value)
    return tuple(
        Observation(date=period, value=fmean(values))
        for period, values in sorted(grouped.items())
    )


def nearest_observation(
    observations: Sequence[Observation], target: date
) -> Observation:
    """Select the available observation closest to a requested baseline date."""

    if not observations:
        raise RecoveryCalculationError("Cannot select a baseline from an empty series.")
    return min(
        observations,
        key=lambda observation: (
            abs((observation.date - target).days),
            observation.date,
        ),
    )


def raw_baseline_percentage(current: float, baseline: float) -> float:
    """Express an observation as a raw percentage of its baseline value."""

    if baseline == 0:
        raise RecoveryCalculationError("A baseline value cannot be zero.")
    return current / baseline * 100.0


def adjusted_score(
    current: float,
    baseline: float,
    direction: str,
) -> float:
    """Normalize direction so a healthier outcome always raises the score.

    Higher-is-better indicators use ``current / baseline * 100``. Lower-is-better
    indicators use the reciprocal ``baseline / current * 100``. Both equal 100 at
    the February 2020 baseline.
    """

    if baseline == 0 or (direction == "lower" and current == 0):
        raise RecoveryCalculationError(
            "Current and baseline values must be non-zero for normalization."
        )
    if direction == "higher":
        return current / baseline * 100.0
    if direction == "lower":
        return baseline / current * 100.0
    raise RecoveryCalculationError(f"Unsupported indicator direction: {direction}")


def build_indicator_payload(
    series_by_id: Mapping[str, Sequence[Observation]],
) -> dict[str, object]:
    """Build the indicator API payload while keeping ICSA's native weekly chart."""

    indicators: list[dict[str, object]] = []
    for config in INDICATORS:
        native = tuple(sorted(series_by_id.get(config.series_id, ()), key=lambda x: x.date))
        if not native:
            raise RecoveryCalculationError(
                f"No observations are available for {config.series_id}."
            )
        monthly = resample_monthly(native)
        baseline = nearest_observation(monthly, BASELINE_DATE)
        latest = native[-1]
        indicators.append(
            {
                "series_id": config.series_id,
                "label": config.label,
                "category": config.category,
                "direction": config.direction,
                "frequency": config.frequency,
                "unit": config.unit,
                "color": config.color,
                "latest": _observation_payload(latest),
                "baseline": {
                    **_observation_payload(baseline),
                    "basis": "calendar-month average"
                    if config.frequency == "weekly"
                    else "monthly observation",
                },
                "percent_of_baseline": _rounded(
                    raw_baseline_percentage(latest.value, baseline.value)
                ),
                "series": [_observation_payload(item) for item in native],
            }
        )
    return {
        "country": "United States",
        "baseline_period": BASELINE_DATE.isoformat(),
        "indicators": indicators,
    }


def build_recovery_payload(
    series_by_id: Mapping[str, Sequence[Observation]],
) -> dict[str, object]:
    """Build a common-month, baseline-relative weighted recovery index."""

    monthly_by_id = {
        config.series_id: resample_monthly(
            series_by_id.get(config.series_id, ())
        )
        for config in INDICATORS
    }
    for series_id, observations in monthly_by_id.items():
        if not observations:
            raise RecoveryCalculationError(
                f"No monthly observations are available for {series_id}."
            )

    values_by_id = {
        series_id: {item.date: item.value for item in observations}
        for series_id, observations in monthly_by_id.items()
    }
    baselines = {
        config.series_id: nearest_observation(
            monthly_by_id[config.series_id], BASELINE_DATE
        )
        for config in INDICATORS
    }

    common_dates = set.intersection(
        *(set(values.keys()) for values in values_by_id.values())
    )
    recovery_dates = sorted(
        period for period in common_dates if period >= BASELINE_DATE
    )
    if not recovery_dates:
        raise RecoveryCalculationError(
            "The indicators do not share any monthly observations after the baseline."
        )

    total_weight = sum(config.weight for config in INDICATORS)
    if total_weight <= 0:
        raise RecoveryCalculationError("Indicator weights must sum to a positive value.")

    series: list[dict[str, object]] = []
    for period in recovery_dates:
        score = sum(
            adjusted_score(
                values_by_id[config.series_id][period],
                baselines[config.series_id].value,
                config.direction,
            )
            * config.weight
            for config in INDICATORS
        ) / total_weight
        series.append({"date": period.isoformat(), "value": _rounded(score)})

    latest_period = recovery_dates[-1]
    contributions = [
        _contribution_payload(
            config,
            values_by_id[config.series_id][latest_period],
            baselines[config.series_id],
            total_weight,
        )
        for config in INDICATORS
    ]

    return {
        "country": "United States",
        "baseline_period": BASELINE_DATE.isoformat(),
        "current": series[-1],
        "series": series,
        "contributions": contributions,
        "trend": calculate_trend([float(point["value"]) for point in series]),
        "methodology": {
            "higher_is_better": "current / baseline * 100",
            "lower_is_better": "baseline / current * 100",
            "aggregation": "weighted arithmetic mean on common monthly periods",
            "weekly_alignment": "ICSA calendar-month average",
        },
    }


def calculate_trend(values: Sequence[float], window: int = 6) -> dict[str, object]:
    """Fit a small linear trend and extrapolate one month, not a forecast."""

    recent = list(values[-window:])
    if len(recent) < 2:
        return {
            "direction": "unavailable",
            "window_months": len(recent),
            "slope_per_month": None,
            "projected_next_value": None,
            "disclaimer": "Insufficient history; this is not a forecast.",
        }

    x_values = np.arange(len(recent), dtype=float)
    slope, intercept = np.polyfit(x_values, np.asarray(recent), 1)
    if abs(slope) < 0.01:
        direction = "stable"
    else:
        direction = "improving" if slope > 0 else "declining"
    projected = intercept + slope * len(recent)
    return {
        "direction": direction,
        "window_months": len(recent),
        "slope_per_month": _rounded(float(slope)),
        "projected_next_value": _rounded(float(projected)),
        "disclaimer": "Naive linear extrapolation of the last six points; not a forecast.",
    }


def _contribution_payload(
    config: IndicatorConfig,
    current: float,
    baseline: Observation,
    total_weight: float,
) -> dict[str, object]:
    score = adjusted_score(current, baseline.value, config.direction)
    normalized_weight = config.weight / total_weight
    return {
        "series_id": config.series_id,
        "label": config.label,
        "color": config.color,
        "direction": config.direction,
        "weight": _rounded(normalized_weight),
        "current_month_value": _rounded(current),
        "baseline_value": _rounded(baseline.value),
        "adjusted_score": _rounded(score),
        "weighted_contribution": _rounded(score * normalized_weight),
    }


def _observation_payload(observation: Observation) -> dict[str, object]:
    return {
        "date": observation.date.isoformat(),
        "value": _rounded(observation.value),
    }


def _rounded(value: float) -> float:
    return round(value, 3)
