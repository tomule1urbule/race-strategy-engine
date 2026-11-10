"""Synthetic GPX generation with a known ground-truth profile (for tests, demos, sample data)."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from .utils import EARTH_RADIUS_M

# (length_m, grade_pct) pieces
ProfileSpec = Sequence[tuple[float, float]]


def make_gpx(
    profile: ProfileSpec,
    *,
    start_lat: float = 54.8985,
    start_lon: float = 23.9036,
    bearing_deg: float = 90.0,
    spacing_m: float = 8.0,
    ele_noise_m: float = 0.0,
    start_ele_m: float = 50.0,
    seed: int = 7,
    name: str = "Synthetic course",
) -> str:
    """Build a straight-line GPX following ``profile`` (piecewise-constant grades)."""
    rng = np.random.default_rng(seed)
    total = sum(length for length, _ in profile)
    n = int(total // spacing_m) + 1
    dist = np.linspace(0.0, total, n)

    bounds = np.cumsum([0.0] + [length for length, _ in profile])
    ele_at_bounds = [start_ele_m]
    for length, grade in profile:
        ele_at_bounds.append(ele_at_bounds[-1] + length * grade / 100.0)
    ele = np.interp(dist, bounds, ele_at_bounds)
    if ele_noise_m:
        ele = ele + rng.normal(0.0, ele_noise_m, size=n)

    lat0, lon0, brg = map(math.radians, (start_lat, start_lon, bearing_deg))
    delta = dist / EARTH_RADIUS_M
    lat = np.arcsin(np.sin(lat0) * np.cos(delta) + np.cos(lat0) * np.sin(delta) * math.cos(brg))
    lon = lon0 + np.arctan2(math.sin(brg) * np.sin(delta) * np.cos(lat0), np.cos(delta) - np.sin(lat0) * np.sin(lat))

    pts = "\n".join(
        f'      <trkpt lat="{math.degrees(a):.7f}" lon="{math.degrees(o):.7f}"><ele>{e:.2f}</ele></trkpt>'
        for a, o, e in zip(lat, lon, ele, strict=True)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="race-strategy-engine" xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"  <trk><name>{name}</name><trkseg>\n{pts}\n  </trkseg></trk>\n</gpx>\n"
    )
