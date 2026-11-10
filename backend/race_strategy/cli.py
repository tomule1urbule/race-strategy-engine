"""Command-line entry point: ``race-strategy course.gpx --athlete a.json --weather w.json``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import build_blueprint
from .models import AthleteProfile, RaceBlueprint, RaceStrategy, WeatherConditions


def _load(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def render_summary(bp: RaceBlueprint) -> str:
    lines = [
        f"Course: {bp.course.total_distance_km:.2f} km, +{bp.course.elevation_gain_m:.0f} m / "
        f"-{bp.course.elevation_loss_m:.0f} m, {bp.course.segment_count} segments",
        f"Predicted finish: {bp.predicted_finish_label}  "
        f"(apparent temp {bp.environment.apparent_temp_start_c:.0f}->{bp.environment.apparent_temp_end_c:.0f} C)",
        "",
        "SEGMENTS",
    ]
    for s in bp.segments:
        target = s.target_pace_label or f"{s.target_power_watts} W"
        lines.append(
            f"  {s.start_km:6.2f}-{s.end_km:6.2f} km  {s.terrain.value:<7} {s.avg_grade_pct:+5.1f}%  "
            f"{target:>10}  HR {s.hr_target_low}-{s.hr_target_high} (cap {s.hr_cap})"
        )
    lines += ["", "FUELING"]
    lines += [f"  {e.message}" for e in bp.fueling_events] or ["  (none)"]
    n = bp.nutrition
    lines += [
        "",
        f"Totals: {n.carbs_total_g:.0f} g carbs, {n.fluid_total_ml} ml fluid, {n.sodium_total_mg} mg sodium; "
        f"est. body-mass loss {n.est_body_mass_loss_pct:.1f}%",
    ]
    if bp.warnings:
        lines += ["", "WARNINGS"] + [f"  ! {w}" for w in bp.warnings]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate a race strategy & nutrition blueprint.")
    ap.add_argument("gpx", help="Path to the course .gpx file")
    ap.add_argument("--athlete", required=True, help="AthleteProfile JSON file")
    ap.add_argument("--weather", required=True, help="WeatherConditions JSON file")
    ap.add_argument("--strategy", help="RaceStrategy JSON file (optional)")
    ap.add_argument("-o", "--output", help="Write blueprint JSON here (default: stdout)")
    ap.add_argument("--summary", action="store_true", help="Print a human-readable summary")
    args = ap.parse_args(argv)

    athlete = AthleteProfile.model_validate(_load(args.athlete))
    weather = WeatherConditions.model_validate(_load(args.weather))
    strategy = RaceStrategy.model_validate(_load(args.strategy)) if args.strategy else None
    bp = build_blueprint(Path(args.gpx), athlete, weather, strategy)

    payload = bp.model_dump_json(indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    elif not args.summary:
        sys.stdout.write(payload + "\n")
    if args.summary:
        print(render_summary(bp))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
