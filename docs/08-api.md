# 08 · Backend layering and HTTP API (frontend contract)

The frontend (next milestone) talks only to the HTTP API. Everything it needs is served from
the same code the CLI uses, so a number shown in the UI is the number the reports and the
planner used.

## 1. Layers

```
ingest/ (icu, Strava)  ──►  store/ (SQLite + Parquet)
                                   │
                    dataset.py  (column-selected snapshot + version cache)
                                   │
   analysis/ (pure models)   planning/ (pure planner)   reports/ (facts + templates)
                                   │
                         services/ (operations, pydantic I/O = schemas.py)
                          │                         │
                    cli/ (typer)               api/ (FastAPI)
```

- **`cyp/dataset.py`** — one read-only snapshot of every model input (activities + metrics,
  daily loads, wellness, fitness, readiness, events, settings, planned workouts). Loaded with
  column selects (no raw JSON payloads). `data_version()` is one aggregate query;
  `DatasetCache` reloads only when it changes. All recompute paths start here.
- **Pure models** — `analysis/longitudinal/{pmc,pdc,ftp,durability,tid,climbs,limiters}.py`,
  `analysis/readiness.py`, `planning/{season,planner,guardrails,adapt}.py`,
  `planning/job.plan_horizon`. No I/O; same input → same output; every verdict carries an
  `Explanation`.
- **Jobs / persistence** — `analysis/longitudinal/run.build_trends`, `analysis/readiness_job`,
  `planning/job.build_plan`: snapshot → pure compute → bulk upsert.
- **Services** (`cyp/services/*`) — what the UI and CLI can *do*. Inputs/outputs are models in
  `cyp/schemas.py`. `AppContext` holds settings, engine, stores, the athlete config path, a
  write lock and (for tests/demos) a frozen clock.

## 2. Performance budgets (synthetic athlete: 304 rides, 400 days; `cyp dev bench`)

| Path | Before refactor | Now (warm) | Budget |
|------|-----------------|------------|--------|
| FTP status / what-if (`GET /v1/ftp`, `POST /v1/ftp/what-if`) | part of trends (~800 ms) | ~17 ms | < 100 ms |
| 14-day plan preview (`POST /v1/plan/preview`) | ~255 ms (YAML parse each run) | ~10 ms | < 300 ms |
| Trends recompute (PMC, FTP, durability, TID, climbs, limiters + writes) | ~800 ms | ~80 ms | < 500 ms |
| Readiness, 7 days | ~230 ms | ~16 ms | < 100 ms |

What made the difference: the snapshot + version cache (no ORM objects, no raw JSON), the
template library cached by file mtimes and memoised `resolve()`, per-ride durability stored in
`activity_metrics.durability` (no Parquet reads in trends), vectorised PMC safety metrics and
a rides × durations matrix for rolling CP fits, bulk upserts instead of per-row `session.get`.
`tests/api/test_api.py::test_recompute_is_fast_when_cached` guards the interactive budgets.

## 3. Running

```bash
uv run cyp dev seed --days 400      # demo data in an empty DB (no API keys needed)
uv run cyp serve                    # http://127.0.0.1:8765, docs at /docs
uv run cyp dev openapi              # writes docs/api/openapi.json for client generation
```
`CYP_API_CORS_ORIGINS` (default Vite dev ports 5173) and `CYP_API_TOKEN` (when set, every
`/v1` request needs `Authorization: Bearer <token>`; `/healthz` stays open) are server
settings: read from the environment / root `.env`, never from a profile. `cyp serve` refuses
to bind beyond loopback without a token; on loopback without one it answers only
`Host: localhost | 127.0.0.1 | ::1` (DNS-rebinding guard). Write endpoints: ADR-0009.

## 4. Endpoints (`/v1`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/meta` | athlete, FTP, W/kg, season week/phase, data version, last job runs |
| GET | `/fitness?start&end` | daily load + CTL/ATL/TSB (icu ledger, else our replay) + planned load |
| GET | `/season` | 26-week skeleton with targets, tests, checkpoints |
| GET | `/activities?start&end&sport&rides_only&classification&offset&limit` | list (newest first) |
| GET | `/activities/{id}` | stored per-ride analysis + Explanation |
| GET | `/activities/{id}/streams?resolution_s` | chart arrays, bucket-averaged (≤ 5 000 points) |
| GET | `/ftp?as_of` | FTP evidence: MMP, CP 2p/3p vs icu, estimate series, proposal |
| POST | `/ftp/what-if` | same against another FTP (nothing stored) |
| POST | `/ftp/accept` | the athlete's explicit decision (manual settings row; icu untouched) |
| GET | `/readiness?start&end`, `/readiness/{date}` | verdicts with components, weights, rules |
| POST | `/readiness/recompute` | `{"dates": [...]}` |
| GET | `/plan?start&days` | stored proposals, rendered (steps + icu workout text) |
| POST | `/plan/preview` | what-if horizon: weekday minutes, days off, indoor days, readiness, bias, CTL |
| POST | `/plan/commit` | store proposals (never writes intervals.icu); 409 while this profile's autopilot runs |
| GET / POST | `/trends/latest`, `/trends/recompute` | longitudinal report |
| GET | `/explain/{key}` | any persisted Explanation (`ride:42`, `readiness.2026-10-06`, `plan.day.…`, `ftp.proposal`) |
| GET / POST | `/reports/{daily\|weekly}`, `/reports/{kind}/{stem}` | Markdown + facts |
| GET | `/glossary`, `/glossary/{term}` | zh-TW model explanations (Markdown) |
| POST / GET | `/jobs/{sync\|analyze\|daily\|weekly}`, `/jobs/{id}` | background work, one at a time, under the profile's autopilot lock |
| GET | `/jobs` | this profile's jobs started by this server process |

Conventions: ISO dates; seconds unless `_min`; W; TSS units; `*_zh` = display text. Errors:
`{"error": "no_data"|"not_found"|"config"|"analysis"|"unauthorized", "message": ...}` with
409 / 404 / 503 / 422 / 401; validation errors are FastAPI's 422.

## 5. Profiles and the web UI (spec [web-ui](specs/web-ui.md))

- **Profile selection**: every `/v1` request may send `X-CYP-Profile: <slug>`; without it the
  server's default profile answers. `api/registry.ProfileRegistry` keeps one `AppContext` per
  profile (same isolation as `cyp --profile`); ETags include the profile. Unknown profile → 404.
- **Endpoints added for the UI**:
  - `GET /v1/profiles`: status rows with `calendar_write_allowed`;
  - `POST /v1/autopilot` `{write, confirm}`: a background job running `jobs.runner.run_profile`.
    `write=true` → 422 without `confirm`, 409 in propose mode, 403 when `api.calendar_write` is
    off; then the usual write guard;
  - `GET /v1/autopilot/runs`: reports saved in `data/reports/autopilot/*.json`;
  - `GET /v1/activities/{id}/ride-log`: WORKOUT + RIDE.LOG text (`services/ride_log.py`);
  - `GET|POST /v1/activities/{id}/feedback` `{rpe, feel}`: post-ride feeling (web answer wins
    over intervals.icu); a POST recomputes the next day's readiness;
  - `POST /v1/activities/{id}/ride-log` `{text}`: save an edited RIDE.LOG (`""` restores the
    generated one); the response's `source` is `saved` or `generated`;
  - `POST /v1/activities/{id}/strava-description` `{text, confirm}`: preview, or write to Strava
    with `confirm=true` (409 when a gate refuses: flag, scope, owner; text ≤ 10 000 chars).
- **UI** (`web/`):
  - Vite + React + TypeScript strict + TanStack Query;
  - types generated from `docs/api/openapi.json` (`npm --prefix web run gen:api`);
  - `uv run poe check` fails when the OpenAPI file or the generated types are stale
    (`tests/api/test_web_api.py`, `npm run check`);
  - `npm --prefix web run build` → `web/dist`, served by `cyp serve` at `/`;
  - `npm --prefix web run dev` (port 5173) proxies `/v1` to :8765.

## 6. Caching contract

Every successful `GET /v1/*` returns an `ETag` derived from the data version, the latest
`job_runs` id, the report files' and `athlete.yaml` mtimes, the local date, the profile, path
and query. Never cached (no ETag): `/jobs`, `/autopilot`, `/profiles`, `/ride-log` and
`/feedback`, which read files or live state. Send `If-None-Match` and get `304` when nothing
changed — the UI can poll `/v1/meta` every few seconds for free and refetch views only when
`data_version` moves (e.g. after a job finishes).

## 7. Interaction model the API is built for

- **Sliders / toggles → `POST /plan/preview`** (≈ 10 ms): change weekday availability, mark a
  day off or indoor, simulate a REST/EASY morning, override CTL; the response includes the
  14 days with workout profiles (`steps`), week targets vs planned, guardrail repairs and the
  projected CTL/ATL/TSB, all with Explanations. `POST /plan/commit` stores the chosen state.
- **FTP panel → `GET /ftp`, `POST /ftp/what-if`, `POST /ftp/accept`**: evidence first, the
  athlete decides; nothing auto-changes FTP.
- **Every number has a "why"**: `explanation` on rides, readiness, plan days, repairs, week
  targets; `/explain/{key}` and `/glossary/{term}` for drill-down.
- **Long work is a job**: start, poll, then refetch on `data_version` change.
