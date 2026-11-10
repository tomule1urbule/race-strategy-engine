"""GPX parsing, grade smoothing and terrain segmentation.

Pipeline
--------
1. Parse track points (gpxpy; stdlib XML fallback if gpxpy is unavailable).
2. Compute cumulative haversine distance, drop zero-length steps.
3. Resample elevation onto a uniform distance grid (default 10 m).
4. Smooth elevation with a centred rolling mean over ``smoothing_window_m`` to kill GPS noise.
5. Rolling grade = centred rolling mean of d(elevation_smooth)/d(distance) * 100.
6. Label each step Flat / Ascent / Descent (+-1.5% thresholds), run-length encode, then
   iteratively absorb blocks shorter than ``min_segment_m`` into their most similar neighbour and
   re-classify by net grade until stable.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Union

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field

from .models import CourseSummary, Segment, TerrainType
from .utils import bearing_rad, haversine_m

try:  # pragma: no cover - exercised only where gpxpy is installed
    import gpxpy

    HAS_GPXPY = True
except ImportError:  # pragma: no cover
    gpxpy = None  # type: ignore[assignment, unused-ignore]
    HAS_GPXPY = False

GPXSource = Union[str, Path, bytes, IO[str], IO[bytes]]  # noqa: UP007 (runtime alias, py3.10)


class GPXParseError(ValueError):
    """Raised when a GPX payload is malformed or lacks usable points."""


class AnalyzerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resample_step_m: float = Field(default=10.0, gt=0, le=100)
    smoothing_window_m: float = Field(default=100.0, ge=0, le=2000)
    flat_threshold_pct: float = Field(default=1.5, gt=0, le=10)
    min_segment_m: float = Field(default=200.0, ge=0)
    max_abs_grade_pct: float = Field(default=40.0, gt=0, description="Clip implausible spikes.")
    gain_hysteresis_m: float = Field(
        default=2.0,
        ge=0,
        description="Dead-band for counting climb/descent (suppresses GPS jitter).",
    )


@dataclass
class CourseAnalysis:
    """Result of :meth:`GPXAnalyzer.analyze`."""

    raw_points: pd.DataFrame
    profile: pd.DataFrame
    segments: list[Segment]
    summary: CourseSummary = field(init=False)

    def __post_init__(self) -> None:
        self.summary = summarize_segments(self.segments, self.profile)


class GPXAnalyzer:
    """Parse a GPX course and split it into terrain blocks."""

    def __init__(self, config: AnalyzerConfig | None = None) -> None:
        self.config = config or AnalyzerConfig()

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _read_text(source: GPXSource) -> str:
        if isinstance(source, Path):
            return source.read_text(encoding="utf-8")
        if isinstance(source, bytes):
            return source.decode("utf-8")
        if isinstance(source, str):
            if source.lstrip().startswith("<"):
                return source
            return Path(source).read_text(encoding="utf-8")
        data = source.read()
        return data.decode("utf-8") if isinstance(data, bytes) else data

    @staticmethod
    def _parse_with_gpxpy(text: str) -> list[tuple[float, float, float | None]]:
        assert gpxpy is not None
        try:
            gpx = gpxpy.parse(io.StringIO(text))
        except Exception as exc:  # gpxpy raises its own exception hierarchy
            raise GPXParseError(f"Invalid GPX: {exc}") from exc
        pts: list[tuple[float, float, float | None]] = [
            (p.latitude, p.longitude, p.elevation) for trk in gpx.tracks for seg in trk.segments for p in seg.points
        ]
        if not pts:
            pts = [(p.latitude, p.longitude, p.elevation) for r in gpx.routes for p in r.points]
        return pts

    @staticmethod
    def _parse_with_stdlib(text: str) -> list[tuple[float, float, float | None]]:
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise GPXParseError(f"Invalid GPX XML: {exc}") from exc

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        def collect(kind: str) -> list[tuple[float, float, float | None]]:
            out: list[tuple[float, float, float | None]] = []
            for el in root.iter():
                if local(el.tag) != kind:
                    continue
                try:
                    lat, lon = float(el.attrib["lat"]), float(el.attrib["lon"])
                except (KeyError, ValueError) as exc:
                    raise GPXParseError(f"Bad {kind} coordinates") from exc
                ele: float | None = None
                for child in el:
                    if local(child.tag) == "ele" and child.text and child.text.strip():
                        ele = float(child.text)
                        break
                out.append((lat, lon, ele))
            return out

        return collect("trkpt") or collect("rtept")

    def load(self, source: GPXSource) -> pd.DataFrame:
        """Return raw points: ``lat, lon, ele, distance_m`` (cumulative)."""
        text = self._read_text(source)
        pts = self._parse_with_gpxpy(text) if HAS_GPXPY else self._parse_with_stdlib(text)
        if len(pts) < 2:
            raise GPXParseError("GPX must contain at least two track/route points")

        df = pd.DataFrame(pts, columns=["lat", "lon", "ele"], dtype="float64")
        if df["ele"].isna().all():
            raise GPXParseError("GPX has no elevation data; cannot compute grades")
        df["ele"] = df["ele"].interpolate(limit_direction="both")

        lat = df["lat"].to_numpy()
        lon = df["lon"].to_numpy()
        step = np.concatenate([[0.0], haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:])])
        keep = np.concatenate([[True], step[1:] > 0.01])  # drop duplicate/stationary points
        df = df.loc[keep].reset_index(drop=True)
        df["distance_m"] = np.cumsum(step[keep])
        if df["distance_m"].iloc[-1] < self.config.resample_step_m * 2:
            raise GPXParseError("Course is too short to analyse")
        out: pd.DataFrame = df
        return out

    # ---------------------------------------------------------------- profiling
    def build_profile(self, raw: pd.DataFrame) -> pd.DataFrame:
        """Uniformly resampled, smoothed elevation profile with per-step grade & heading."""
        cfg = self.config
        total = float(raw["distance_m"].iloc[-1])
        grid = np.arange(0.0, total, cfg.resample_step_m)
        if total - grid[-1] > 1e-6:
            grid = np.append(grid, total)

        xp = raw["distance_m"].to_numpy()
        ele = np.interp(grid, xp, raw["ele"].to_numpy())
        lat = np.interp(grid, xp, raw["lat"].to_numpy())
        lon = np.interp(grid, xp, raw["lon"].to_numpy())

        window = max(1, round(cfg.smoothing_window_m / cfg.resample_step_m))
        window += (window + 1) % 2  # force odd for a symmetric centred window
        ele_smooth = pd.Series(ele).rolling(window=window, center=True, min_periods=1).mean().to_numpy()

        dx = np.diff(grid)
        step_grade = np.clip(np.diff(ele_smooth) / dx * 100.0, -cfg.max_abs_grade_pct, cfg.max_abs_grade_pct)
        # Rolling grade: a second centred mean over the same window (=> triangular kernel on
        # elevation), which suppresses the residual jitter that differentiation amplifies.
        grade = pd.Series(step_grade).rolling(window=window, center=True, min_periods=1).mean().to_numpy()
        heading = bearing_rad(lat[:-1], lon[:-1], lat[1:], lon[1:])

        profile: pd.DataFrame = pd.DataFrame(
            {
                "distance_m": grid,
                "lat": lat,
                "lon": lon,
                "ele": ele,
                "ele_smooth": ele_smooth,
                # step quantities are attributed to the step's starting point; last row padded
                "step_m": np.append(dx, 0.0),
                "grade_pct": np.append(grade, grade[-1]),
                "heading_rad": np.append(heading, heading[-1]),
            }
        )
        return profile

    # ------------------------------------------------------------- segmentation
    def classify_grade(self, grade_pct: float) -> TerrainType:
        t = self.config.flat_threshold_pct
        if grade_pct > t:
            return TerrainType.ASCENT
        if grade_pct < -t:
            return TerrainType.DESCENT
        return TerrainType.FLAT

    def segment(self, profile: pd.DataFrame) -> list[Segment]:
        dist = profile["distance_m"].to_numpy()
        ele = profile["ele_smooth"].to_numpy()
        n_steps = len(dist) - 1
        step_grade = profile["grade_pct"].to_numpy()[:n_steps]

        # Run-length encode per-step labels into [start_pt, end_pt] blocks.
        labels = [self.classify_grade(float(g)) for g in step_grade]
        blocks: list[list[int]] = []
        start = 0
        for i in range(1, n_steps + 1):
            if i == n_steps or labels[i] != labels[start]:
                blocks.append([start, i])
                start = i

        blocks = self._merge_short_blocks(blocks, dist, ele)
        return [self._make_segment(k, a, b, profile) for k, (a, b) in enumerate(blocks)]

    def _block_grade(self, block: Sequence[int], dist: NDArray[np.float64], ele: NDArray[np.float64]) -> float:
        a, b = block[0], block[1]
        return float((ele[b] - ele[a]) / (dist[b] - dist[a]) * 100.0)

    def _merge_short_blocks(
        self, blocks: list[list[int]], dist: NDArray[np.float64], ele: NDArray[np.float64]
    ) -> list[list[int]]:
        min_len = self.config.min_segment_m

        def coalesce(bl: list[list[int]]) -> list[list[int]]:
            out: list[list[int]] = []
            for blk in bl:
                if out and self.classify_grade(self._block_grade(out[-1], dist, ele)) == (
                    self.classify_grade(self._block_grade(blk, dist, ele))
                ):
                    out[-1][1] = blk[1]
                else:
                    out.append(list(blk))
            return out

        blocks = coalesce(blocks)
        while len(blocks) > 1:
            lengths = [dist[b] - dist[a] for a, b in blocks]
            k = int(np.argmin(lengths))
            if lengths[k] >= min_len:
                break
            g = self._block_grade(blocks[k], dist, ele)
            if k == 0:
                j = 1
            elif k == len(blocks) - 1:
                j = k - 1
            else:
                gl = self._block_grade(blocks[k - 1], dist, ele)
                gr = self._block_grade(blocks[k + 1], dist, ele)
                j = k - 1 if abs(gl - g) <= abs(gr - g) else k + 1
            lo, hi = min(j, k), max(j, k)
            blocks[lo] = [blocks[lo][0], blocks[hi][1]]
            del blocks[hi]
            blocks = coalesce(blocks)
        return blocks

    def _make_segment(self, index: int, a: int, b: int, profile: pd.DataFrame) -> Segment:
        dist = profile["distance_m"].to_numpy()
        ele = profile["ele_smooth"].to_numpy()
        steps = profile["step_m"].to_numpy()[a:b]
        heading = profile["heading_rad"].to_numpy()[a:b]
        gain, loss = gain_loss_with_hysteresis(ele[a : b + 1], self.config.gain_hysteresis_m)
        length = float(dist[b] - dist[a])
        grade = float((ele[b] - ele[a]) / length * 100.0)
        w = steps / steps.sum()
        return Segment(
            index=index,
            terrain=self.classify_grade(grade),
            start_distance_m=round(float(dist[a]), 2),
            end_distance_m=round(float(dist[b]), 2),
            avg_grade_pct=round(grade, 3),
            elevation_delta_m=round(float(ele[b] - ele[a]), 2),
            elevation_gain_m=round(gain, 2),
            elevation_loss_m=round(loss, 2),
            start_elevation_m=round(float(ele[a]), 2),
            end_elevation_m=round(float(ele[b]), 2),
            heading_east=round(float(np.clip(np.sum(w * np.sin(heading)), -1, 1)), 4),
            heading_north=round(float(np.clip(np.sum(w * np.cos(heading)), -1, 1)), 4),
        )

    # ------------------------------------------------------------------ facade
    def analyze(self, source: GPXSource) -> CourseAnalysis:
        raw = self.load(source)
        profile = self.build_profile(raw)
        return CourseAnalysis(raw_points=raw, profile=profile, segments=self.segment(profile))


def gain_loss_with_hysteresis(ele: NDArray[np.float64], threshold_m: float) -> tuple[float, float]:
    """Total climb/descent counting only reversals larger than ``threshold_m``.

    Partial movement at the end of the series is credited so a segment's
    ``gain - loss`` always equals its net elevation change.
    """
    if len(ele) < 2:
        return 0.0, 0.0
    if threshold_m <= 0:
        d = np.diff(ele)
        return float(d[d > 0].sum()), float(-d[d < 0].sum())
    gain = loss = 0.0
    anchor = float(ele[0])
    for e in ele[1:]:
        delta = float(e) - anchor
        if delta >= threshold_m:
            gain += delta
            anchor = float(e)
        elif delta <= -threshold_m:
            loss -= delta
            anchor = float(e)
    tail = float(ele[-1]) - anchor
    if tail > 0:
        gain += tail
    else:
        loss -= tail
    return gain, loss


def summarize_segments(segments: list[Segment], profile: pd.DataFrame | None = None) -> CourseSummary:
    terrain_km = {t: 0.0 for t in TerrainType}
    for s in segments:
        terrain_km[s.terrain] += s.distance_m / 1000.0
    if profile is not None:
        lo, hi = float(profile["ele_smooth"].min()), float(profile["ele_smooth"].max())
    else:
        elevs = [e for s in segments for e in (s.start_elevation_m, s.end_elevation_m)]
        lo, hi = min(elevs), max(elevs)
    return CourseSummary(
        total_distance_km=round(segments[-1].end_distance_m / 1000.0, 3),
        elevation_gain_m=round(sum(s.elevation_gain_m for s in segments), 1),
        elevation_loss_m=round(sum(s.elevation_loss_m for s in segments), 1),
        min_elevation_m=round(lo, 1),
        max_elevation_m=round(hi, 1),
        segment_count=len(segments),
        terrain_km={k: round(v, 3) for k, v in terrain_km.items()},
    )
