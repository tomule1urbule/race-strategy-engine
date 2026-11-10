"""Environmental & physiological race engine.

Turns an :class:`AthleteProfile`, a segmented course and weather into a :class:`RaceBlueprint`.

Models used (all constants are module-level so they can be tuned / unit-tested):

* **Running grade cost** - Minetti et al. (2002) energy-cost polynomial for uphill; downhill uses a
  50%-damped Minetti curve floored at 0.85 (field GAP data shows runners cannot harvest the full
  treadmill-measured downhill benefit at race pace), rising again on very steep descents.
* **Running pace** - with a goal time, base flat pace is solved so the course sums to the goal;
  otherwise threshold pace is extended with Riegel's exponent (1.06) to the race duration.
* **Ascent protection** - climbs get an extra effort buffer (run) or a power cap (bike) plus a hard
  HR cap at the Zone 3/4 boundary (95% LTHR) for races >= 90 min. Flats/descents absorb the
  difference so the overall finish target is still met.
* **Cycling** - steady-state power balance (gravity + rolling + aero with head/tailwind, air
  density from temperature & altitude) solved for speed by bisection.
* **Heat** - Steadman apparent temperature; performance penalty 0.3 %/C above 15 C (scaled by
  duration); sweat rate +15 % per 5 C above 18 C (continuous).
* **Fluids** - replace enough sweat to keep body-mass loss near 2 % (40-80 % replacement), capped
  by gastric-emptying ceiling ``max_fluid_l_hr``.
* **Carbs** - duration-gated (Jeukendrup 2014): none <45 min, <=30 g/h <75 min, <=60 g/h <2.5 h,
  athlete target beyond.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

import numpy as np

from .gpx_analyzer import summarize_segments
from .models import (
    AthleteProfile,
    EnvironmentSummary,
    FuelingEvent,
    NutritionSummary,
    RaceBlueprint,
    RaceStrategy,
    Segment,
    SegmentPlan,
    Split,
    Sport,
    TerrainType,
    WeatherConditions,
)
from .utils import air_density, apparent_temperature_c, format_duration, format_pace

G = 9.80665

# --- running grade model ------------------------------------------------------------
DOWNHILL_DAMPING = 0.5
DOWNHILL_RATIO_FLOOR = 0.85
STEEP_DOWNHILL_GRADE = -0.15
RIEGEL_EXPONENT = 0.06
ASCENT_EFFORT_BUFFER = 0.03  # climbs run 3% "easier" than even effort
NEGATIVE_SPLIT_DELTA = 0.01
RUN_AIR_COST_FRACTION = 0.04  # share of running cost spent on air drag at 5.5 m/s
RUN_AIR_REF_SPEED = 5.5

# --- HR model (fractions of LTHR) --------------------------------------------------
ZONE_3_4_BOUNDARY = 0.95
LONG_RACE_S = 90 * 60

# --- cycling ------------------------------------------------------------------------
BIKE_ASCENT_POWER_MULT = 1.10
BIKE_ASCENT_FTP_CAP_LONG = 0.95
BIKE_ASCENT_FTP_CAP_SHORT = 1.05
BIKE_STEEP_DESCENT_GRADE = -0.04
BIKE_STEEP_DESCENT_MULT = 0.40
BIKE_DESCENT_MULT = 0.80

# --- heat & hydration ----------------------------------------------------------------
HEAT_PENALTY_ONSET_C = 15.0
HEAT_PENALTY_PER_C = 0.003
SWEAT_ONSET_C = 18.0
SWEAT_INCREASE_PER_5C = 0.15
MAX_SWEAT_L_HR = 3.0
TARGET_BODY_MASS_LOSS = 0.02
FLUID_REPLACEMENT_MIN = 0.4
FLUID_REPLACEMENT_MAX = 0.8

# --- fueling schedule ----------------------------------------------------------------
MIN_FUEL_INTERVAL_S = 15 * 60
MAX_FUEL_INTERVAL_S = 45 * 60
FLUID_ONLY_INTERVAL_S = 20 * 60
NO_FUEL_BEFORE_FINISH_S = 10 * 60
TERRAIN_SHIFT_WINDOW_S = 4 * 60
MIN_EVENT_SPACING_S = 5 * 60
FLUID_ROUNDING_ML = 50


def run_gap_factor(grade_frac: float) -> float:
    """Metabolic cost of running at ``grade_frac`` relative to flat (>1 = harder)."""
    i = float(np.clip(grade_frac, -0.45, 0.45))
    cost = 155.4 * i**5 - 30.4 * i**4 - 43.3 * i**3 + 46.3 * i**2 + 19.5 * i + 3.6
    ratio = cost / 3.6
    if i >= 0:
        return ratio
    damped = 1.0 + DOWNHILL_DAMPING * (ratio - 1.0)
    floor = DOWNHILL_RATIO_FLOOR + max(0.0, abs(i) - abs(STEEP_DOWNHILL_GRADE)) * 1.5
    return max(damped, floor)


def run_wind_factor(speed_ms: float, headwind_ms: float) -> float:
    """Pace multiplier from head/tailwind (tailwind benefit halved: body shape & cadence)."""
    if headwind_ms == 0 or speed_ms <= 0:
        return 1.0
    c0 = RUN_AIR_COST_FRACTION * (speed_ms / RUN_AIR_REF_SPEED) ** 2
    rel = speed_ms + headwind_ms
    extra = c0 * (math.copysign(rel * rel, rel) - speed_ms**2) / speed_ms**2
    if extra < 0:
        extra *= 0.5
    return max(0.9, 1.0 + extra)


def bike_power_for_speed(
    v: float,
    grade: float,
    mass: float,
    cda: float,
    crr: float,
    rho: float,
    headwind: float,
    eta: float,
) -> float:
    theta = math.atan(grade)
    v_air = v + headwind
    force = mass * G * (math.sin(theta) + crr * math.cos(theta)) + 0.5 * rho * cda * v_air * abs(v_air)
    return force * v / eta


def bike_speed_for_power(
    power: float,
    grade: float,
    mass: float,
    cda: float,
    crr: float,
    rho: float,
    headwind: float,
    eta: float,
    v_max: float,
) -> float:
    """Steady-state speed (m/s) for a given power via bisection, capped at ``v_max``."""
    lo, hi = 0.3, v_max
    if bike_power_for_speed(hi, grade, mass, cda, crr, rho, headwind, eta) <= power:
        return hi
    if bike_power_for_speed(lo, grade, mass, cda, crr, rho, headwind, eta) >= power:
        return lo
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bike_power_for_speed(mid, grade, mass, cda, crr, rho, headwind, eta) < power:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def sweat_rate_l_hr(base_l_hr: float, apparent_temp_c: float) -> float:
    """Baseline sweat rate +15 % for every 5 C of apparent temperature above 18 C."""
    excess = max(0.0, apparent_temp_c - SWEAT_ONSET_C)
    return min(MAX_SWEAT_L_HR, base_l_hr * (1.0 + SWEAT_INCREASE_PER_5C * excess / 5.0))


def heat_penalty_frac(apparent_temp_c: float, duration_s: float) -> float:
    excess = max(0.0, apparent_temp_c - HEAT_PENALTY_ONSET_C)
    scale = float(np.clip(duration_s / 3600.0 / 3.0, 0.33, 1.2))
    return excess * HEAT_PENALTY_PER_C * scale


def carb_rate_for_duration(target_g_h: float, duration_s: float) -> float:
    h = duration_s / 3600.0
    if h < 0.75:
        return 0.0
    if h < 1.25:
        return min(target_g_h, 30.0)
    if h < 2.5:
        return min(target_g_h, 60.0)
    return target_g_h


@dataclass
class _SegPlan:
    seg: Segment
    duration_s: float
    speed_ms: float
    effort: float
    wind: float
    power: float | None = None


class RaceEngine:
    """Builds a :class:`RaceBlueprint` from athlete, course segments and weather."""

    def __init__(
        self,
        athlete: AthleteProfile,
        segments: Sequence[Segment],
        weather: WeatherConditions,
        strategy: RaceStrategy | None = None,
    ) -> None:
        if not segments:
            raise ValueError("At least one course segment is required")
        self.athlete = athlete
        self.segments = list(segments)
        self.weather = weather
        self.strategy = strategy or RaceStrategy()
        self.total_m = self.segments[-1].end_distance_m
        self.warnings: list[str] = []

        t_end = weather.temperature_end_c if weather.temperature_end_c is not None else weather.temperature_c
        self.at_start = apparent_temperature_c(weather.temperature_c, weather.humidity_pct, weather.wind_speed_kmh)
        self.at_end = apparent_temperature_c(t_end, weather.humidity_pct, weather.wind_speed_kmh)
        self.at_mean = 0.5 * (self.at_start + self.at_end)
        self._headwinds = [self._headwind_ms(s) for s in self.segments]

    # ------------------------------------------------------------------ weather
    def _headwind_ms(self, seg: Segment) -> float:
        if self.weather.wind_direction_deg is None or self.weather.wind_speed_kmh == 0:
            return 0.0
        w = self.weather.wind_speed_kmh / 3.6
        theta = math.radians(self.weather.wind_direction_deg)
        return w * (math.sin(theta) * seg.heading_east + math.cos(theta) * seg.heading_north)

    def apparent_temp_at(self, elapsed_s: float, duration_s: float) -> float:
        frac = 0.0 if duration_s <= 0 else min(1.0, max(0.0, elapsed_s / duration_s))
        return self.at_start + (self.at_end - self.at_start) * frac

    def _ns_mult(self, seg: Segment) -> float:
        if self.strategy.pacing != "negative_split" or seg.terrain is TerrainType.ASCENT:
            return 1.0
        mid = 0.5 * (seg.start_distance_m + seg.end_distance_m)
        return 1.0 + NEGATIVE_SPLIT_DELTA if mid < self.total_m / 2 else 1.0 - NEGATIVE_SPLIT_DELTA

    # ------------------------------------------------------------------ running
    def _run_multipliers(self, base_pace: float) -> list[tuple[float, float, float]]:
        """(total multiplier, gap factor, wind factor) per segment for a base flat pace."""
        out = []
        for seg, hw in zip(self.segments, self._headwinds, strict=True):
            gap = run_gap_factor(seg.avg_grade_pct / 100.0)
            wind = run_wind_factor(1000.0 / (base_pace * gap), hw)
            asc = 1.0 + ASCENT_EFFORT_BUFFER if seg.terrain is TerrainType.ASCENT else 1.0
            out.append((gap * wind * asc * self._ns_mult(seg), gap, wind))
        return out

    def _run_time(self, base_pace: float) -> float:
        return sum(
            s.distance_m / 1000.0 * base_pace * m
            for s, (m, _, _) in zip(self.segments, self._run_multipliers(base_pace), strict=True)
        )

    def _plan_run(self) -> tuple[list[_SegPlan], float]:
        thr = float(self.athlete.threshold_pace_sec_km or 0)

        # Sustainable estimate: Riegel-extended threshold pace + heat penalty (fixed point).
        b = thr
        t_sust = self._run_time(b)
        for _ in range(30):
            b = thr * (t_sust / 3600.0) ** RIEGEL_EXPONENT * (1 + heat_penalty_frac(self.at_mean, t_sust))
            t_sust = self._run_time(b)

        target = self.strategy.target_finish_time_s
        if target is not None:
            b_sust = b
            b = target / (self._run_time(1.0) or 1.0)
            for _ in range(10):  # wind factor depends on speed -> refine
                b *= target / self._run_time(b)
            diff = (b_sust - b) / b_sust
            if diff > 0.02:
                self.warnings.append(
                    f"Target {format_duration(target)} needs a flat-equivalent pace "
                    f"{diff * 100:.1f}% faster than the model's sustainable estimate "
                    f"({format_duration(t_sust)}) in these conditions. Consider a more conservative goal."
                )

        plans = []
        for seg, (m, gap, wind) in zip(self.segments, self._run_multipliers(b), strict=True):
            pace = b * m
            plans.append(
                _SegPlan(
                    seg=seg,
                    duration_s=seg.distance_m / 1000.0 * pace,
                    speed_ms=1000.0 / pace,
                    effort=gap,
                    wind=wind,
                )
            )
        return plans, t_sust

    # ------------------------------------------------------------------ cycling
    def _bike_plan_for_power(self, p_race: float, ftp: float, long_race: bool) -> list[_SegPlan]:
        bike = self.strategy.bike
        mass = self.athlete.weight_kg + bike.bike_mass_kg
        cap = (BIKE_ASCENT_FTP_CAP_LONG if long_race else BIKE_ASCENT_FTP_CAP_SHORT) * ftp
        v_max = bike.max_descent_speed_kmh / 3.6
        t_mean = 0.5 * (self.weather.temperature_c + (self.weather.temperature_end_c or self.weather.temperature_c))

        plans = []
        for seg, hw in zip(self.segments, self._headwinds, strict=True):
            g = seg.avg_grade_pct / 100.0
            if seg.terrain is TerrainType.ASCENT:
                p = min(BIKE_ASCENT_POWER_MULT * p_race, max(p_race, cap))
            elif seg.terrain is TerrainType.DESCENT:
                p = p_race * (BIKE_STEEP_DESCENT_MULT if g < BIKE_STEEP_DESCENT_GRADE else BIKE_DESCENT_MULT)
            else:
                p = p_race
            p *= self._ns_mult(seg)
            rho = air_density(t_mean, 0.5 * (seg.start_elevation_m + seg.end_elevation_m))
            args = (mass, bike.cda_m2, bike.crr, rho)
            v = bike_speed_for_power(p, g, *args, hw, bike.drivetrain_efficiency, v_max)
            v_flat = bike_speed_for_power(p_race, 0.0, *args, 0.0, bike.drivetrain_efficiency, v_max)
            v_still = bike_speed_for_power(p, g, *args, 0.0, bike.drivetrain_efficiency, v_max)
            plans.append(
                _SegPlan(
                    seg=seg,
                    duration_s=seg.distance_m / v,
                    speed_ms=v,
                    effort=v_flat / v_still,
                    wind=v_still / v,
                    power=p,
                )
            )
        return plans

    def _plan_bike(self) -> tuple[list[_SegPlan], float]:
        ftp = float(self.athlete.threshold_power_watts or 0)

        def intensity(t: float) -> float:
            return float(np.clip(1.0 - 0.13 * math.log(max(t, 600.0) / 3600.0), 0.6, 1.05))

        def total(plans: list[_SegPlan]) -> float:
            return sum(p.duration_s for p in plans)

        t_sust = total(self._bike_plan_for_power(0.8 * ftp, ftp, True))
        for _ in range(30):
            p_sust = intensity(t_sust) * ftp * (1 - heat_penalty_frac(self.at_mean, t_sust))
            t_sust = total(self._bike_plan_for_power(p_sust, ftp, t_sust >= LONG_RACE_S))

        target = self.strategy.target_finish_time_s
        if target is None:
            return self._bike_plan_for_power(p_sust, ftp, t_sust >= LONG_RACE_S), t_sust

        long_race = target >= LONG_RACE_S
        lo, hi = 0.2 * ftp, 2.0 * ftp
        if total(self._bike_plan_for_power(hi, ftp, long_race)) > target:
            self.warnings.append("Target time is not reachable even at 200% FTP; planning at maximum.")
            p = hi
        else:
            for _ in range(60):
                mid = 0.5 * (lo + hi)
                if total(self._bike_plan_for_power(mid, ftp, long_race)) > target:
                    lo = mid
                else:
                    hi = mid
            p = 0.5 * (lo + hi)
        if p > p_sust * 1.02:
            self.warnings.append(
                f"Target {format_duration(target)} needs ~{p:.0f} W race power "
                f"(IF {p / ftp:.2f}) vs a sustainable ~{p_sust:.0f} W in these conditions."
            )
        return self._bike_plan_for_power(p, ftp, long_race), t_sust

    # ------------------------------------------------------------------ heart rate
    def _hr_targets(self, terrain: TerrainType, start_s: float, finish_s: float) -> tuple[int, int, int]:
        lthr = self.athlete.lthr
        h = finish_s / 3600.0
        center = float(np.clip(1.03 - 0.035 * h, 0.80, 1.03))
        ascent_cap = ZONE_3_4_BOUNDARY if finish_s >= LONG_RACE_S else 1.0
        heat_amp = 1.0 + max(0.0, self.at_mean - HEAT_PENALTY_ONSET_C) / 20.0
        drift = min(0.04, 0.012 * max(0.0, start_s / 3600.0 - 1.0) * heat_amp)

        if terrain is TerrainType.ASCENT:
            cap = ascent_cap
            high = cap
            low = min(center - 0.01, cap - 0.03)
        elif terrain is TerrainType.DESCENT:
            low, high = center - 0.05, center + 0.01 + drift
            cap = center + 0.03 + drift
        else:
            low, high = center - 0.02, center + 0.02 + drift
            cap = center + 0.03 + drift

        def bpm(f: float) -> int:
            return min(self.athlete.max_hr, round(f * lthr))

        return bpm(low), bpm(high), bpm(cap)

    # ------------------------------------------------------------------ timeline
    def _build_timeline(self, plans: list[_SegPlan]) -> tuple[np.ndarray, np.ndarray]:
        d = [plans[0].seg.start_distance_m]
        t = [0.0]
        for p in plans:
            d.append(p.seg.end_distance_m)
            t.append(t[-1] + p.duration_s)
        return np.asarray(d), np.asarray(t)

    def _splits(self, dist: np.ndarray, time: np.ndarray) -> list[Split]:
        step = 1000.0 if self.athlete.sport is Sport.RUN else 5000.0
        marks = list(np.arange(step, self.total_m, step))
        if not marks or self.total_m - marks[-1] > 1.0:
            marks.append(self.total_m)
        out, prev_t = [], 0.0
        for i, m in enumerate(marks, start=1):
            cum = float(np.interp(m, dist, time))
            out.append(
                Split(
                    index=i,
                    end_km=round(m / 1000.0, 3),
                    split_s=round(cum - prev_t, 1),
                    cumulative_s=round(cum, 1),
                    split_label=format_duration(cum - prev_t),
                    cumulative_label=format_duration(cum),
                )
            )
            prev_t = cum
        return out

    # ------------------------------------------------------------------ nutrition
    def _segment_at(self, distance_m: float) -> Segment:
        for s in self.segments:
            if distance_m < s.end_distance_m:
                return s
        return self.segments[-1]

    def _integrate(self, f: Callable[[float], float], t0: float, t1: float) -> float:
        """Integrate a per-hour rate over [t0, t1] seconds -> amount."""
        if t1 <= t0:
            return 0.0
        n = max(2, int((t1 - t0) // 30) + 1)
        ts = np.linspace(t0, t1, n)
        vals = np.array([f(float(x)) for x in ts])
        return float(np.trapezoid(vals, ts) / 3600.0)

    def _fuel_times(
        self,
        finish_s: float,
        carb_rate: float,
        fluid_rate_avg: float,
        dist: np.ndarray,
        time: np.ndarray,
    ) -> list[tuple[float, str]]:
        cutoff = finish_s - NO_FUEL_BEFORE_FINISH_S
        fp = self.strategy.fueling
        if self.strategy.aid_stations_km:
            times = sorted(
                float(np.interp(km * 1000.0, dist, time))
                for km in self.strategy.aid_stations_km
                if 0 < km * 1000.0 < self.total_m
            )
            return [(t, "") for t in times if MIN_EVENT_SPACING_S <= t <= cutoff]

        gel_need = max(0.0, carb_rate - fluid_rate_avg * fp.drink_carbs_g_per_l)
        interval = fp.gel_carbs_g / gel_need * 3600.0 if gel_need > 0 else FLUID_ONLY_INTERVAL_S
        interval = float(np.clip(interval, MIN_FUEL_INTERVAL_S, MAX_FUEL_INTERVAL_S))

        avoid = TerrainType.ASCENT if self.athlete.sport is Sport.RUN else TerrainType.DESCENT
        word = "climb" if avoid is TerrainType.ASCENT else "descent"
        out: list[tuple[float, str]] = []
        t, prev = interval, 0.0
        while t <= cutoff:
            when, note = t, ""
            seg = self._segment_at(float(np.interp(t, time, dist)))
            if seg.terrain is avoid and abs(seg.avg_grade_pct) > 3.0:
                seg_t0 = float(np.interp(seg.start_distance_m, dist, time)) - 30.0
                seg_t1 = float(np.interp(seg.end_distance_m, dist, time)) + 30.0
                if t - seg_t0 <= TERRAIN_SHIFT_WINDOW_S and seg_t0 - prev >= MIN_EVENT_SPACING_S:
                    when, note = seg_t0, f" (taken just before the {word})"
                elif seg_t1 - t <= TERRAIN_SHIFT_WINDOW_S and seg_t1 <= cutoff:
                    when, note = seg_t1, f" (taken just after the {word})"
            out.append((when, note))
            prev = when
            t = when + interval
        return out

    def _nutrition(
        self, finish_s: float, dist: np.ndarray, time: np.ndarray
    ) -> tuple[list[FuelingEvent], NutritionSummary, tuple[float, float]]:
        a, s, fp = self.athlete, self.strategy, self.strategy.fueling

        def sweat(t: float) -> float:
            return sweat_rate_l_hr(a.sweat_rate_l_hr, self.apparent_temp_at(t, finish_s))

        sweat_total = self._integrate(sweat, 0.0, finish_s)
        replace = (sweat_total - TARGET_BODY_MASS_LOSS * a.weight_kg) / sweat_total if sweat_total > 0 else 0.0
        replace = float(np.clip(replace, FLUID_REPLACEMENT_MIN, FLUID_REPLACEMENT_MAX))

        def fluid(t: float) -> float:
            return min(s.max_fluid_l_hr, replace * sweat(t))

        carb_rate = float(carb_rate_for_duration(a.target_carbs_per_hour, finish_s))
        if carb_rate < a.target_carbs_per_hour:
            self.warnings.append(
                f"Carb target reduced to {carb_rate:.0f} g/h for a ~{format_duration(finish_s)} effort "
                f"(more is unnecessary and raises GI risk)."
                if carb_rate > 0
                else "Race is under 45 min: no fueling needed; a carbohydrate mouth-rinse is optional."
            )
        if carb_rate > 90:
            self.warnings.append("Carb intake above 90 g/h requires a trained gut and 2:1 glucose:fructose products.")

        events: list[FuelingEvent] = []
        if finish_s >= 45 * 60:
            fluid_avg = self._integrate(fluid, 0.0, finish_s) / (finish_s / 3600.0)
            times = self._fuel_times(finish_s, carb_rate, fluid_avg, dist, time)
            prev, carb_carry, fluid_carry = 0.0, 0.0, 0.0
            for t, note in times:
                due_carbs = carb_rate * (t - prev) / 3600.0 + carb_carry
                fluid_l = self._integrate(fluid, prev, t) + fluid_carry
                fluid_ml = round(fluid_l * 1000 / FLUID_ROUNDING_ML) * FLUID_ROUNDING_ML
                fluid_carry = fluid_l - fluid_ml / 1000.0
                drink_carbs = fluid_ml / 1000.0 * fp.drink_carbs_g_per_l
                gels = max(0, math.floor((due_carbs - drink_carbs) / fp.gel_carbs_g + 0.5))
                carbs = gels * fp.gel_carbs_g + drink_carbs
                carb_carry = max(-fp.gel_carbs_g, due_carbs - carbs)
                prev = t
                if gels == 0 and fluid_ml == 0:
                    continue
                d_m = float(np.interp(t, time, dist))
                km = d_m / 1000.0
                items = []
                if gels:
                    items.append(f"{gels} {fp.gel_name}{'s' if gels > 1 else ''}")
                if fluid_ml:
                    items.append(f"{fluid_ml}ml {fp.drink_name}")
                events.append(
                    FuelingEvent(
                        elapsed_s=round(t, 1),
                        elapsed_label=format_duration(t),
                        clock_time=self.weather.start_time + timedelta(seconds=round(t)),
                        distance_km=round(km, 2),
                        terrain=self._segment_at(d_m).terrain,
                        gels=gels,
                        carbs_g=round(carbs, 1),
                        fluid_ml=fluid_ml,
                        sodium_mg=round(fluid_ml / 1000.0 * fp.drink_sodium_mg_per_l + gels * fp.gel_sodium_mg),
                        message=f"At km {km:.1f} [Approx. {format_duration(t)}], consume {' + '.join(items)}{note}",
                    )
                )

        fluid_total = sum(e.fluid_ml for e in events)
        sodium_total = sum(e.sodium_mg for e in events)
        mass_loss = (sweat_total - fluid_total / 1000.0) / a.weight_kg * 100.0
        if mass_loss > 2.5:
            self.warnings.append(
                f"Projected body-mass loss {mass_loss:.1f}% exceeds 2-3%: practise drinking more or "
                "pre-load fluids/sodium."
            )
        sodium_lost = sweat_total * a.sweat_sodium_mg_l
        if finish_s > 3 * 3600 and sodium_total < 0.3 * sodium_lost:
            self.warnings.append(
                f"Sodium intake ({sodium_total} mg) covers <30% of estimated sweat losses "
                f"({sodium_lost:.0f} mg); consider salt capsules or a stronger electrolyte mix."
            )

        hours = finish_s / 3600.0
        summary = NutritionSummary(
            carbs_per_hour_target=carb_rate,
            carbs_total_g=round(sum(e.carbs_g for e in events), 1),
            gels_total=sum(e.gels for e in events),
            fluid_total_ml=fluid_total,
            fluid_avg_l_hr=round(fluid_total / 1000.0 / hours, 3),
            sweat_loss_total_l=round(sweat_total, 3),
            sweat_rate_avg_l_hr=round(sweat_total / hours, 3),
            est_body_mass_loss_pct=round(mass_loss, 2),
            sodium_total_mg=sodium_total,
        )
        return events, summary, (sweat(0.0), sweat(finish_s))

    # ------------------------------------------------------------------ guidance
    def _guidance(self, p: _SegPlan, hr: tuple[int, int, int], pace_label: str | None) -> str:
        seg, (low, high, cap) = p.seg, hr
        km = seg.distance_m / 1000.0
        target = pace_label if pace_label else f"{p.power:.0f} W"
        if seg.terrain is TerrainType.ASCENT:
            tail = (
                "shorten stride and hike pitches if needed to protect glycogen."
                if self.athlete.sport is Sport.RUN
                else "spin a lower gear and stay seated."
            )
            return (
                f"Climb {seg.avg_grade_pct:+.1f}% for {km:.2f} km: go by effort, not speed (~{target}). "
                f"Hard HR cap {cap} bpm; if exceeded, ease off - {tail}"
            )
        if seg.terrain is TerrainType.DESCENT:
            tail = "quick, light cadence, no braking" if self.athlete.sport is Sport.RUN else "tuck and recover"
            return (
                f"Descent {seg.avg_grade_pct:+.1f}% for {km:.2f} km: ~{target}, {tail}. "
                f"Let HR settle to {low}-{high} bpm."
            )
        return f"Flat {km:.2f} km: settle at ~{target}; HR {low}-{high} bpm."

    # ------------------------------------------------------------------ entry point
    def generate(self) -> RaceBlueprint:
        self.warnings = []
        if self.athlete.sport is Sport.RUN:
            plans, t_sust = self._plan_run()
        else:
            plans, t_sust = self._plan_bike()

        dist, time = self._build_timeline(plans)
        finish = float(time[-1])

        seg_plans: list[SegmentPlan] = []
        for p, t0 in zip(plans, time[:-1], strict=True):
            hr = self._hr_targets(p.seg.terrain, float(t0), finish)
            pace = 1000.0 / p.speed_ms
            is_run = self.athlete.sport is Sport.RUN
            label = format_pace(pace) if is_run else None
            seg_plans.append(
                SegmentPlan(
                    segment_index=p.seg.index,
                    terrain=p.seg.terrain,
                    start_km=round(p.seg.start_distance_m / 1000.0, 3),
                    end_km=round(p.seg.end_distance_m / 1000.0, 3),
                    avg_grade_pct=p.seg.avg_grade_pct,
                    est_start_s=round(float(t0), 1),
                    est_duration_s=round(p.duration_s, 1),
                    target_speed_kmh=round(p.speed_ms * 3.6, 2),
                    target_pace_sec_km=round(pace, 1) if is_run else None,
                    target_pace_label=label,
                    target_power_watts=None if p.power is None else round(p.power),
                    effort_factor=round(p.effort, 4),
                    wind_factor=round(p.wind, 4),
                    hr_target_low=hr[0],
                    hr_target_high=hr[1],
                    hr_cap=hr[2],
                    guidance=self._guidance(p, hr, label),
                )
            )

        events, nutrition, (sw0, sw1) = self._nutrition(finish, dist, time)
        heat = heat_penalty_frac(self.at_mean, finish)
        if heat > 0.03:
            self.warnings.append(
                f"Heat stress: apparent temperature ~{self.at_mean:.0f} C costs roughly {heat * 100:.1f}% "
                "performance; start conservatively and use cooling at aid stations."
            )

        mean_elev = float(np.mean([0.5 * (s.start_elevation_m + s.end_elevation_m) for s in self.segments]))
        return RaceBlueprint(
            athlete=self.athlete,
            weather=self.weather,
            strategy=self.strategy,
            course=summarize_segments(self.segments),
            environment=EnvironmentSummary(
                apparent_temp_start_c=round(self.at_start, 1),
                apparent_temp_end_c=round(self.at_end, 1),
                heat_performance_penalty_pct=round(heat * 100, 2),
                sweat_rate_start_l_hr=round(sw0, 3),
                sweat_rate_end_l_hr=round(sw1, 3),
                air_density_kg_m3=round(air_density(self.weather.temperature_c, mean_elev), 4),
            ),
            predicted_finish_s=round(finish, 1),
            predicted_finish_label=format_duration(finish),
            sustainable_finish_s=round(t_sust, 1),
            segments=seg_plans,
            splits=self._splits(dist, time),
            fueling_events=events,
            nutrition=nutrition,
            warnings=list(self.warnings),
        )
