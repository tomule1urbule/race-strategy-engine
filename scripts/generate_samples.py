"""Regenerate sample GPX courses, example inputs, the JSON Schema and an example blueprint.

Usage:  python scripts/generate_samples.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from race_strategy import (  # noqa: E402
    AthleteProfile,
    RaceBlueprint,
    RaceStrategy,
    WeatherConditions,
    build_blueprint,
)
from race_strategy.synthetic import make_gpx  # noqa: E402

DATA = ROOT / "data"

COURSES = {
    "hilly_half_marathon.gpx": (
        [(3000, 0.2), (1500, 3.5), (1200, -2.8), (4000, 0.0), (2500, 4.2), (2500, -4.0), (6397.5, -0.3)],
        "Hilly Half Marathon (synthetic)",
    ),
    "rolling_gran_fondo.gpx": (
        [(25000, 0.3), (9000, 5.5), (9000, -5.5), (20000, 0.0), (5000, 3.0), (5000, -3.0), (27000, 0.2)],
        "Rolling Gran Fondo 100k (synthetic)",
    ),
}

EXAMPLE_INPUTS = {
    "athlete_runner.json": {
        "sport": "run",
        "weight_kg": 72,
        "threshold_pace_sec_km": 270,
        "max_hr": 190,
        "threshold_hr": 172,
        "target_carbs_per_hour": 60,
        "sweat_rate_l_hr": 0.8,
    },
    "athlete_cyclist.json": {
        "sport": "bike",
        "weight_kg": 75,
        "threshold_power_watts": 280,
        "max_hr": 185,
        "threshold_hr": 165,
        "target_carbs_per_hour": 80,
        "sweat_rate_l_hr": 1.0,
    },
    "weather_warm.json": {
        "temperature_c": 21,
        "temperature_end_c": 25,
        "humidity_pct": 65,
        "wind_speed_kmh": 12,
        "wind_direction_deg": 270,
        "start_time": "2026-11-15T08:30:00+02:00",
    },
    "strategy_half.json": {"target_finish_time_s": 6000, "pacing": "even_effort"},
}


def main() -> None:
    (DATA / "sample_courses").mkdir(parents=True, exist_ok=True)
    (DATA / "examples").mkdir(parents=True, exist_ok=True)
    (DATA / "schema").mkdir(parents=True, exist_ok=True)

    for fname, (profile, name) in COURSES.items():
        gpx = make_gpx(profile, ele_noise_m=1.5, spacing_m=10, name=name)
        (DATA / "sample_courses" / fname).write_text(gpx, encoding="utf-8")

    for fname, payload in EXAMPLE_INPUTS.items():
        (DATA / "examples" / fname).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    schema = RaceBlueprint.model_json_schema(mode="serialization")
    (DATA / "schema" / "race_blueprint.schema.json").write_text(json.dumps(schema, indent=2) + "\n")

    bp = build_blueprint(
        DATA / "sample_courses" / "hilly_half_marathon.gpx",
        AthleteProfile.model_validate(EXAMPLE_INPUTS["athlete_runner.json"]),
        WeatherConditions.model_validate(EXAMPLE_INPUTS["weather_warm.json"]),
        RaceStrategy.model_validate(EXAMPLE_INPUTS["strategy_half.json"]),
    )
    out = bp.model_dump(mode="json")
    out["generated_at"] = "2026-11-10T00:00:00Z"  # stable diff
    (DATA / "examples" / "blueprint_hilly_half.json").write_text(json.dumps(out, indent=2) + "\n")
    print("Samples regenerated in", DATA)


if __name__ == "__main__":
    main()
