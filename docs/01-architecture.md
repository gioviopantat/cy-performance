# 01 · Architecture

## 1. Goals

1. **Analyse every ride** with power/HR metrics, climb detection, pacing, durability (decoupling,
   HR lag) and compare it against the athlete's history.
2. **Track long-term fitness** (CTL/ATL/TSB, eFTP, power-duration curve, W/kg, durability trend)
   and detect plateaus, overreaching, and under-training.
3. **Generate and maintain a training plan** on the intervals.icu calendar: a rolling 14-day
   horizon inside a goal-driven periodized season, re-planned daily from actual load + readiness.
4. **Run unattended** on a schedule with zero manual steps after one-time OAuth.
5. **Be safe to automate**: every write to intervals.icu is idempotent, diffed, reversible, and
   bounded by physiological guardrails.

## 2. Non-goals (v1)

- No web UI. The CLI, log files, and the intervals.icu calendar itself are the UI.
- No multi-athlete / coach mode. One athlete, one API key, one Strava OAuth token.
- No run/swim planning. Other sports are ingested only as **load** (so the PMC is correct).
- No device sync. intervals.icu already pushes workouts to Garmin/Zwift/Rouvy.

## 3. Principles

| Principle | Consequence |
|-----------|-------------|
| **intervals.icu is the canonical load ledger** | We read CTL/ATL/TSB, eFTP, zones, wellness from it. We do not maintain a competing PMC. Our own PMC exists only for simulation ("what if I do this plan"). |
| **intervals.icu is also the per-point stream source** | The Edge 850 and Rouvy are linked directly, so icu serves full streams and detected intervals. Strava is secondary: segments, PRs, and a stream fallback. The stream store is source-agnostic. See [ADR-0003](adr/0003-source-of-truth.md). |
| **Deterministic planner, optional LLM narrator** | Load targets, workout selection, and all numbers come from rules + a small optimizer that can be unit-tested and replayed. An LLM may only (a) write the human-readable coach note, (b) *propose* adjustments that must pass the same guardrails. See [ADR-0004](adr/0004-deterministic-planner.md). |
| **Raw first, derive later** | Every API payload is stored verbatim (JSON) next to the typed columns. Analysis is a pure function of stored data and can be re-run after any algorithm change. |
| **Idempotent, diff-first writes** | Calendar events carry our `external_id`. Publishing = compute desired state → diff against what intervals.icu has → upsert/delete only our own events. `--dry-run` is the default until the athlete flips `CYP_PLAN_MODE=apply`. |
| **Local-first** | SQLite + Parquet in `data/`. Nothing requires a server. A VPS/container deployment is supported, not required. |

## 4. System context

```
                 ┌──────────────────────┐          ┌─────────────────────────┐
  Garmin Edge ──►│        Strava        │          │      intervals.icu      │◄── Garmin (direct, recommended)
  Rouvy       ──►│  OAuth2 · REST v3    │──sync───►│  API key · REST v1      │◄── wellness (HRV/sleep/RHR)
                 │  webhooks (optional) │          │  calendar · PMC · eFTP  │──► Garmin / Zwift / Rouvy (workout push)
                 └──────────┬───────────┘          └─────────────┬───────────┘
                            │ activities, streams,                │ activities, intervals, wellness,
                            │ segment efforts, zones              │ power curves, sport settings, events
                            ▼                                     ▼
                 ┌──────────────────────────────────────────────────────────┐
                 │                      cy-performance                      │
                 │  ingest ─► store ─► analysis ─► planning ─► publish ─────┼──► PUT/POST events (our external_id only)
                 │                        │                                 │
                 │                        └─► reports (md/html), logs, llm  │
                 └──────────────────────────────────────────────────────────┘
                            ▲
                 launchd / cron / `cyp serve` scheduler
```

## 5. Components (Python package `cyp`)

### 5.1 `core/`
Domain types shared by everything else. Pydantic v2 models, no I/O.
- `athlete.py` — `AthleteProfile` (ftp, weight, lthr, max_hr, zones, w_prime, p_max, cp), `ZoneModel`
- `activity.py` — `Activity` (unified), `Stream` names/units, `Interval`
- `load.py` — `TrainingLoad`, `FitnessState` (ctl, atl, tsb, ramp_rate), `Readiness`
- `plan.py` — `Season`, `Block`, `WeekTemplate`, `PlannedWorkout`, `WorkoutStep`
- `units.py`, `timeutil.py` (all timestamps UTC + local; athlete tz `Asia/Taipei`)
- `errors.py`

### 5.2 `ingest/`
Thin, rate-limit-aware HTTP clients + sync jobs that write raw payloads to the store.
- `strava/client.py` — OAuth refresh, 429 handling, 15-min window budgeting. Port of
  `strava-analyis/strava_analysis/api/client.py`.
- `strava/sync.py` — incremental cursor (`after=` epoch), detail → streams → efforts → zones.
- `strava/webhook.py` — optional; validates `hub.challenge`, enqueues `object_id` (used only by `cyp serve`).
- `intervals/client.py` — Basic auth `API_KEY:<key>`, `athlete/0`, 429 + `Retry-After`.
- `intervals/sync.py` — activities (summary + `icu_*` fields + intervals), wellness, power curves,
  sport settings, athlete, events (read-back of our published plan and the athlete's own races/notes).
- `matcher.py` — joins Strava ↔ intervals.icu activities (`external_id`/`strava_id` when present,
  else start time ±120 s + duration ±2%).

### 5.3 `store/`
- `models.py` — SQLAlchemy 2.0 declarative models (see [03-data-model](03-data-model.md)).
- `migrations/` — Alembic.
- `streams.py` — Parquet-per-activity stream store (`data/streams/{activity_id}.parquet`),
  columns = stream names at 1 Hz after resampling; original resolution kept as JSON if not 1 Hz.
- `repo/` — repositories (`ActivityRepo`, `WellnessRepo`, `PlanRepo`, …). Only module that
  talks SQL; analysis/planning receive DataFrames and domain models.

### 5.4 `analysis/`
Pure functions over stored data. Details in [04-analysis-engine](04-analysis-engine.md).
- `ride/` — NP, IF, TSS, power curve, climbs, HR drift/decoupling, HR lag, pacing variability,
  time in zone, W′bal, estimated power for no-power rides.
- `longitudinal/` — PMC replay/simulation, eFTP tracking, power-duration model (CP/W′),
  durability trend, segment PR timeline, polarization index, monotony/strain, ACWR.
- `readiness.py` — fuses wellness (HRV, RHR, sleep) + yesterday's internal/external load response
  into a `Readiness` score with a `status ∈ {FRESH, NORMAL, BLUNTED, OVERREACHED}`.
- `compliance.py` — planned vs executed workout (paired via intervals.icu `paired_event_id`).

### 5.5 `planning/`
Details in [05-training-engine](05-training-engine.md).
- `athlete_model.py` — builds `AthleteProfile` from sport settings + eFTP + power curve + weight.
- `season.py` — goal events → macro phases → mesocycles (3:1 / 2:1) → weekly load targets.
- `library/` — workout templates (YAML) parameterised by %FTP/zone/duration, with intent tags
  (`endurance`, `tempo`, `sweetspot`, `threshold`, `vo2`, `anaerobic`, `recovery`, `climb_specific`).
- `planner.py` — fills a rolling horizon: weekly TSS target → day allocation by availability →
  template selection by phase + limiter + readiness → concrete steps.
- `guardrails.py` — hard constraints (max ramp rate, min rest, max HIT days, TSB floor, taper rules).
- `renderer.py` — `PlannedWorkout` → intervals.icu workout text (see 02 §4.6) and `EventEx` payload.

### 5.6 `publish/`
- `intervals_calendar.py` — desired-state → diff → `POST /events/bulk?upsert=true` (external_id
  `cyp:{plan_id}:{date}:{slot}`) and `PUT /events/bulk-delete` for stale ones. Never touches
  events without our prefix. Writes `publish_log`.
- `report.py` — Markdown/HTML daily + weekly report (Jinja2), optionally posted as an
  intervals.icu NOTE event and/or Strava activity description footer (the existing `RIDE.LOG`
  block the athlete already uses).

### 5.7 `jobs/`
Orchestration only; each stage is independently runnable and resumable.
- `sync.py` — `run_sync()`: intervals (all stages or month-paged backfill) → matcher → Strava
  (if enabled) → matcher; each stage under `job_run`, one umbrella `sync`/`backfill` row.
  Backs `cyp sync` and `cyp backfill --days N`.
- `daily.py` — sync → analyze → readiness → replan horizon → publish → report
- `weekly.py` — FTP/eFTP review, block progression, next-week template, weekly report
- (backfill lives in `sync.py`; history import is bounded by the Strava rate budget and
  resumable via `sync_cursors`)
- `scheduler.py` — APScheduler wiring for `cyp serve`; launchd/cron call `cyp daily` directly

### 5.8 `llm/` (optional, off by default)
- `narrator.py` — turns the structured daily/weekly result into a coach note in zh-TW/English.
- `reviewer.py` — given the proposed plan + athlete state, may return *suggested edits* as
  structured JSON; edits re-enter `guardrails` and are logged as `plan_revisions.source='llm'`.
- Prompt inputs are aggregates and our own metrics, never raw Strava streams (see 02 §3.3).

### 5.9 `dataset.py`, `services/`, `cli/` and `api/`
- `dataset.py` — read-only, column-selected snapshot of every model input with a one-query
  `data_version` and a process-wide cache; all recompute paths start from it.
- `services/` — the operations (meta, fitness, activities, FTP status / what-if / accept,
  readiness, plan preview / commit / season, trends, explain, reports, pipelines, background
  jobs) with pydantic I/O from `schemas.py`. The CLI and the API both call these, never the
  models directly.
- `cli/` — typer commands grouped by domain (core, sync, analysis, planning, reports, dev,
  serve).
- `api/` — FastAPI `create_app(AppContext)`: `/healthz` and `/v1/*` (reads, what-ifs, explicit
  writes, jobs) with ETag caching and optional bearer token. Contract and budgets:
  [08-api.md](08-api.md).

## 6. Runtime and deployment

**Default (M1–M4): macOS launchd**, same pattern as `strava-analyis/launchd`, running
`cyp daily` at 05:30 local and `cyp weekly` on Monday 05:45. Polling, no public endpoint.

**Optional (M5+): `cyp serve`** in a container on a small VPS/Fly.io with APScheduler + Strava
webhooks for near-real-time ingest. Same code, same SQLite file on a persistent volume
(Litestream for backup). Postgres is a config change thanks to SQLAlchemy, not a rewrite.

## 7. Configuration

`pydantic-settings` reading `.env` (see `.env.example`). Secrets (OAuth tokens) live in
`data/tokens/*.json`, mode 600, never in the DB or git. All planner knobs (ramp caps, weekly
availability, goal events) live in `config/athlete.yaml`, versioned in git because they are
the athlete's intent, not secrets.

## 8. Observability

- `structlog` JSON lines to `data/logs/cyp.jsonl` + human console.
- `job_runs` table: one row per stage run with status, counts, rate-limit snapshot, duration.
- `cyp doctor` prints token expiry, remaining Strava budget, last successful sync per source,
  schema version, and whether the last publish diff was applied.
- Every plan change is a `plan_revisions` row with before/after JSON and a reason string.

## 9. Testing strategy

- Unit: metrics against hand-computed fixtures (ported tests from `strava-analyis`).
- Contract: recorded API responses (`tests/fixtures/{strava,intervals}/*.json`) via `respx`.
- Planner: **simulation harness** with a Banister impulse-response athlete; assert plan
  converges to CTL target without breaching guardrails over a 16-week synthetic season.
- Replay: run the planner against the athlete's real 2026 history and inspect the plan it
  *would* have produced each week (regression snapshot).

## 10. Athlete answers (2026-10-02)

1. Garmin Edge 850 **is** linked directly to intervals.icu → icu is the stream source.
2. No race. Goal: **FTP ≥ 300 W within 6 months** (icu FTP 265 W on 2026-10-02, Strava still 250; 64 kg → 4.7 W/kg). Season type
   `ftp_target`, target date 2027-04-02. See [05 §2.4](05-training-engine.md).
3. **≤ 15 h / week**, outdoor strongly preferred; indoor only as weather fallback and for tests.
4. Strength and yoga are **not** planned; they are ingested as load only.
