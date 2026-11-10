from __future__ import annotations

import math
import re
from datetime import datetime
from itertools import pairwise

import pytest
from race_strategy import (
    AthleteProfile,
    GPXAnalyzer,
    RaceBlueprint,
    RaceEngine,
    RaceStrategy,
    Segment,
    TerrainType,
    WeatherConditions,
)
from race_strategy.race_engine import (
    ZONE_3_4_BOUNDARY,
    bike_power_for_speed,
    bike_speed_for_power,
    carb_rate_for_duration,
    run_gap_factor,
    run_wind_factor,
    sweat_rate_l_hr,
)
from race_strategy.synthetic import make_gpx

MESSAGE_RE = re.compile(r"^At km \d+\.\d \[Approx\. (\d+:)?\d{1,2}:\d{2}\], consume .+")


def _plans(bp: RaceBlueprint, terrain: TerrainType):  # type: ignore[no-untyped-def]
    return [s for s in bp.segments if s.terrain is terrain]


# ======================================================================= model functions
class TestGradeModel:
    def test_flat_is_unity(self) -> None:
        assert run_gap_factor(0.0) == pytest.approx(1.0)

    def test_minetti_uphill_values(self) -> None:
        assert run_gap_factor(0.05) == pytest.approx(1.301, abs=0.01)
        assert run_gap_factor(0.10) == pytest.approx(1.658, abs=0.01)

    def test_uphill_monotonic(self) -> None:
        vals = [run_gap_factor(g / 100) for g in range(0, 30, 2)]
        assert vals == sorted(vals)

    def test_downhill_benefit_is_bounded(self) -> None:
        assert 0.85 <= run_gap_factor(-0.05) < 1.0
        assert run_gap_factor(-0.10) == pytest.approx(0.85, abs=1e-6)
        assert run_gap_factor(-0.30) > run_gap_factor(-0.10)  # very steep descents get slower

    def test_wind(self) -> None:
        assert run_wind_factor(4.0, 0.0) == 1.0
        head, tail = run_wind_factor(4.0, 5.0), run_wind_factor(4.0, -5.0)
        assert head > 1.0 > tail
        assert head - 1.0 > 1.0 - tail  # tailwind helps less than headwind hurts


class TestPhysiology:
    @pytest.mark.parametrize(
        "apparent,expected",
        [(10, 0.8), (18, 0.8), (23, 0.8 * 1.15), (28, 0.8 * 1.30), (20.5, 0.8 * 1.075)],
    )
    def test_sweat_rate_15pct_per_5c(self, apparent: float, expected: float) -> None:
        assert sweat_rate_l_hr(0.8, apparent) == pytest.approx(expected)

    def test_sweat_rate_capped(self) -> None:
        assert sweat_rate_l_hr(2.5, 60) == 3.0

    @pytest.mark.parametrize(
        "hours,target,expected",
        [(0.5, 60, 0), (1.0, 60, 30), (2.0, 90, 60), (2.0, 40, 40), (3.5, 90, 90)],
    )
    def test_carb_gating(self, hours: float, target: float, expected: float) -> None:
        assert carb_rate_for_duration(target, hours * 3600) == expected


class TestBikePhysics:
    def test_power_speed_inverse(self) -> None:
        args = (83.5, 0.32, 0.004, 1.225, 0.0, 0.975)
        v = bike_speed_for_power(250, 0.0, *args, v_max=30)
        assert bike_power_for_speed(v, 0.0, *args) == pytest.approx(250, rel=1e-4)

    def test_realistic_flat_speed(self) -> None:
        v = bike_speed_for_power(200, 0.0, 83.5, 0.32, 0.004, 1.225, 0.0, 0.975, v_max=30)
        assert 32 < v * 3.6 < 37

    def test_descent_speed_cap(self) -> None:
        v = bike_speed_for_power(300, -0.10, 83.5, 0.32, 0.004, 1.225, 0.0, 0.975, v_max=70 / 3.6)
        assert v == pytest.approx(70 / 3.6)

    def test_climbing_speed(self) -> None:
        v = bike_speed_for_power(250, 0.07, 83.5, 0.32, 0.004, 1.225, 0.0, 0.975, v_max=30)
        assert 12 < v * 3.6 < 16


# ======================================================================= running engine
class TestRunningPlan:
    def test_target_time_hit_exactly(self, runner, hilly_half_segments, mild_weather) -> None:  # type: ignore[no-untyped-def]
        bp = RaceEngine(runner, hilly_half_segments, mild_weather, RaceStrategy(target_finish_time_s=6000)).generate()
        assert bp.predicted_finish_s == pytest.approx(6000, abs=1)
        assert sum(s.est_duration_s for s in bp.segments) == pytest.approx(6000, abs=1)
        assert sum(s.split_s for s in bp.splits) == pytest.approx(6000, abs=1)

    def test_terrain_pacing_order(self, runner, hilly_half_segments, mild_weather) -> None:  # type: ignore[no-untyped-def]
        bp = RaceEngine(runner, hilly_half_segments, mild_weather).generate()
        asc = _plans(bp, TerrainType.ASCENT)[0].target_pace_sec_km
        desc = _plans(bp, TerrainType.DESCENT)[0].target_pace_sec_km
        flat = _plans(bp, TerrainType.FLAT)[0].target_pace_sec_km
        assert asc is not None and desc is not None and flat is not None
        assert asc > flat > desc

    def test_ascents_are_effort_capped(self, runner, hilly_half_segments, mild_weather) -> None:  # type: ignore[no-untyped-def]
        bp = RaceEngine(runner, hilly_half_segments, mild_weather, RaceStrategy(target_finish_time_s=6000)).generate()
        asc = _plans(bp, TerrainType.ASCENT)[0]
        flat = _plans(bp, TerrainType.FLAT)[0]
        # grade-adjusted ascent pace is slower than flat pace -> flats absorb the difference
        assert asc.target_pace_sec_km / asc.effort_factor > flat.target_pace_sec_km * 1.02  # type: ignore[operator]
        assert asc.hr_cap == round(ZONE_3_4_BOUNDARY * runner.lthr)
        assert asc.hr_target_high <= asc.hr_cap
        assert all(s.hr_cap <= runner.max_hr for s in bp.segments)

    def test_short_race_ascent_cap_is_lthr(self, runner, mild_weather) -> None:  # type: ignore[no-untyped-def]
        segs = GPXAnalyzer().analyze(make_gpx([(2000, 0), (1000, 4), (2000, 0)])).segments
        bp = RaceEngine(runner, segs, mild_weather).generate()
        assert _plans(bp, TerrainType.ASCENT)[0].hr_cap == runner.lthr

    def test_negative_split(self, runner, mild_weather) -> None:  # type: ignore[no-untyped-def]
        segs = GPXAnalyzer().analyze(make_gpx([(10000, 0)], spacing_m=20)).segments
        # split a flat course in two artificial halves
        a, b = (
            segs[0].model_dump(exclude={"distance_m"}),
            segs[0].model_dump(exclude={"distance_m"}),
        )
        a["end_distance_m"] = b["start_distance_m"] = 5000
        b["index"] = 1
        halves = [Segment.model_validate(a), Segment.model_validate(b)]
        bp = RaceEngine(
            runner,
            halves,
            mild_weather,
            RaceStrategy(target_finish_time_s=2700, pacing="negative_split"),
        ).generate()
        first, second = bp.segments
        assert second.target_pace_sec_km < first.target_pace_sec_km  # type: ignore[operator]
        assert bp.predicted_finish_s == pytest.approx(2700, abs=1)

    def test_headwind_vs_tailwind(self, runner) -> None:  # type: ignore[no-untyped-def]
        segs = GPXAnalyzer().analyze(make_gpx([(10000, 0)], bearing_deg=90)).segments

        def finish(direction: float) -> float:
            w = WeatherConditions(
                temperature_c=12,
                humidity_pct=60,
                wind_speed_kmh=20,
                wind_direction_deg=direction,
                start_time=datetime(2026, 1, 1, 9),
            )
            return RaceEngine(runner, segs, w).generate().predicted_finish_s

        assert finish(90) > finish(0) > finish(270)  # from east = headwind for eastbound course

    def test_heat_slows_and_raises_sweat(self, runner, hilly_half_segments, mild_weather, hot_weather) -> None:  # type: ignore[no-untyped-def]
        cool = RaceEngine(runner, hilly_half_segments, mild_weather).generate()
        hot = RaceEngine(runner, hilly_half_segments, hot_weather).generate()
        assert hot.predicted_finish_s > cool.predicted_finish_s
        assert hot.environment.sweat_rate_end_l_hr > hot.environment.sweat_rate_start_l_hr > runner.sweat_rate_l_hr
        assert cool.environment.sweat_rate_start_l_hr == pytest.approx(runner.sweat_rate_l_hr)
        assert any("Heat stress" in w for w in hot.warnings)

    def test_aggressive_target_warns(self, runner, hilly_half_segments, mild_weather) -> None:  # type: ignore[no-untyped-def]
        bp = RaceEngine(runner, hilly_half_segments, mild_weather, RaceStrategy(target_finish_time_s=4800)).generate()
        assert any("faster than the model's sustainable" in w for w in bp.warnings)

    def test_prediction_is_plausible(self, runner, hilly_half_segments, mild_weather) -> None:  # type: ignore[no-untyped-def]
        # 4:30/km threshold -> roughly 1:36-1:42 on a hilly half in cool, still conditions
        bp = RaceEngine(runner, hilly_half_segments, mild_weather).generate()
        assert 96 * 60 < bp.predicted_finish_s < 102 * 60


# ======================================================================= nutrition
class TestNutrition:
    def _marathon(self, runner: AthleteProfile, weather: WeatherConditions, **kw: object) -> RaceBlueprint:
        segs = GPXAnalyzer().analyze(make_gpx([(42195, 0)], spacing_m=25)).segments
        return RaceEngine(runner, segs, weather, RaceStrategy(target_finish_time_s=3 * 3600 + 30 * 60, **kw)).generate()  # type: ignore[arg-type]

    def test_schedule_shape(self, runner, mild_weather) -> None:  # type: ignore[no-untyped-def]
        bp = self._marathon(runner, mild_weather)
        ev = bp.fueling_events
        assert len(ev) >= 8
        times = [e.elapsed_s for e in ev]
        assert times == sorted(times)
        assert all(b - a >= 5 * 60 for a, b in pairwise(times))
        assert times[-1] <= bp.predicted_finish_s - 10 * 60
        assert all(MESSAGE_RE.match(e.message) for e in ev), ev[0].message
        assert ev[0].clock_time > mild_weather.start_time

    def test_carb_total_matches_target(self, runner, mild_weather) -> None:  # type: ignore[no-untyped-def]
        bp = self._marathon(runner, mild_weather)
        last = bp.fueling_events[-1].elapsed_s
        expected = runner.target_carbs_per_hour * last / 3600
        assert abs(bp.nutrition.carbs_total_g - expected) <= 22  # within one gel
        assert bp.nutrition.gels_total == sum(e.gels for e in bp.fueling_events)

    def test_fluid_bounds(self, runner, hot_weather) -> None:  # type: ignore[no-untyped-def]
        bp = self._marathon(runner, hot_weather)
        n = bp.nutrition
        assert n.fluid_avg_l_hr <= RaceStrategy().max_fluid_l_hr + 1e-6
        assert 0.4 * n.sweat_loss_total_l * 0.9 <= n.fluid_total_ml / 1000 <= 0.8 * n.sweat_loss_total_l * 1.05
        assert all(e.fluid_ml % 50 == 0 for e in bp.fueling_events)
        assert n.sodium_total_mg == sum(e.sodium_mg for e in bp.fueling_events)

    def test_hot_race_drinks_more(self, runner, mild_weather, hot_weather) -> None:  # type: ignore[no-untyped-def]
        assert (
            self._marathon(runner, hot_weather).nutrition.fluid_total_ml
            > self._marathon(runner, mild_weather).nutrition.fluid_total_ml
        )

    def test_aid_stations(self, runner, mild_weather) -> None:  # type: ignore[no-untyped-def]
        stations = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0]
        bp = self._marathon(runner, mild_weather, aid_stations_km=stations)
        assert [round(e.distance_km, 1) for e in bp.fueling_events] == stations

    def test_short_race_has_no_fueling(self, runner, mild_weather) -> None:  # type: ignore[no-untyped-def]
        segs = GPXAnalyzer().analyze(make_gpx([(5000, 0)])).segments
        bp = RaceEngine(runner, segs, mild_weather).generate()
        assert bp.fueling_events == []
        assert any("under 45 min" in w for w in bp.warnings)

    def test_running_fuel_avoids_climbs(self, runner, mild_weather) -> None:  # type: ignore[no-untyped-def]
        # ~5:00/km -> first gel due ~22 min, i.e. inside the climb that starts at 4.2 km
        gpx = make_gpx([(4200, 0), (1200, 6), (15000, 0)])
        segs = GPXAnalyzer().analyze(gpx).segments
        bp = RaceEngine(runner, segs, mild_weather, RaceStrategy(target_finish_time_s=6100)).generate()
        assert bp.fueling_events
        assert all(e.terrain is not TerrainType.ASCENT for e in bp.fueling_events)
        assert "before the climb" in bp.fueling_events[0].message

    def test_high_carb_warning(self, mild_weather) -> None:  # type: ignore[no-untyped-def]
        athlete = AthleteProfile(weight_kg=70, threshold_pace_sec_km=270, max_hr=190, target_carbs_per_hour=100)
        assert any("90 g/h" in w for w in self._marathon(athlete, mild_weather).warnings)


# ======================================================================= cycling & output
class TestCyclingAndOutput:
    def _gran_fondo(self) -> list[Segment]:
        return GPXAnalyzer().analyze(make_gpx([(30000, 0), (8000, 6), (8000, -6), (30000, 0)], spacing_m=25)).segments

    def test_cycling_plan(self, cyclist, mild_weather) -> None:  # type: ignore[no-untyped-def]
        bp = RaceEngine(cyclist, self._gran_fondo(), mild_weather).generate()
        ftp = cyclist.threshold_power_watts
        asc = _plans(bp, TerrainType.ASCENT)[0]
        flat = _plans(bp, TerrainType.FLAT)[0]
        desc = _plans(bp, TerrainType.DESCENT)[0]
        assert asc.target_power_watts <= round(0.95 * ftp)  # type: ignore[operator]
        assert asc.target_power_watts > flat.target_power_watts > desc.target_power_watts  # type: ignore[operator]
        assert desc.target_speed_kmh <= 70.0
        assert asc.target_pace_sec_km is None
        assert 2.0 * 3600 < bp.predicted_finish_s < 3.0 * 3600
        assert bp.splits[0].end_km == 5.0

    def test_cycling_target_time(self, cyclist, mild_weather) -> None:  # type: ignore[no-untyped-def]
        bp = RaceEngine(cyclist, self._gran_fondo(), mild_weather, RaceStrategy(target_finish_time_s=9000)).generate()
        assert bp.predicted_finish_s == pytest.approx(9000, abs=2)

    def test_json_roundtrip(self, runner, hilly_half_segments, hot_weather) -> None:  # type: ignore[no-untyped-def]
        bp = RaceEngine(runner, hilly_half_segments, hot_weather).generate()
        again = RaceBlueprint.model_validate_json(bp.model_dump_json())
        assert again == bp
        assert len(bp.splits) == math.ceil(bp.course.total_distance_km)

    def test_empty_segments_rejected(self, runner, mild_weather) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ValueError):
            RaceEngine(runner, [], mild_weather)
