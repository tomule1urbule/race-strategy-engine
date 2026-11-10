from __future__ import annotations

from datetime import datetime

import pytest
from race_strategy import AthleteProfile, GPXAnalyzer, Sport, WeatherConditions
from race_strategy.synthetic import make_gpx

# Half-marathon with a known 2 km climb at +5% and 2 km descent at -5%.
HILLY_HALF = [(5000.0, 0.0), (2000.0, 5.0), (2000.0, -5.0), (12097.5, 0.3)]


@pytest.fixture
def hilly_half_gpx() -> str:
    return make_gpx(HILLY_HALF)


@pytest.fixture
def noisy_hilly_half_gpx() -> str:
    return make_gpx(HILLY_HALF, ele_noise_m=2.0, seed=11)


@pytest.fixture
def hilly_half_segments(hilly_half_gpx: str):  # type: ignore[no-untyped-def]
    return GPXAnalyzer().analyze(hilly_half_gpx).segments


@pytest.fixture
def runner() -> AthleteProfile:
    return AthleteProfile(weight_kg=72, threshold_pace_sec_km=270, max_hr=190, threshold_hr=172)


@pytest.fixture
def cyclist() -> AthleteProfile:
    return AthleteProfile(sport=Sport.BIKE, weight_kg=75, threshold_power_watts=280, max_hr=185, threshold_hr=165)


@pytest.fixture
def mild_weather() -> WeatherConditions:
    return WeatherConditions(
        temperature_c=12, humidity_pct=60, wind_speed_kmh=0, start_time=datetime(2026, 11, 15, 8, 0)
    )


@pytest.fixture
def hot_weather() -> WeatherConditions:
    return WeatherConditions(
        temperature_c=28,
        temperature_end_c=32,
        humidity_pct=70,
        wind_speed_kmh=5,
        start_time=datetime(2026, 11, 15, 8, 0),
    )
