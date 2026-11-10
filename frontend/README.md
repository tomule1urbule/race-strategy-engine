# Race Blueprint web app

This is a mobile-first static web app. It runs the **same Python engine** in the browser through [Pyodide](https://pyodide.org), so no server is needed and the logic exists in one place.

- `index.html`, `style.css` and `app.js` are the UI: course upload, athlete and race-day inputs, a terrain-coloured elevation profile with numbered fuel stops, and Pacing, Fueling and Splits tabs.
- `backend/race_strategy/web.py` is the JSON bridge the page calls: `run(gpx_text, payload_json) -> json`.
- Pyodide 0.29.3 loads from jsDelivr together with `numpy`, `pandas` and `pydantic`. GPX parsing uses the stdlib fallback, because `gpxpy` isn't bundled.
- Inputs and the last course are kept in `localStorage`, so the form is pre-filled next time.

## Run locally

```bash
python scripts/build_site.py
python -m http.server 8000 -d _site   # open http://localhost:8000
```

## Deploy

`.github/workflows/pages.yml` builds `_site/` and deploys it to GitHub Pages on every push to `main`. In the repo's settings, Pages → Source must be set to **GitHub Actions**.

The first visit downloads about 15 MB (Python and the maths libraries) and takes 5–15 s. After that the browser caches it.

## Other clients

The FastAPI service (`POST /api/v1/blueprint`) returns the same `RaceBlueprint`. Its schema is in [`data/schema/race_blueprint.schema.json`](../data/schema/race_blueprint.schema.json).
