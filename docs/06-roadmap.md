# 06 · Roadmap

Each milestone is independently useful and ends with the pipeline still running unattended.

| M | Scope | Done when |
|---|-------|-----------|
| **M0 Scaffold** (1–2 d) | `uv` project, `cyp` CLI skeleton, settings, structlog, SQLite + Alembic, `job_runs`, `cyp doctor`, CI (ruff, pytest) | `uv run cyp doctor` reports config + empty DB; tests green |
| **M1 Ingest** (3–5 d) | Port Strava client/OAuth/sync from `strava-analyis`; intervals.icu client + sync (athlete, sport settings, activities, intervals, wellness, power curves, events); matcher; Parquet stream store; 365-day backfill with rate budgeting | Both sources synced; every ride has streams (Strava) + icu fields; `fitness_daily.ctl_sim` tracks icu CTL ±1 |
| **M2 Analysis** (1 wk) | Port per-ride metrics + climbs + estimate; durability (decoupling, HR lag, status/next); longitudinal (PMC sim, ACWR/monotony, CP/W′ fit, durability trend, TID); readiness; compliance; daily/weekly Markdown report; optional Strava `RIDE.LOG` footer parity with the current project | Reports for all 2026 rides regenerate from stored data; readiness computed daily; `cyp daily` runs via launchd |
| **M3 Publish spike** (2 d) | Hand-written 3-event plan → `events/bulk` upsert on `external_id`, read-back `icu_training_load`, bulk-delete, verify Garmin push appears on the Edge 850. Confirm whether `upsert=true` works with API-key auth or needs `uid` | Idempotent re-run produces zero diff; events visible on device |
| **M4 Planner v1** (2 wk) | `config/athlete.yaml`, goals from icu calendar, season/blocks/weeks, ~25 YAML templates + renderer, daily replan loop, guardrails, `plan --dry-run` diff, simulation harness, replay against 2026 history | 16-wk synthetic season passes assertions; dry-run diff reviewed for 1 real week; then `CYP_PLAN_MODE=apply` |
| **M5 Adaptive + Explain** (ongoing) | Weekly review automation (FTP proposals, block adjustments), limiter-driven template bias, HR-fallback workouts; **explainability layer** per [07](07-explainability.md): zh-TW glossary of every model, daily/weekly reports that explain each verdict and workout, icu description footer + NOTE, `cyp explain`, LLM narrator (opt-in) | 4 weeks of unattended plans with compliance ≥ 80 % and no `needs_review` |
| **M6 Service** (optional) | `cyp serve` FastAPI + APScheduler, Strava webhooks, container + Litestream, read-only JSON API for a future UI | Same daily output from a VPS; webhook → analysis within 5 min of ride upload |

## Decisions taken (2026-10-02)
1. Edge 850 is linked directly to intervals.icu → icu streams primary, Strava secondary (M1
   should implement the icu stream fetch first; Strava stream fetch is the fallback path).
2. Season type `ftp_target`: 250 → ≥ 300 W by 2027-04-02, ≤ 15 h/wk, outdoor-first.
   Encoded in `config/athlete.yaml`.
3. Strength/yoga stay load-only.

## Explicitly deferred
Frontend, multi-athlete, nutrition/fuelling planning (icu `carbs_per_hour` exists; later),
run/swim planning, automatic FTP changes, route/weather-aware scheduling (icu has
`weather-forecast`; nice M5+ add-on for outdoor vs indoor choice).
