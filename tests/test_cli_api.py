from __future__ import annotations

import json
from pathlib import Path

import pytest
from race_strategy import RaceBlueprint
from race_strategy.cli import main

ATHLETE = {"weight_kg": 72, "threshold_pace_sec_km": 270, "max_hr": 190}
WEATHER = {
    "temperature_c": 16,
    "humidity_pct": 65,
    "wind_speed_kmh": 10,
    "start_time": "2026-11-15T08:00:00",
}


@pytest.fixture
def inputs(tmp_path: Path, hilly_half_gpx: str) -> dict[str, Path]:
    files = {
        "gpx": tmp_path / "c.gpx",
        "athlete": tmp_path / "a.json",
        "weather": tmp_path / "w.json",
        "strategy": tmp_path / "s.json",
    }
    files["gpx"].write_text(hilly_half_gpx)
    files["athlete"].write_text(json.dumps(ATHLETE))
    files["weather"].write_text(json.dumps(WEATHER))
    files["strategy"].write_text(json.dumps({"target_finish_time_s": 6000}))
    return files


def test_cli_writes_valid_blueprint(
    inputs: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "bp.json"
    rc = main(
        [
            str(inputs["gpx"]),
            "--athlete",
            str(inputs["athlete"]),
            "--weather",
            str(inputs["weather"]),
            "--strategy",
            str(inputs["strategy"]),
            "-o",
            str(out),
            "--summary",
        ]
    )
    assert rc == 0
    bp = RaceBlueprint.model_validate_json(out.read_text())
    assert bp.predicted_finish_s == pytest.approx(6000, abs=1)
    assert "FUELING" in capsys.readouterr().out


def test_api_endpoint(inputs: dict[str, Path]) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient
    from race_strategy.api import app

    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    resp = client.post(
        "/api/v1/blueprint",
        files={"gpx_file": ("c.gpx", inputs["gpx"].read_bytes(), "application/gpx+xml")},
        data={"payload": json.dumps({"athlete": ATHLETE, "weather": WEATHER})},
    )
    assert resp.status_code == 200, resp.text
    assert RaceBlueprint.model_validate(resp.json()).fueling_events

    bad = client.post(
        "/api/v1/blueprint",
        files={"gpx_file": ("c.gpx", b"<gpx>", "application/gpx+xml")},
        data={"payload": json.dumps({"athlete": ATHLETE, "weather": WEATHER})},
    )
    assert bad.status_code == 400
