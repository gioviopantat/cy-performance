# 03 · Data model

Storage: **SQLite** (WAL) via SQLAlchemy 2.0 + Alembic for relational data; **Parquet** files for
per-point streams; JSON columns keep every raw payload. Postgres is a URL change (no SQLite-only
SQL allowed outside `store/`). See [ADR-0002](adr/0002-storage.md).

Conventions: all timestamps ISO-8601 UTC in `*_utc`, athlete-local in `*_local`; ids are the
source's native ids with a `source` discriminator; every externally-sourced table has `raw_json`
and `fetched_at`.

```
athletes 1─┬─* activities 1─┬─1 activity_metrics
           │                ├─* activity_intervals
           │                ├─* segment_efforts ─* segments
           │                ├─* activity_zones
           │                └─1 stream_files (parquet pointer)
           ├─* wellness_daily
           ├─* fitness_daily              (icu ctl/atl + our simulated)
           ├─* power_curve_snapshots
           ├─* athlete_settings_history   (ftp/eftp/weight/zones over time)
           ├─* goals                      (RACE_A/B/C, targets)
           ├─* seasons 1─* blocks 1─* week_plans 1─* planned_workouts
           │                                            └─* publish_log
           ├─* plan_revisions
           └─* readiness_daily
workout_templates, job_runs, sync_cursors, schema_meta
```

## Tables

### athletes
| col | type | notes |
|-----|------|-------|
| id | int PK | internal |
| strava_id | int unique | 188844906 |
| intervals_id | text unique | icu athlete id (e.g. `i123456`) |
| name, sex, timezone | text | `Asia/Taipei` |
| weight_kg | real | latest; history in `athlete_settings_history` |
| raw_strava_json, raw_intervals_json | json | |
| updated_at | text | |

### activities (unified ride record)
| col | type | notes |
|-----|------|-------|
| id | int PK | internal |
| strava_id | int unique null | icu sends it as a string; for Strava-origin icu stubs it equals `intervals_id` |
| intervals_id | text unique null | icu activity id: `i…` for Garmin/manual, the bare Strava id for Strava-origin |
| match_method | text | `strava_id` / `time_window` / `single_source` — set by the syncs and repaired by `matcher.rematch_all()` (docs/02 §3.6) |
| sport_type | text | Ride, VirtualRide, GravelRide, … (non-ride sports kept for load) |
| is_ride | bool | sport_type in ride set |
| name, description | text | |
| start_utc, start_local, tz | text | |
| moving_s, elapsed_s | int | |
| distance_m, elev_gain_m | real | |
| trainer, commute, race, manual | bool | |
| has_power, has_hr, has_cadence | bool | `device_watts` |
| device_name, gear_id, gear_name | text | Edge 850 / Rouvy / … |
| avg_w, np_w (icu_weighted_avg_watts), max_w, kj | real | summary from source |
| avg_hr, max_hr, avg_cad | real | |
| icu_training_load, icu_intensity, icu_ftp, icu_eftp | real | from icu; `icu_eftp` is icu's `icu_pm_ftp` (the API has no `icu_eftp`) |
| icu_pm_cp, icu_pm_w_prime, icu_pm_p_max | real | icu power-model fit for this ride |
| icu_decoupling, icu_polarization_index, icu_joules_above_ftp, icu_max_wbal_depletion | real | |
| icu_zone_times, icu_hr_zone_times | json | arrays of secs in zone order (icu sends `icu_zone_times` as `[{id, secs}]`; flattened on ingest) |
| paired_event_id | int | icu planned workout this ride fulfilled |
| icu_rpe, feel | int | athlete subjective |
| raw_strava_json, raw_intervals_json | json | verbatim payloads; Strava `laps` live in `raw_strava_json.laps` (no laps table) |
| stage_flags | json | `{detail, streams, efforts, zones, intervals, analyzed, streams_skipped, strava_detail, strava_detail_failed, strava_streams_failed}`; `streams_skipped ∈ {strava_origin, no_streams, empty_payload}` records why icu had nothing to fetch; `strava_detail` = Strava `GET /activities/{id}?include_all_efforts` was persisted (independent of icu's `detail`); `*_failed` = `"<iso ts> <http status>"` negative cache for a permanent 4xx, skipped by the Strava fallback stages until `sync strava --full` clears it |
| pending_detail, pending_streams, pending_analysis | bool | indexed work queues (Strava detail, icu streams, analysis) |
| fetched_at, updated_at | text | |
Indexes: `(start_utc)`, `(is_ride, start_utc)`, one per `pending_*` column.

### stream_files
| col | type | notes |
|-----|------|-------|
| activity_id | int PK FK | |
| source | text | `strava` / `intervals` |
| path | text | `data/streams/{activity_id}.parquet` |
| columns | json | present stream names |
| hz | real | 1.0 after resample; `original_size`, `resolution` kept |
| n_samples | int | |
| raw_json_path | text null | original non-1 Hz payload if resampled |

Parquet schema: `t_s:int32, dist_m:float32, lat:float64, lng:float64, alt_m:float32,
speed_mps:float32, hr:int16, cad:int16, watts:int16, watts_est:int16, temp_c:int8, moving:bool,
grade_pct:float32`. One file per ride keeps rewrites cheap; longitudinal scans use polars
`scan_parquet("data/streams/*.parquet")`.

### activity_metrics (our computed per-ride analysis)
| col | type | notes |
|-----|------|-------|
| activity_id | int PK FK | |
| algo_version | text | bump → recompute |
| np_w, if_, tss, vi | real | our own, to cross-check icu |
| tss_source | text | `power` / `hr` / `estimated` |
| ef, decoupling_pct, hr_lag_s, hr_drift_detail | real/json | durability |
| power_curve | json | `{5:…, 60:…, 300:…, 1200:…, 3600:…}` W |
| time_in_zone_power, time_in_zone_hr | json | |
| climbs | json | detected climbs with VAM, W/kg, grade |
| efforts | json | detected hard efforts |
| pacing | json | variability, first/second-half split |
| wbal_min_j | real | if CP/W′ known |
| estimated_power_meta | json | method used for no-power rides |
| comparison | json | deltas vs trailing baselines (EF, NP@HR, climb times) |
| status, next_recommendation | text | `NORMAL/BLUNTED/…`, `EASY/REST/…` (the `RIDE.LOG` fields) |
| explanation | json | `Explanation` object, see [07](07-explainability.md) |
| computed_at | text | |

### activity_intervals (from icu, plus our detector)
`id, activity_id, source(icu|cyp), idx, label, type(WORK|RECOVERY|…), start_s, duration_s,
avg_w, np_w, intensity, avg_hr, max_hr, avg_cad, decoupling, wbal_start, wbal_end, zone,
training_load, raw_json`

### segments / segment_efforts / activity_zones
Port of `strava-analyis/strava_analysis/db/schema.sql` with our naming (see `store/models.py`):
- `segments`: `id (strava), name, activity_type, distance_m, average_grade, maximum_grade,
  elevation_high, elevation_low, climb_category, city, state, country, starred, raw_json, fetched_at`
- `segment_efforts`: `id (strava effort), activity_id (our FK), segment_id, name, start_utc,
  start_index, end_index, elapsed_s, moving_s, distance_m, avg_w, device_watts, avg_hr, max_hr,
  avg_cad, pr_rank, kom_rank, achievements json, raw_json, fetched_at`
- `activity_zones`: `(activity_id, zone_type) unique`, `zone_type ∈ {power, heartrate}`,
  `sensor_based, custom_zones, points, distribution_buckets json, raw_json, fetched_at`
  (one row per entry of `GET /activities/{id}/zones`).
The matcher re-points `segment_efforts.activity_id` / `activity_zones.activity_id` when it
merges a Strava row onto its icu twin.

### wellness_daily (icu)
`athlete_id, date_local PK, ctl, atl, ramp_rate, ctl_load, atl_load, resting_hr, hrv, hrv_sdnn,
sleep_s, sleep_score, sleep_quality, avg_sleeping_hr, soreness, fatigue, stress, mood,
motivation, injury, readiness_icu, weight_kg, vo2max, steps, comments, raw_json, fetched_at`

### fitness_daily
| col | notes |
|-----|-------|
| athlete_id, date_local PK | |
| ctl_icu, atl_icu, tsb_icu | copied from wellness for convenience |
| ctl_sim, atl_sim, tsb_sim | our Banister replay (42/7) over `activities.icu_training_load` — must match icu within ±1; drift = bug or missing activity |
| load_actual, load_planned | TSS |
| acwr_7_28, monotony_7, strain_7 | Foster / Gabbett |

### power_curve_snapshots
`athlete_id, as_of_date, window(42d|90d|season|all), durations_s json, watts json, w_kg json,
cp, w_prime, p_max, eftp_icu, source(icu|cyp), raw_json`

### athlete_settings_history
`athlete_id, effective_from, ftp, indoor_ftp, eftp, w_prime, p_max, lthr, max_hr, resting_hr,
weight_kg, power_zones json, hr_zones json, source(icu_sport_settings|icu_eftp|manual), raw_json`

### readiness_daily (ours)
`athlete_id, date_local PK, score_0_100, status(FRESH|NORMAL|BLUNTED|OVERREACHED|SICK),
inputs json (hrv_z, rhr_z, sleep_z, tsb, yesterday_decoupling, yesterday_hr_lag, soreness…),
recommendation(REST|EASY|AS_PLANNED|UPGRADE), explanation json (Explanation), algo_version`

### goals
`id, athlete_id, name, date_local, category(RACE_A|RACE_B|RACE_C|TARGET), kind(climb_tt|
gran_fondo|crit|ftp_target|wkg_target), target json ({ftp:280} / {route_id, time_s}),
icu_event_id, priority, notes`

### seasons / blocks / week_plans
- `seasons`: `id, athlete_id, name, start_date, end_date, goal_id, status(active|archived), config json (ramp caps, availability, intensity model), created_at`
- `blocks`: `id, season_id, idx, phase(base|build|specialty|peak|taper|recovery|transition), start_date, end_date, weeks, load_pattern(3:1|2:1), focus json (limiters, key sessions)`
- `week_plans`: `id, block_id, week_start, target_tss, target_hours, hit_sessions, planned_ctl_end, template_id, status(draft|published|completed), notes`

### planned_workouts
| col | notes |
|-----|-------|
| id PK, week_plan_id FK, athlete_id | |
| date_local, slot | slot 1..n per day |
| external_id | `cyp:{season}:{date}:{slot}` — the idempotency key on icu |
| template_id, template_version | |
| name, intent(endurance|tempo|sweetspot|threshold|vo2|anaerobic|recovery|opener|test|race) | |
| steps json | our structured steps (duration_s, target_pct_lo/hi, cadence, kind) |
| workout_text | rendered icu grammar |
| target_tss, target_duration_s, target_if | |
| indoor bool, target_mode(POWER|HR) | |
| status | `proposed` → `published` → `completed` / `skipped` / `modified` / `superseded` |
| icu_event_id, icu_training_load_readback | verify after publish |
| executed_activity_id, compliance json | filled by `analysis/compliance.py` |
| created_by(planner|llm|athlete), revision | |
| explanation | json | why this workout today (Explanation) |

### plan_revisions
`id, season_id, created_at, trigger(daily|weekly|manual|llm|goal_change), reason text,
horizon_start, horizon_end, before json, after json, diff json, explanation json, applied bool`

### publish_log
`id, run_id, planned_workout_id, action(create|update|delete|noop), external_id,
icu_event_id, request json, response json, status(ok|failed|needs_review), verified_load, at`

### workout_templates
Loaded from `src/cyp/planning/library/*.yaml` into the DB for referential integrity:
`id, version, name, intent, phase_tags json, min_ftp_pct, steps_template json (with parameters),
duration_range_s, tss_formula, indoor_ok, outdoor_ok, requires_power bool, description`

### job_runs
`id, job, started_at, finished_at, status(running|ok|failed), counts json,
rate_limit_snapshot json, error text, log_path`

`job` names in use: umbrella `sync` / `backfill` (`cyp sync`, `cyp backfill`), stages
`sync:icu:{athlete,activities,wellness,power_curves,events}`, `backfill:icu`, `sync:strava`,
`match`; later `analyze`, `plan`, `publish`, `daily`, `weekly`.

### sync_cursors
`source, key, value, updated_at` — `strava/{activities_after, last_sync_at, rate_limit_snapshot}`,
`intervals/{athlete_id, activities_newest, wellness_newest, backfill_progress}`.

## Retention
Raw JSON and Parquet are kept indefinitely (single athlete, ~50 MB/year). If Strava access is
revoked, `cyp purge strava` deletes Strava-sourced rows/files (`source='strava'`) to satisfy the
API agreement.
