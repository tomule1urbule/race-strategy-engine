from __future__ import annotations

import pytest
from pydantic import ValidationError
from race_strategy import AthleteProfile, RaceStrategy, Segment, Sport, TerrainType


def test_runner_requires_threshold_pace() -> None:
    with pytest.raises(ValidationError, match="threshold_pace_sec_km"):
        AthleteProfile(weight_kg=70, max_hr=190)


def test_cyclist_requires_ftp() -> None:
    with pytest.raises(ValidationError, match="threshold_power_watts"):
        AthleteProfile(sport=Sport.BIKE, weight_kg=70, max_hr=190, threshold_pace_sec_km=270)


def test_defaults_and_lthr_fallback() -> None:
    a = AthleteProfile(weight_kg=70, threshold_pace_sec_km=270, max_hr=190)
    assert a.target_carbs_per_hour == 60
    assert a.sweat_rate_l_hr == 0.8
    assert a.lthr == 171


def test_threshold_hr_must_be_below_max() -> None:
    with pytest.raises(ValidationError):
        AthleteProfile(weight_kg=70, threshold_pace_sec_km=270, max_hr=180, threshold_hr=185)


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        AthleteProfile(weight_kg=70, threshold_pace_sec_km=270, max_hr=190, vo2=60)  # type: ignore[call-arg]


@pytest.mark.parametrize("field,value", [("weight_kg", -1), ("max_hr", 300), ("target_carbs_per_hour", 500)])
def test_range_validation(field: str, value: float) -> None:
    kwargs = {"weight_kg": 70, "threshold_pace_sec_km": 270, "max_hr": 190, field: value}
    with pytest.raises(ValidationError):
        AthleteProfile(**kwargs)  # type: ignore[arg-type]


def test_segment_distance_and_order() -> None:
    seg = Segment(
        index=0,
        terrain=TerrainType.FLAT,
        start_distance_m=100,
        end_distance_m=600,
        avg_grade_pct=0,
        elevation_delta_m=0,
        elevation_gain_m=0,
        elevation_loss_m=0,
        start_elevation_m=10,
        end_elevation_m=10,
    )
    assert seg.distance_m == 500
    assert seg.model_dump()["distance_m"] == 500
    with pytest.raises(ValidationError):
        Segment.model_validate({**seg.model_dump(exclude={"distance_m"}), "end_distance_m": 50})


def test_strategy_defaults() -> None:
    s = RaceStrategy()
    assert s.pacing == "even_effort"
    assert s.fueling.gel_carbs_g == 22
