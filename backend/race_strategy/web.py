"""JSON-in / JSON-out bridge used by the browser frontend (Pyodide) and any thin client.

``run(gpx_text, payload_json)`` never raises: it returns a JSON string with either
``{"ok": true, "blueprint": {...}, "profile": [[km, ele], ...]}`` or
``{"ok": false, "errors": [{"field": ..., "message": ...}]}``.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from pydantic import ValidationError

from .gpx_analyzer import GPXAnalyzer, GPXParseError
from .models import AthleteProfile, RaceStrategy, WeatherConditions
from .race_engine import RaceEngine

MAX_PROFILE_POINTS = 400


def _profile(analysis_profile: Any) -> list[list[float]]:
    dist = analysis_profile["distance_m"].to_numpy() / 1000.0
    ele = analysis_profile["ele_smooth"].to_numpy()
    if len(dist) > MAX_PROFILE_POINTS:
        idx = np.linspace(0, len(dist) - 1, MAX_PROFILE_POINTS).round().astype(int)
        dist, ele = dist[idx], ele[idx]
    return [[round(float(d), 3), round(float(e), 1)] for d, e in zip(dist, ele, strict=True)]


def _errors_from_validation(exc: ValidationError, prefix: str) -> list[dict[str, str]]:
    return [{"field": ".".join([prefix, *(str(p) for p in err["loc"])]), "message": err["msg"]} for err in exc.errors()]


def run(gpx_text: str, payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"ok": False, "errors": [{"field": "payload", "message": str(exc)}]})

    errors: list[dict[str, str]] = []
    parsed: dict[str, Any] = {}
    for key, model in (("athlete", AthleteProfile), ("weather", WeatherConditions), ("strategy", RaceStrategy)):
        try:
            parsed[key] = (
                model.model_validate(payload.get(key) or {})
                if key == "strategy"
                else model.model_validate(payload.get(key))
            )
        except ValidationError as exc:
            errors.extend(_errors_from_validation(exc, key))
    if errors:
        return json.dumps({"ok": False, "errors": errors})

    try:
        analysis = GPXAnalyzer().analyze(gpx_text)
    except (GPXParseError, UnicodeDecodeError, ValueError) as exc:
        return json.dumps({"ok": False, "errors": [{"field": "gpx", "message": str(exc)}]})

    blueprint = RaceEngine(parsed["athlete"], analysis.segments, parsed["weather"], parsed["strategy"]).generate()
    return json.dumps(
        {"ok": True, "blueprint": blueprint.model_dump(mode="json"), "profile": _profile(analysis.profile)}
    )
