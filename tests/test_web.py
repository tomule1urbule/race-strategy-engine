from __future__ import annotations

import json

from race_strategy.web import MAX_PROFILE_POINTS, run

ATHLETE = {"weight_kg": 72, "threshold_pace_sec_km": 270, "max_hr": 190}
WEATHER = {"temperature_c": 18, "humidity_pct": 60, "start_time": "2026-11-15T08:00:00"}


def test_run_ok(hilly_half_gpx: str) -> None:
    out = json.loads(run(hilly_half_gpx, json.dumps({"athlete": ATHLETE, "weather": WEATHER})))
    assert out["ok"] is True
    assert out["blueprint"]["fueling_events"]
    assert 2 < len(out["profile"]) <= MAX_PROFILE_POINTS
    assert out["profile"][-1][0] == round(out["blueprint"]["course"]["total_distance_km"], 3)


def test_run_validation_errors_are_field_scoped(hilly_half_gpx: str) -> None:
    bad = {"athlete": {**ATHLETE, "max_hr": 999}, "weather": WEATHER}
    out = json.loads(run(hilly_half_gpx, json.dumps(bad)))
    assert out["ok"] is False
    assert out["errors"][0]["field"] == "athlete.max_hr"


def test_run_bad_gpx() -> None:
    out = json.loads(run("<gpx>", json.dumps({"athlete": ATHLETE, "weather": WEATHER})))
    assert out == {"ok": False, "errors": [{"field": "gpx", "message": out["errors"][0]["message"]}]}


def test_run_bad_json(hilly_half_gpx: str) -> None:
    assert json.loads(run(hilly_half_gpx, "{"))["errors"][0]["field"] == "payload"
