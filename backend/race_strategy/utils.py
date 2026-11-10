"""Small shared helpers: geodesy, formatting, thermal models."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(
    lat1: NDArray[np.float64],
    lon1: NDArray[np.float64],
    lat2: NDArray[np.float64],
    lon2: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Vectorised great-circle distance in metres."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return np.asarray(2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0))), dtype=np.float64)


def bearing_rad(
    lat1: NDArray[np.float64],
    lon1: NDArray[np.float64],
    lat2: NDArray[np.float64],
    lon2: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Initial bearing (radians, clockwise from north)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dlmb = np.radians(lon2 - lon1)
    x = np.sin(dlmb) * np.cos(p2)
    y = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dlmb)
    return np.asarray(np.arctan2(x, y), dtype=np.float64)


def format_duration(seconds: float) -> str:
    """58:00 under an hour, 1:02:03 above."""
    s = round(max(seconds, 0.0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_pace(sec_per_km: float) -> str:
    s = round(sec_per_km)
    return f"{s // 60}:{s % 60:02d}/km"


def apparent_temperature_c(temp_c: float, humidity_pct: float, wind_kmh: float) -> float:
    """Steadman (1994) apparent temperature for shade, combining heat, humidity and wind."""
    vapour_pressure_hpa = (humidity_pct / 100.0) * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))
    wind_ms = wind_kmh / 3.6
    return temp_c + 0.33 * vapour_pressure_hpa - 0.70 * wind_ms - 4.00


def air_density(temp_c: float, elevation_m: float = 0.0) -> float:
    """Dry-air density from barometric formula (ISA) and temperature."""
    pressure = 101_325.0 * (1 - 2.25577e-5 * elevation_m) ** 5.25588
    return float(pressure / (287.05 * (temp_c + 273.15)))
