"""FastAPI service exposing the generator to a frontend.

Run with:  ``uvicorn race_strategy.api:app --reload --app-dir backend``
Requires the ``api`` extra: ``pip install -e ".[api]"``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, ValidationError

from . import __version__, build_blueprint
from .gpx_analyzer import GPXParseError
from .models import AthleteProfile, RaceBlueprint, RaceStrategy, WeatherConditions

MAX_GPX_BYTES = 20 * 1024 * 1024

app = FastAPI(title="Race Strategy Engine", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class BlueprintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    athlete: AthleteProfile
    weather: WeatherConditions
    strategy: RaceStrategy | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/api/v1/blueprint", response_model=RaceBlueprint)
async def create_blueprint(
    gpx_file: Annotated[UploadFile, File(description="Course .gpx")],
    payload: Annotated[str, Form(description="JSON BlueprintRequest")],
) -> RaceBlueprint:
    try:
        req = BlueprintRequest.model_validate_json(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    data = await gpx_file.read(MAX_GPX_BYTES + 1)
    if len(data) > MAX_GPX_BYTES:
        raise HTTPException(status_code=413, detail="GPX file too large")
    try:
        return build_blueprint(data, req.athlete, req.weather, req.strategy)
    except GPXParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
