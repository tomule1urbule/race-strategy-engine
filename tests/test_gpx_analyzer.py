from __future__ import annotations

import io
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest
from race_strategy import AnalyzerConfig, GPXAnalyzer, GPXParseError, TerrainType
from race_strategy import gpx_analyzer as ga
from race_strategy.gpx_analyzer import gain_loss_with_hysteresis
from race_strategy.synthetic import make_gpx

from .conftest import HILLY_HALF

TRUE_DISTANCE = sum(length for length, _ in HILLY_HALF)


# ------------------------------------------------------------------ parsing
def test_distance_accuracy(hilly_half_gpx: str) -> None:
    raw = GPXAnalyzer().load(hilly_half_gpx)
    assert raw["distance_m"].iloc[-1] == pytest.approx(TRUE_DISTANCE, rel=1e-3)
    assert list(raw.columns) == ["lat", "lon", "ele", "distance_m"]
    assert raw["distance_m"].is_monotonic_increasing


def test_all_source_types_agree(hilly_half_gpx: str, tmp_path: Path) -> None:
    path = tmp_path / "course.gpx"
    path.write_text(hilly_half_gpx, encoding="utf-8")
    sources = [
        hilly_half_gpx,
        path,
        str(path),
        hilly_half_gpx.encode(),
        io.BytesIO(hilly_half_gpx.encode()),
        io.StringIO(hilly_half_gpx),
    ]
    totals = {round(GPXAnalyzer().load(s)["distance_m"].iloc[-1], 3) for s in sources}  # type: ignore[arg-type]
    assert len(totals) == 1


def test_route_points_supported() -> None:
    gpx = make_gpx([(1000, 0)]).replace("trkpt", "rtept").replace("<trk>", "<rte>")
    gpx = gpx.replace("</trk>", "</rte>").replace("<trkseg>", "").replace("</trkseg>", "")
    raw = GPXAnalyzer().load(gpx)
    assert raw["distance_m"].iloc[-1] == pytest.approx(1000, rel=1e-3)


def test_partial_elevation_is_interpolated() -> None:
    gpx = make_gpx([(1000, 2.0)], spacing_m=100)
    lines = gpx.splitlines()
    idx = next(i for i, ln in enumerate(lines) if "<trkpt" in ln) + 3
    lines[idx] = lines[idx].split("<ele>")[0] + "</trkpt>"
    raw = GPXAnalyzer().load("\n".join(lines))
    assert not raw["ele"].isna().any()


@pytest.mark.parametrize(
    "payload,match",
    [
        ("<gpx><trk><trkseg>", "Invalid GPX"),
        ('<gpx xmlns="http://www.topografix.com/GPX/1/1"></gpx>', "at least two"),
        (
            '<gpx><trk><trkseg><trkpt lat="54.9" lon="23.9"/><trkpt lat="54.91" lon="23.9"/></trkseg></trk></gpx>',
            "no elevation",
        ),
    ],
)
def test_parse_errors(payload: str, match: str) -> None:
    with pytest.raises(GPXParseError, match=match):
        GPXAnalyzer().load(payload)


@pytest.mark.skipif(not ga.HAS_GPXPY, reason="gpxpy not installed")
def test_gpxpy_and_stdlib_parsers_agree(hilly_half_gpx: str) -> None:
    a = GPXAnalyzer._parse_with_gpxpy(hilly_half_gpx)
    b = GPXAnalyzer._parse_with_stdlib(hilly_half_gpx)
    assert np.allclose(np.array(a, dtype=float), np.array(b, dtype=float))


# ------------------------------------------------------------------ grades & segmentation
def test_known_profile_segmentation(hilly_half_gpx: str) -> None:
    analysis = GPXAnalyzer().analyze(hilly_half_gpx)
    segs = analysis.segments
    assert [s.terrain for s in segs] == [
        TerrainType.FLAT,
        TerrainType.ASCENT,
        TerrainType.DESCENT,
        TerrainType.FLAT,
    ]
    # boundaries within one smoothing window of truth
    for seg, true_start in zip(segs[1:], [5000, 7000, 9000], strict=True):
        assert abs(seg.start_distance_m - true_start) <= 100
    assert segs[1].avg_grade_pct == pytest.approx(5.0, abs=0.3)
    assert segs[2].avg_grade_pct == pytest.approx(-5.0, abs=0.3)
    assert segs[1].elevation_delta_m == pytest.approx(100, abs=5)


def test_segments_contiguous_and_complete(noisy_hilly_half_gpx: str) -> None:
    segs = GPXAnalyzer().analyze(noisy_hilly_half_gpx).segments
    assert segs[0].start_distance_m == 0
    assert segs[-1].end_distance_m == pytest.approx(TRUE_DISTANCE, rel=1e-3)
    for prev, nxt in pairwise(segs):
        assert prev.end_distance_m == nxt.start_distance_m
        assert prev.terrain != nxt.terrain  # adjacent same-type blocks are coalesced
    assert [s.index for s in segs] == list(range(len(segs)))
    for s in segs:
        assert s.elevation_gain_m - s.elevation_loss_m == pytest.approx(s.elevation_delta_m, abs=0.05)


def test_noise_is_smoothed(noisy_hilly_half_gpx: str) -> None:
    analysis = GPXAnalyzer().analyze(noisy_hilly_half_gpx)
    assert [s.terrain for s in analysis.segments] == [
        TerrainType.FLAT,
        TerrainType.ASCENT,
        TerrainType.DESCENT,
        TerrainType.FLAT,
    ]
    true_gain = 100 + 12097.5 * 0.003
    assert analysis.summary.elevation_gain_m == pytest.approx(true_gain, rel=0.08)
    # smoothed grades are far less noisy than raw point-to-point grades
    raw = analysis.raw_points
    raw_grade = np.diff(raw["ele"]) / np.diff(raw["distance_m"]) * 100
    raw_flat = raw_grade[(raw["distance_m"].iloc[1:] > 500) & (raw["distance_m"].iloc[1:] < 4500)]
    flat = analysis.profile[analysis.profile["distance_m"].between(500, 4500)]["grade_pct"]
    assert flat.std() < 0.05 * raw_flat.std()
    assert abs(flat.mean()) < 0.3


@pytest.mark.parametrize(
    "grade,expected",
    [
        (0.0, TerrainType.FLAT),
        (1.5, TerrainType.FLAT),
        (-1.5, TerrainType.FLAT),
        (1.51, TerrainType.ASCENT),
        (-1.51, TerrainType.DESCENT),
    ],
)
def test_threshold_boundaries(grade: float, expected: TerrainType) -> None:
    assert GPXAnalyzer().classify_grade(grade) is expected


@pytest.mark.parametrize(
    "grade,expected",
    [(1.2, TerrainType.FLAT), (2.0, TerrainType.ASCENT), (-2.0, TerrainType.DESCENT)],
)
def test_uniform_grade_course(grade: float, expected: TerrainType) -> None:
    segs = GPXAnalyzer().analyze(make_gpx([(3000, grade)])).segments
    assert len(segs) == 1
    assert segs[0].terrain is expected
    assert segs[0].avg_grade_pct == pytest.approx(grade, abs=0.05)


def test_short_blips_are_absorbed() -> None:
    gpx = make_gpx([(2000, 0), (80, 6), (2000, 0)])
    segs = GPXAnalyzer(AnalyzerConfig(min_segment_m=200)).analyze(gpx).segments
    assert all(s.distance_m >= 200 for s in segs)
    assert TerrainType.ASCENT not in {s.terrain for s in segs}


def test_min_segment_zero_keeps_detail() -> None:
    gpx = make_gpx([(2000, 0), (400, 6), (2000, 0)])
    segs = GPXAnalyzer(AnalyzerConfig(min_segment_m=0, smoothing_window_m=30)).analyze(gpx).segments
    assert TerrainType.ASCENT in {s.terrain for s in segs}


def test_heading_components() -> None:
    east = GPXAnalyzer().analyze(make_gpx([(2000, 0)], bearing_deg=90)).segments[0]
    north = GPXAnalyzer().analyze(make_gpx([(2000, 0)], bearing_deg=0)).segments[0]
    assert east.heading_east == pytest.approx(1, abs=0.01)
    assert north.heading_north == pytest.approx(1, abs=0.01)


def test_gain_loss_hysteresis() -> None:
    jitter = np.array([0, 1, 0, 1, 0, 1, 0, 10, 9.5, 10, 5], dtype=float)
    gain, loss = gain_loss_with_hysteresis(jitter, 2.0)
    assert gain == pytest.approx(10)
    assert loss == pytest.approx(5)
    g0, l0 = gain_loss_with_hysteresis(jitter, 0.0)
    assert g0 > gain and g0 - l0 == pytest.approx(5)
