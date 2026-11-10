"""Algorithmic Race Strategy & Nutrition Generator."""

from .gpx_analyzer import AnalyzerConfig, CourseAnalysis, GPXAnalyzer, GPXParseError
from .models import (
    AthleteProfile,
    BikeSetup,
    FuelingEvent,
    FuelingProducts,
    RaceBlueprint,
    RaceStrategy,
    Segment,
    SegmentPlan,
    Sport,
    TerrainType,
    WeatherConditions,
)
from .race_engine import RaceEngine

__all__ = [
    "AnalyzerConfig",
    "AthleteProfile",
    "BikeSetup",
    "CourseAnalysis",
    "FuelingEvent",
    "FuelingProducts",
    "GPXAnalyzer",
    "GPXParseError",
    "RaceBlueprint",
    "RaceEngine",
    "RaceStrategy",
    "Segment",
    "SegmentPlan",
    "Sport",
    "TerrainType",
    "WeatherConditions",
    "build_blueprint",
]

__version__ = "0.1.0"


def build_blueprint(
    gpx: object,
    athlete: AthleteProfile,
    weather: WeatherConditions,
    strategy: RaceStrategy | None = None,
    analyzer_config: AnalyzerConfig | None = None,
) -> RaceBlueprint:
    """One-call convenience: GPX source -> validated :class:`RaceBlueprint`."""
    analysis = GPXAnalyzer(analyzer_config).analyze(gpx)  # type: ignore[arg-type]
    return RaceEngine(athlete, analysis.segments, weather, strategy).generate()
