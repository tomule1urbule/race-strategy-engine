# Race Strategy Engine

**An algorithmic race strategy and nutrition generator for endurance runners and cyclists.**

You provide a course `.gpx`, an athlete profile and a weather forecast. You get back a validated `RaceBlueprint` JSON with:
- terrain-segmented pacing (pace for runners, power for cyclists)
- heart-rate caps and target ranges
- km or 5 km splits
- a timed fueling and hydration schedule, for example:

> `At km 12.5 [Approx. 58:00], consume 1 Isotonic Gel + 200ml Electrolytes`

```
GPX ──► GPXAnalyzer ──► [Segment…] ──► RaceEngine ◄── AthleteProfile, WeatherConditions, RaceStrategy
                                          │
                                          ▼
                                     RaceBlueprint (Pydantic v2 → JSON / FastAPI / CLI)
```

## Web app

**[tomule1urbule.github.io/race-strategy-engine](https://tomule1urbule.github.io/race-strategy-engine/)**

Upload a GPX, enter your threshold and the forecast, and get the plan on your phone. The page runs the Python engine in the browser with Pyodide, so no server is needed. See [`frontend/README.md`](frontend/README.md).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[api,dev]"

# CLI
race-strategy data/sample_courses/hilly_half_marathon.gpx \
  --athlete data/examples/athlete_runner.json \
  --weather data/examples/weather_warm.json \
  --strategy data/examples/strategy_half.json --summary

# API (http://localhost:8000/docs)
uvicorn race_strategy.api:app --reload --app-dir backend

# Tests, lint and types
pytest && ruff check backend tests && mypy backend/race_strategy
```

### Python usage

```python
from datetime import datetime
from race_strategy import AthleteProfile, WeatherConditions, RaceStrategy, build_blueprint

athlete = AthleteProfile(weight_kg=72, threshold_pace_sec_km=270, max_hr=190)
weather = WeatherConditions(temperature_c=21, temperature_end_c=25, humidity_pct=65,
                            wind_speed_kmh=12, wind_direction_deg=270,
                            start_time=datetime(2026, 11, 15, 8, 30))
bp = build_blueprint("course.gpx", athlete, weather, RaceStrategy(target_finish_time_s=6000))
print(bp.model_dump_json(indent=2))
```

## Repository layout

```
backend/race_strategy/
  models.py         Pydantic v2 inputs (AthleteProfile, WeatherConditions, RaceStrategy) and outputs (RaceBlueprint…)
  gpx_analyzer.py   GPX parsing, resampling, rolling-grade smoothing, terrain segmentation
  race_engine.py    Pacing, HR caps, heat/wind models, fueling and hydration timeline
  api.py            FastAPI service (POST /api/v1/blueprint)
  cli.py            `race-strategy` command
  synthetic.py      Ground-truth GPX generator (tests and samples)
  utils.py          Geodesy, apparent temperature, air density, formatting
  web.py            JSON bridge called by the browser app
tests/              pytest suite (parsing accuracy, segmentation, physiology, nutrition, API)
data/               Sample courses, example inputs, JSON Schema, example blueprint
frontend/           Mobile web app (Pyodide), deployed to GitHub Pages
scripts/            generate_samples.py (regenerates data/), build_site.py (builds the web app)
```

## Methodology

### Course analysis (`GPXAnalyzer`)
1. **Parse.** Uses `gpxpy`, or a stdlib XML fallback if it isn't installed. Accepts tracks or routes. Gaps in elevation are interpolated and stationary duplicate points are dropped.
2. **Resample.** Points go onto a uniform 10 m grid using haversine cumulative distance.
3. **Smooth.** Elevation gets a centred rolling mean (100 m). Grade is then a rolling mean of the step gradient, which works as a triangular kernel and suppresses GPS jitter.
4. **Segment.** Each step is labelled `Flat` (−1.5 % … +1.5 %), `Ascent` (> +1.5 %) or `Descent` (< −1.5 %). Blocks under 200 m merge into their most similar neighbour and are reclassified by net grade until the result is stable.
5. **Elevation gain and loss.** A 2 m hysteresis dead-band stops noise from inflating totals. For each segment, `gain − loss = Δelevation`.

### Pacing (`RaceEngine`)
| Element | Model |
|---|---|
| Running grade cost | Minetti et al. (2002) polynomial uphill. Downhill uses a 50 %-damped version floored at 0.85×, because field data shows runners can't capture the full treadmill downhill benefit. |
| Running prediction | Threshold pace extended to race duration with Riegel's exponent (1.06), solved as a fixed point. |
| Goal time | Base flat pace is solved so the course sums exactly to the target. A warning appears if the goal is more than 2 % beyond the sustainable estimate. |
| **Ascent protection** | Running: climbs are paced 3 % easier than even effort. Cycling: climbs are capped at min(110 % race power, 95 % FTP). In both sports a **hard HR cap at 95 % LTHR (Zone 3/4 boundary)** applies for races of 90 min or more. Flats and descents make up the time. |
| Cycling | Steady-state power balance (gravity, rolling resistance, aero drag) solved for speed by bisection. Air density depends on temperature and altitude. Descent speed is capped at 70 km/h by default. |
| Wind | Each segment's mean heading vector gives its headwind component. Running uses a drag share of about 4 % at 5.5 m/s, with tailwind benefit halved. Cycling applies wind directly in the aero term. |
| Heat | Steadman apparent temperature, with a ~0.3 %/°C performance penalty above 15 °C, scaled by duration. |
| HR | Target band centred on a duration-dependent %LTHR. Cardiac-drift allowance applies after 60 min on flats and descents only. Climbs keep the strict cap. |

### Nutrition and hydration
- **Sweat rate:** baseline × (1 + 0.15 × (T_apparent − 18 °C) / 5), modelled continuously and drifting with the forecast `temperature_end_c`.
- **Fluids:** replace 40–80 % of sweat, aiming for about 2 % body-mass loss, capped at `max_fluid_l_hr` (default 1.0 L/h). Volumes are rounded to 50 ml with carry-over, so totals stay exact.
- **Carbs** (Jeukendrup 2014): none under 45 min, ≤ 30 g/h under 75 min, ≤ 60 g/h under 2.5 h, the athlete's target beyond that. A gut-training warning appears above 90 g/h.
- **Schedule:** gels go at an interval derived from gel size and carb rate (15–45 min), or at `aid_stations_km` if those are given. Nothing is scheduled in the last 10 minutes. Runners' fuel stops move off steep climbs; cyclists' stops move off descents.
- **Sodium:** tracked from drink and gels. You get a warning if it covers less than 30 % of sweat losses in races over 3 h.

All coefficients are module-level constants in `race_engine.py`, so you can tune and test them.

> ⚠️ This is a planning aid, not medical advice. Rehearse any fueling strategy in training.

## Roadmap
- [x] Mobile web app (`frontend/`)
- [ ] FIT course and workout export, plus a PDF wristband
- [ ] Hourly weather from a forecast API (Open-Meteo)
- [ ] Personal grade-cost calibration from Garmin history
- [ ] Per-point GAP integration and a W′-balance model for cycling surges

## License
MIT
