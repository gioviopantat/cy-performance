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
- No multi-tenant service / coach mode. A few athletes on one host run as isolated profiles
  ([ADR-0006](adr/0006-profiles-and-tenancy.md)); a shared DB with sign-up is deferred.
- No run/swim planning. Other sports are ingested only as **load** (so the PMC is correct).
- No device sync. intervals.icu already pushes workouts to Garmin/Zwift/Rouvy.

## 3. Principles

| Principle | Consequence |
|-----------|-------------|
| **intervals.icu is the canonical load ledger** | We read CTL/ATL/TSB, eFTP, zones, wellness from it. We do not maintain a competing PMC. Our own PMC exists only for simulation ("what if I do this plan"). |
| **intervals.icu is also the per-point stream source** | The head unit and Rouvy are linked directly, so icu serves full streams and detected intervals. Strava is secondary: segments, PRs, and a stream fallback. The stream store is source-agnostic. See [ADR-0003](adr/0003-source-of-truth.md). |
| **Deterministic planner, optional LLM narrator** | Load targets, workout selection, and all numbers come from rules + a small optimizer that can be unit-tested and replayed. An LLM may only (a) write the human-readable coach note, (b) *propose* adjustments that must pass the same guardrails. See [ADR-0004](adr/0004-deterministic-planner.md). |
| **Raw first, derive later** | Every API payload is stored verbatim (JSON) next to the typed columns. Analysis is a pure function of stored data and can be re-run after any algorithm change. |
| **Idempotent, diff-first writes** | Calendar events carry our `external_id`. Publishing = compute desired state → diff against what intervals.icu has → upsert/delete only our own events. Nothing is written unless the profile is promoted to `athlete.yaml` `planner.mode: apply` (or `plan --apply --confirm-write`), and the write guard passes (ADR-0006). |
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
- `features.py` — the feature-flag registry and resolver ([ADR-0008](adr/0008-feature-flags.md),
  [09-features](09-features.md)). Pure: no I/O, no settings.
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
- `compliance.py` *(planned)* — planned vs executed workout (paired via intervals.icu `paired_event_id`).

### 5.5 `planning/`
Details in [05-training-engine](05-training-engine.md).
- `athlete_model.py` *(planned; today `dataset.py` + `analysis/longitudinal`)* — builds `AthleteProfile` from sport settings + eFTP + power curve + weight.
- `season.py` — goal events → macro phases → mesocycles (3:1 / 2:1) → weekly load targets;
  long-ride progression for a distance goal.
- `athlete_rules.py` — onboarding answers (birth year, health screen) → effective planner
  config; applied in `AppContext.athlete_config()` (docs/05 §2.5).
- `menus.py` — goal-emphasis workout menus, intensity bias, the masters' light hard day
  (docs/05 §2.6).
- `library/` — workout templates (YAML) parameterised by %FTP/zone/duration, with intent tags
  (`endurance`, `tempo`, `sweetspot`, `threshold`, `vo2`, `anaerobic`, `recovery`, `climb_specific`).
- `planner.py` — fills a rolling horizon: weekly TSS target → day allocation by availability →
  template selection by phase + limiter + readiness → concrete steps.
- `guardrails.py` — hard constraints (max ramp rate, min rest, max HIT days, TSB floor, taper rules).
- `renderer.py` — `PlannedWorkout` → intervals.icu workout text (see 02 §4.6) and `EventEx` payload.

### 5.6 `publish/`
- `events.py` — `EventSpec`, the icu `EventEx` payload, `is_ours`. The `external_id` scheme
  `cyp:{season}:{date}:{slot}` lives in `core/ids.py` because the planner stores it too.
- `plan_events.py` — a `PlanRun` as `EventSpec`s: rendered workout text, explanation footer,
  suggested climb. Lives here so `planning` never imports `publish`.
- `diff.py` — desired state vs live calendar → create / update / delete / noop.
- `publisher.py` — applies the diff via `POST /events/bulk` (upsert or uid mode), reads back
  `icu_training_load`, writes `publish_log`. Never touches events without our prefix.
- `spike.py` — one-off probe of which upsert mode works with this API key.

### 5.7 `jobs/` and `services/`
- `jobs/sync.py` — `run_sync()`: intervals (all stages or month-paged backfill) → matcher →
  Strava (if enabled) → matcher. Backs `cyp sync` and `cyp backfill --days N`.
- `store/runs.py` — `job_run()` records every stage in `job_runs`. It sits in `store` so that
  `ingest` and `analysis` can use it without depending on the orchestration layer.
- `services/pipeline.py` — the `cyp daily` / `cyp weekly` composites (analyze → trends →
  readiness → report), shared with API jobs.
- `services/publish.py` — `publish_plan()`: the only path that writes the calendar (CLI and
  autopilot). Gates: caller asks, `publish.calendar` on, upsert mode known, **write guard**
  (the API key's owner = the profile's `icu_athlete_id`).
- `services/autopilot.py` — `run_autopilot()`: one profile's morning, stage by stage
  (sync → analyze → report.daily → report.weekly → plan → publish), each behind a feature flag,
  failures isolated (an invalid athlete.yaml is a failed `config` stage), one `job_runs` row
  `autopilot`. Publishing never writes a plan that needs review; a failed write fails the stage.
- `services/profiles.py` — onboarding (`fetch_icu_athlete`, `onboard`, `backfill`), legacy
  `plan_adopt`/`adopt_legacy`, and `profile_status` (last run, Garmin push) for the CLI and a
  future sign-up UI.
- `jobs/runner.py` — `run_profile()` (lock → migrate → autopilot → notify; always returns a
  report) and `run_all()` (`cyp run --all`: one subprocess per profile, notifies for crashes).
- `jobs/sync.py` also checks, before any intervals.icu sync of a profile, that the API key
  belongs to the profile's athlete (otherwise their rides would enter this DB).
- `jobs/launchd.py` — the macOS launch agent (`cyp schedule install`); `jobs/notify.py` —
  macOS notification on failure (`notify.macos`).

### 5.7.1 Layering (enforced by `tests/test_layering.py`)
A package may import only from its own layer or lower ones:

| Layer | Packages |
|-------|----------|
| 0 | `core` (pure domain: models, ids, explanations, data-quality rules, conversions) |
| 1 | `settings`, `logging`, `schemas`, `profiles` |
| 2 | `store` |
| 3 | `dataset`, `ingest` |
| 4 | `analysis` |
| 5 | `planning`, `reports` |
| 6 | `publish` |
| 7 | `jobs`, `services`, `llm` |
| 8 | `cli`, `api`, `devtools` |

### 5.8 `llm/` (planned; empty today, will ship behind a feature flag)
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
  serve, profiles, autopilot, ridelog). Global `--profile <name>`.
- `api/` — FastAPI `create_app(AppContext, registry=..., web_dir=...)`: `/healthz` and `/v1/*`
  (reads, what-ifs, explicit writes, jobs, profiles, autopilot) with ETag caching and optional
  bearer token. `api/registry.py` serves every profile (`X-CYP-Profile`). Contract and budgets:
  [08-api.md](08-api.md).
- `services/calendar.py` — the calendar view: planned (calendar first) vs done per day, compliance status and week summaries (`GET /v1/calendar`, read-only);
- `services/ride_feedback.py` — post-ride RPE / feel answered on the web, fed into readiness (`data/ride_feedback/<id>.json`);
- `services/ride_log.py` — the deterministic WORKOUT + RIDE.LOG text of a ride, plus the athlete-edited version saved in `data/ride_logs/<id>.txt` (previous version kept as `.prev.txt`);
  `services/strava_write.py` — the only Strava write (the ride's description), flag-gated, with
  a scope and owner check (`cyp ride-log push`, `POST /v1/activities/{id}/strava-description`).
- `web/` (outside the Python layers) — the local dashboard; talks only to `/v1`, types generated
  from the OpenAPI file ([specs/web-ui.md](specs/web-ui.md)).

### 5.10 `profiles.py` and feature flags
- `profiles.py` — `ProfileStore` protocol + `FilesystemProfileStore` (`profiles/<slug>/` =
  `.env`, `athlete.yaml`, `profile.yaml`, `data/`), `set_planner_mode()`. Settings resolution
  for a profile lives in `settings.profile_settings()` (the root `.env` is not read; credentials
  never inherit). See [ADR-0006](adr/0006-profiles-and-tenancy.md).
- Flags: `settings.resolve_features()` (registry default → `STRAVA_ENABLED` legacy →
  `athlete.yaml features:` → `CYP_FEATURES`), read through `AppContext.features()`.
  `settings.with_feature_switches()` is the one bridge into lower layers (`strava_enabled`).

## 6. Runtime and deployment

**Default: the owner's always-on Mac.** `cyp schedule install` writes the launch agent
`~/Library/LaunchAgents/com.cy-performance.autopilot.plist`, which runs
`<venv python> -m cyp.cli run --all` daily at 05:30 local (a run missed while asleep fires on
wake). `run --all` starts one subprocess per profile; each takes the profile's lock, migrates
its DB to head, runs the autopilot and prints a JSON report. Log:
`~/Library/Logs/cy-performance/autopilot.log`; per-profile detail in `job_runs` and
`profiles/<slug>/data/logs/cyp.jsonl`.

Athletes other than the owner install nothing: intervals.icu + their head unit is their UI.

**Later (ADR-0006 L4): `cyp serve`** in a container with APScheduler + webhooks, a DB-backed
`ProfileStore` and intervals.icu OAuth instead of pasted keys. Same engine.

## 7. Configuration

| What | Where | In git? |
|------|-------|---------|
| Code, docs, `config/athlete.example.yaml` | repo | yes (public) |
| Secrets + machine settings per athlete | `profiles/<slug>/.env` (mode 600) | **no** |
| Athlete intent + planner knobs + feature flags | `profiles/<slug>/athlete.yaml` | **no** (`profiles/` may be its own private repo) |
| Profile identity (icu athlete id for the write guard) | `profiles/<slug>/profile.yaml` | **no** |
| Data: SQLite, Parquet, tokens, logs, reports | `profiles/<slug>/data/` | **no** |
| Default profile | `profiles/.default` | **no** |

Selection: `--profile` > `CYP_PROFILE` > `profiles/.default` > legacy layout (`.env`,
`config/athlete.yaml`, `data/`). `tests/test_privacy.py` fails if personal files get tracked.

## 8. Observability

- `structlog` JSON lines to `data/logs/cyp.jsonl` + human console.
- `job_runs` table: one row per stage run with status, counts, rate-limit snapshot, duration.
- `cyp doctor` prints token expiry, remaining Strava budget, last successful sync per source,
  schema version, and the last run of each job (including `autopilot`).
- Every plan change is a `plan_revisions` row with before/after JSON and a reason string.
- Every scheduled run is a `job_runs` row `autopilot` (counts per stage status; `failed` with
  the failed stage names); `cyp profile list` shows each profile's last run. Runbooks for known
  failures: [runbooks/](runbooks/README.md).

## 9. Testing strategy

- Unit: metrics against hand-computed fixtures (ported tests from `strava-analyis`).
- Contract: recorded API responses (`tests/fixtures/{strava,intervals}/*.json`) via `respx`.
- Planner: **simulation harness** with a Banister impulse-response athlete; assert plan
  converges to CTL target without breaching guardrails over a 16-week synthetic season.
- Replay: run the planner against the athlete's real 2026 history and inspect the plan it
  *would* have produced each week (regression snapshot).

## 10. Reference athlete (2026-10-02)

The first profile's answers shaped v1: the head unit is linked directly to intervals.icu (icu is
the stream source); an FTP target season with no race; a weekly hour ceiling, outdoor first with
indoor only for weather and tests; strength and yoga ingested as load only. The concrete numbers
live in that athlete's private `profiles/<name>/athlete.yaml`; new athletes answer
[onboarding-questions.md](onboarding-questions.md).
