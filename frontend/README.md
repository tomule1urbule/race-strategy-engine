# Frontend (planned)

The UI is not built yet. This folder is reserved for it. The backend contract is stable and can be developed against today:

| What | Where |
|---|---|
| Endpoint | `POST /api/v1/blueprint` (multipart: `gpx_file` + `payload` JSON) |
| Request payload | `{ "athlete": AthleteProfile, "weather": WeatherConditions, "strategy"?: RaceStrategy }` |
| Response JSON Schema | [`data/schema/race_blueprint.schema.json`](../data/schema/race_blueprint.schema.json) |
| Example response | [`data/examples/blueprint_hilly_half.json`](../data/examples/blueprint_hilly_half.json) |

Generate TypeScript types from the schema with:

```bash
npx json-schema-to-typescript ../data/schema/race_blueprint.schema.json > src/types/blueprint.ts
```

Suggested views:
1. **Elevation profile.** Colour segments by `terrain`, overlay `fueling_events` as markers.
2. **Pacing table.** Show `segments[]` with pace or power, HR range and cap, and `guidance`.
3. **Splits.** Show `splits[]` (1 km for running, 5 km for cycling), printable as a wristband.
4. **Fueling timeline.** Show `fueling_events[].message` with `clock_time`.

CORS in `backend/race_strategy/api.py` already allows `localhost:5173` (Vite) and `localhost:3000`.
