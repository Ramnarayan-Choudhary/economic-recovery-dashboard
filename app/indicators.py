"""Static indicator and recovery-index configuration."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

Direction = Literal["higher", "lower"]
Frequency = Literal["monthly", "weekly"]

BASELINE_DATE = date(2020, 2, 1)
OBSERVATION_START = date(2018, 1, 1)
CACHE_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class IndicatorConfig:
    """Metadata needed to fetch, display, and normalize one FRED series."""

    series_id: str
    label: str
    category: str
    direction: Direction
    frequency: Frequency
    weight: float
    unit: str
    color: str


INDICATORS: tuple[IndicatorConfig, ...] = (
    IndicatorConfig(
        series_id="UNRATE",
        label="Unemployment Rate",
        category="Labour market",
        direction="lower",
        frequency="monthly",
        weight=0.25,
        unit="percent",
        color="#c2415d",
    ),
    IndicatorConfig(
        series_id="INDPRO",
        label="Industrial Production",
        category="Economic output",
        direction="higher",
        frequency="monthly",
        weight=0.25,
        unit="index",
        color="#3566c8",
    ),
    IndicatorConfig(
        series_id="PCEC96",
        label="Real Personal Consumption",
        category="Consumer activity",
        direction="higher",
        frequency="monthly",
        weight=0.25,
        unit="billions of chained dollars",
        color="#13866f",
    ),
    IndicatorConfig(
        series_id="ICSA",
        label="Initial Jobless Claims",
        category="High-frequency labour signal",
        direction="lower",
        frequency="weekly",
        weight=0.25,
        unit="claims",
        color="#d07a1d",
    ),
)

INDICATORS_BY_ID = {indicator.series_id: indicator for indicator in INDICATORS}
