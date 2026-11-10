"""Pydantic v2 domain models: inputs (athlete, weather, strategy) and outputs (blueprint)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

SCHEMA_VERSION = "1.0"


class _Model(BaseModel):
    """Base model: reject unknown fields, validate on assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Sport(str, Enum):
    RUN = "run"
    BIKE = "bike"


class TerrainType(str, Enum):
    FLAT = "Flat"
    ASCENT = "Ascent"
    DESCENT = "Descent"


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------


class AthleteProfile(_Model):
    """Physiological profile of the athlete."""

    sport: Sport = Sport.RUN
    weight_kg: float = Field(gt=30, le=200, description="Body mass in kg.")
    threshold_pace_sec_km: int | None = Field(
        default=None, ge=150, le=600, description="Running threshold pace, s/km (270 = 4:30/km)."
    )
    threshold_power_watts: int | None = Field(default=None, ge=50, le=600, description="Cycling FTP in watts.")
    max_hr: int = Field(ge=120, le=230, description="Maximum heart rate, bpm.")
    threshold_hr: int | None = Field(
        default=None,
        ge=100,
        le=220,
        description="Lactate-threshold HR (LTHR). Defaults to 90% of max_hr when omitted.",
    )
    target_carbs_per_hour: int = Field(
        default=60,
        ge=0,
        le=150,
        description="Carbohydrate target, g/h (60 default, 90+ if gut-trained).",
    )
    sweat_rate_l_hr: float = Field(default=0.8, gt=0, le=3.5, description="Baseline sweat rate at <=18 C, L/h.")
    sweat_sodium_mg_l: int = Field(default=900, ge=200, le=2500, description="Sweat sodium concentration, mg/L.")

    @model_validator(mode="after")
    def _check_sport_threshold(self) -> AthleteProfile:
        if self.sport is Sport.RUN and self.threshold_pace_sec_km is None:
            raise ValueError("threshold_pace_sec_km is required for runners")
        if self.sport is Sport.BIKE and self.threshold_power_watts is None:
            raise ValueError("threshold_power_watts is required for cyclists")
        if self.threshold_hr is not None and self.threshold_hr >= self.max_hr:
            raise ValueError("threshold_hr must be below max_hr")
        return self

    @property
    def lthr(self) -> int:
        """Lactate-threshold heart rate (explicit or 90% of max)."""
        return self.threshold_hr if self.threshold_hr is not None else round(0.90 * self.max_hr)


class WeatherConditions(_Model):
    temperature_c: float = Field(ge=-20, le=50, description="Air temperature at start, C.")
    temperature_end_c: float | None = Field(
        default=None, ge=-20, le=50, description="Forecast temperature at finish (linear drift)."
    )
    humidity_pct: float = Field(ge=0, le=100)
    wind_speed_kmh: float = Field(default=0.0, ge=0, le=120)
    wind_direction_deg: float | None = Field(
        default=None,
        ge=0,
        le=360,
        description="Meteorological wind direction (where wind blows FROM). Enables head/tailwind.",
    )
    start_time: datetime


class FuelingProducts(_Model):
    gel_name: str = "Isotonic Gel"
    gel_carbs_g: float = Field(default=22.0, gt=0, le=100)
    gel_sodium_mg: float = Field(default=0.0, ge=0, le=1000)
    drink_name: str = "Electrolytes"
    drink_carbs_g_per_l: float = Field(default=0.0, ge=0, le=150)
    drink_sodium_mg_per_l: float = Field(default=500.0, ge=0, le=2000)


class BikeSetup(_Model):
    bike_mass_kg: float = Field(default=8.5, gt=3, le=40)
    cda_m2: float = Field(default=0.32, gt=0.15, le=0.8, description="Drag area, m^2.")
    crr: float = Field(default=0.004, gt=0.001, le=0.02, description="Rolling resistance coeff.")
    drivetrain_efficiency: float = Field(default=0.975, gt=0.8, le=1.0)
    max_descent_speed_kmh: float = Field(default=70.0, gt=20, le=120)


class RaceStrategy(_Model):
    target_finish_time_s: int | None = Field(
        default=None, gt=0, description="Goal time; if omitted the engine predicts one."
    )
    pacing: Literal["even_effort", "negative_split"] = "even_effort"
    aid_stations_km: list[float] = Field(
        default_factory=list, description="If given, fueling events are scheduled at these points."
    )
    max_fluid_l_hr: float = Field(default=1.0, gt=0.1, le=1.5)
    fueling: FuelingProducts = Field(default_factory=FuelingProducts)
    bike: BikeSetup = Field(default_factory=BikeSetup)


# --------------------------------------------------------------------------------------
# Course
# --------------------------------------------------------------------------------------


class Segment(_Model):
    """A contiguous block of homogeneous terrain."""

    index: int = Field(ge=0)
    terrain: TerrainType
    start_distance_m: float = Field(ge=0)
    end_distance_m: float = Field(ge=0)
    avg_grade_pct: float
    elevation_delta_m: float
    elevation_gain_m: float = Field(ge=0)
    elevation_loss_m: float = Field(ge=0)
    start_elevation_m: float
    end_elevation_m: float
    heading_east: float = Field(
        default=0.0, ge=-1, le=1, description="Distance-weighted mean east component of travel."
    )
    heading_north: float = Field(
        default=0.0, ge=-1, le=1, description="Distance-weighted mean north component of travel."
    )

    @model_validator(mode="after")
    def _check_order(self) -> Segment:
        if self.end_distance_m <= self.start_distance_m:
            raise ValueError("end_distance_m must exceed start_distance_m")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def distance_m(self) -> float:
        return self.end_distance_m - self.start_distance_m


class CourseSummary(_Model):
    total_distance_km: float
    elevation_gain_m: float
    elevation_loss_m: float
    min_elevation_m: float
    max_elevation_m: float
    segment_count: int
    terrain_km: dict[TerrainType, float]


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


class EnvironmentSummary(_Model):
    apparent_temp_start_c: float
    apparent_temp_end_c: float
    heat_performance_penalty_pct: float = Field(description="Estimated slowdown (run) or power loss (bike) from heat.")
    sweat_rate_start_l_hr: float
    sweat_rate_end_l_hr: float
    air_density_kg_m3: float


class SegmentPlan(_Model):
    segment_index: int
    terrain: TerrainType
    start_km: float
    end_km: float
    avg_grade_pct: float
    est_start_s: float
    est_duration_s: float
    target_speed_kmh: float
    target_pace_sec_km: float | None = None
    target_pace_label: str | None = None
    target_power_watts: int | None = None
    effort_factor: float = Field(description="Grade cost multiplier vs flat (GAP factor).")
    wind_factor: float
    hr_target_low: int
    hr_target_high: int
    hr_cap: int
    guidance: str


class Split(_Model):
    index: int
    end_km: float
    split_s: float
    cumulative_s: float
    split_label: str
    cumulative_label: str


class FuelingEvent(_Model):
    elapsed_s: float
    elapsed_label: str
    clock_time: datetime
    distance_km: float
    terrain: TerrainType
    gels: int = Field(ge=0)
    carbs_g: float = Field(ge=0)
    fluid_ml: int = Field(ge=0)
    sodium_mg: int = Field(ge=0)
    message: str


class NutritionSummary(_Model):
    carbs_per_hour_target: float
    carbs_total_g: float
    gels_total: int
    fluid_total_ml: int
    fluid_avg_l_hr: float
    sweat_loss_total_l: float
    sweat_rate_avg_l_hr: float
    est_body_mass_loss_pct: float
    sodium_total_mg: int


class RaceBlueprint(_Model):
    schema_version: str = SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    athlete: AthleteProfile
    weather: WeatherConditions
    strategy: RaceStrategy
    course: CourseSummary
    environment: EnvironmentSummary
    predicted_finish_s: float
    predicted_finish_label: str
    sustainable_finish_s: float = Field(description="Model's estimate of a sustainable finish in these conditions.")
    segments: list[SegmentPlan]
    splits: list[Split]
    fueling_events: list[FuelingEvent]
    nutrition: NutritionSummary
    warnings: list[str] = Field(default_factory=list)
