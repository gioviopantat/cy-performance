# 02 · Integrations: Strava and intervals.icu

Facts below were verified on 2026-10-02 against the live intervals.icu OpenAPI document
(`GET https://intervals.icu/api/v1/docs`, 118 endpoints) and Strava developer docs. Re-verify
before relying on anything marked *(forum)*.

## 1. Source-of-truth matrix

| Data | Primary | Fallback | Why |
|------|---------|----------|-----|
| Activity list + summary | intervals.icu | Strava | icu already merges Strava + Garmin + Rouvy and adds `icu_training_load`, `icu_intensity`, `icu_ftp`, `decoupling`, zone times |
| Per-point streams (watts, hr, cadence, altitude, latlng, grade) | **intervals.icu** | Strava | Edge 850 and Rouvy are linked directly to icu, so streams and intervals are served. icu does **not** serve streams for Strava-origin activities, so Strava remains the fallback for anything that only exists there |
| Official segments, segment efforts, PRs | Strava only | — | Not in icu |
| Detected intervals (laps/auto) | intervals.icu | our own detector | icu `GET /activity/{id}/intervals` is good and editable |
| CTL / ATL / TSB / ramp rate | intervals.icu `wellness.ctl/atl/rampRate` | our PMC replay | Single ledger; ours is for simulation only |
| FTP, eFTP, W′, Pmax, zones, LTHR, max HR | intervals.icu `sport-settings` + `activity.icu_ftp/icu_pm_ftp/icu_pm_*` | Strava `athlete/zones` | icu is where the athlete edits them |
| Power-duration curve | intervals.icu `power-curves` | computed from our streams | icu has multi-period curves + ranks |
| Wellness (HRV, RHR, sleep, weight, soreness, fatigue) | intervals.icu `wellness` | — | Garmin Health → icu |
| Planned workouts, races, notes | intervals.icu `events` | — | The calendar **is** the product surface |

## 2. Strava API v3

### 2.1 Auth
OAuth2 authorization code. Scopes: `activity:read_all,profile:read_all` (add `activity:write`
only if we keep writing the `RIDE.LOG` footer into descriptions). Access tokens live 6 h;
refresh tokens are rotated on every refresh — persist the new one atomically.
Existing working implementation: `strava-analyis/strava_analysis/auth/oauth.py` + `scripts/setup_oauth.py`.

### 2.2 Rate limits (per application)
| Window | Overall | Non-upload (reads) |
|--------|---------|--------------------|
| 15 min (resets :00 :15 :30 :45) | 200 | 100 |
| Daily (UTC midnight) | 2 000 | 1 000 |

Budget per new ride: 1 detail + 1 streams + 1 zones + (laps) ≈ 3–4 reads. Daily volume for one
athlete is < 20 reads. Backfill of 365 days ≈ 4 × rides, paced at ≤ 90 reads / 15 min →
resumable job with a persisted cursor. The existing client already parses `X-RateLimit-Usage`
and sleeps to the next window; port it.

### 2.3 Endpoints used
- `GET /athlete`, `GET /athlete/zones` (`StravaClient.get_zones`; stored on `athletes.raw_strava_json.zones`)
- `GET /athlete/activities?after=&before=&per_page=200&page=` (summary, incremental by `after`)
- `GET /activities/{id}?include_all_efforts=true` (detail + segment_efforts + laps + gear)
- `GET /activities/{id}/streams?keys=time,distance,latlng,altitude,velocity_smooth,heartrate,cadence,watts,temp,moving,grade_smooth&key_by_type=true`
- `GET /activities/{id}/zones` (`StravaClient.get_activity_zones`; one `activity_zones` row per `type` = `power` / `heartrate`)
- `PUT /activities/{id}` (optional description footer)

Implementation facts (`ingest/strava/sync.py`, verified 2026-10-02):
- Sync order per activity is **detail (+ segment efforts) → zones → streams**, then a final
  **stream-fallback stage** over every ride with a `strava_id` and no `stream_files` row,
  whatever its `pending_detail` state (covers icu Strava-origin rows). Detail and fallback
  share one per-run budget (`STRAVA_MAX_DETAIL_FETCHES`, default 60).
- Streams are fetched only when `--streams` / `cyp sync` (streams on) and no Parquet exists
  for the activity (ADR-0003: icu is the primary stream source).
- Laps are **not** a table: they stay in `activities.raw_strava_json.laps`.
- `segments` / `segment_efforts` column names follow `store/models.py` (`distance_m`,
  `elapsed_s`, `moving_s`, `avg_w`, `avg_hr`, `pr_rank`, `kom_rank`, `achievements`, `raw_json`),
  not Strava's field names.
- Webhooks (optional, `cyp serve` only): one subscription per app, `GET` validation must echo
  `{"hub.challenge": ...}` within 2 s, `POST` events `{object_type, aspect_type, object_id, owner_id, updates}`;
  ack 200 immediately and process async. Retries 3×.

### 2.4 Legal constraints that shape the design
- Strava data may be shown **only to the athlete it belongs to**. Each profile's Strava data
  stays in its own `profiles/<name>/data/`; never show one profile's Strava-derived data to
  another, and never add a public share feature that includes it.
- The Nov-2024 agreement update restricts third-party **AI/ML use** of Strava data. For the
  optional LLM narrator we therefore feed only our own derived aggregates (TSS, NP, decoupling
  numbers we computed) and intervals.icu fields, never Strava stream arrays or Strava text. The narrator will ship
  behind a feature flag that defaults to off (ADR-0008). Re-read https://www.strava.com/legal/api before enabling.
- Delete stored Strava data if the athlete deauthorises (webhook `athlete` / `deauthorize`).

## 3. intervals.icu API v1

### 3.1 Auth and limits
- Base `https://intervals.icu/api/v1`. HTTP Basic, username literally `API_KEY`, password = the
  key from Settings → Developer. Athlete id `0` = key owner (we will still store the real id).
- Rate limit (API-key users, *forum*): 5 000 / day, 2 500 / rolling 15 min. Headers
  `X-RateLimit-Limit`, `X-RateLimit-Remaining`; 429 with `Retry-After`. Effectively unlimited for us.
- Send a browser-like `User-Agent`; Cloudflare has blocked default Python UAs *(forum)*.
- OAuth exists for multi-user apps (100 req/user/day). Not needed for v1; `ingest/intervals/client.py`
  takes an auth strategy object so it can be added later.

### 3.2 Read endpoints we use
| Endpoint | Purpose | Notes |
|----------|---------|-------|
| `GET /athlete/{id}` | profile incl. `sportSettings`, `icu_resting_hr`, `icu_weight`, timezone | 158 fields |
| `GET /athlete/{athleteId}/sport-settings` | per-sport `ftp`, `indoor_ftp`, `w_prime`, `p_max`, `power_zones`, `lthr`, `max_hr`, `hr_zones`, `mmp_model` | zones source of truth |
| `GET /athlete/{id}/activities?oldest=&newest=&fields=` | activity list, 184 fields incl. `icu_training_load`, `icu_intensity`, `icu_ftp`, `icu_pm_ftp` (per-ride eFTP), `icu_pm_cp/w_prime/p_max`, `icu_ctl`, `icu_atl`, `decoupling`, `icu_zone_times`, `icu_hr_zone_times`, `polarization_index`, `icu_joules_above_ftp`, `icu_max_wbal_depletion`, `paired_event_id`, `strava_id`, `external_id`, `device_name`, `source` | desc date order; use `fields` to trim |
| `GET /activity/{id}` | single activity, same schema | |
| `GET /activity/{id}/intervals` | detected/edited intervals, 84 fields each (`average_watts`, `intensity`, `decoupling`, `wbal_start/end`, `zone`, `training_load` …) | |
| `GET /activity/{id}/streams.json?types=` | streams (types `time,watts,heartrate,cadence,velocity_smooth,altitude,latlng,distance,grade_smooth,temp,torque,left_right_balance`) | primary stream source; empty for Strava-origin activities |
| `GET /activity/{id}/power-curve.json`, `/power-vs-hr`, `/time-at-hr`, `/best-efforts` | per-activity curves | |
| `GET /athlete/{id}/power-curves.json?type=Ride&curves=&newest=` | best power curves (season / 42d / custom) | returns `DataCurveSetPowerCurve{list: [...]}`, one entry per requested curve |
| `GET /athlete/{id}/mmp-model?type=Ride` | the power model icu uses for `%MMP` workout steps; **CP / W′ / Pmax come from here**, not from the curves | |
| `GET /athlete/{id}/wellness.json?oldest=&newest=` | daily `ctl, atl, rampRate, ctlLoad, atlLoad, restingHR, hrv, hrvSDNN, sleepSecs, sleepScore, soreness, fatigue, stress, mood, motivation, readiness, weight, vo2max, steps, comments` | our readiness input |
| `GET /athlete/{id}/events.json?oldest=&newest=` | calendar read-back: our workouts + athlete's races/notes | |
| `GET /athlete/{id}/fitness-model-events` | SET_EFTP / SET_FITNESS / SEASON_START markers | |
| `GET /athlete/{id}/workouts`, `/folders` | the athlete's own workout library | seed our template library |

Schema facts that differ from the prose above (verified against the live OpenAPI document and
real payloads, 2026-10-02; `ingest/intervals/mapping.py` is the reference):
- `Activity` has **no `icu_eftp`**. The per-ride eFTP is `icu_pm_ftp` (we store it in our
  `activities.icu_eftp` column). `icu_ftp` is the FTP in force on that day.
- `icu_zone_times` is a list of `{id: "Z1", secs: 120}` objects (we flatten to seconds, zone
  order); `icu_hr_zone_times` is a plain list of seconds.
- `strava_id` is a **string** (`"12345678901"`), nullable; we store it as an integer.
- `source` is the origin discriminator: `GARMIN_CONNECT`, `STRAVA`, `MANUAL`, `UPLOAD`, … .
  `source == "STRAVA"` is authoritative for "Strava-origin"; `strava_id` is a fallback.
- **Strava-origin activities come back as stubs**: `{"id": "<stravaId>", "icu_athlete_id",
  "start_date_local", "source": "STRAVA", "_note": "STRAVA activities are not available via
  the API"}` — no `type`, no metrics, no streams, no `strava_id`. The icu `id` of such an
  activity **is** the Strava activity id (see §3.6).
- Path templates in the OpenAPI document are `{ext}` / `{format}`; the working value is `.json`.
- Power curves come as `DataCurveSetPowerCurve{list: [{id, secs[], watts[], …}]}`; CP/W′ are
  not in the curve payload but in `/mmp-model`.

### 3.3 Write endpoints we use
| Endpoint | Purpose |
|----------|---------|
| `POST /athlete/{id}/events/bulk?upsert=true` | create/update planned workouts matched on `external_id` (API-key auth: use `upsertOnUid=true` with our `uid` if `upsert` is only honoured for OAuth clients — verify in M3 spike) |
| `PUT /athlete/{id}/events/bulk-delete` | remove our stale events by id / external_id |
| `POST /athlete/{id}/events` | single NOTE (daily coach note) |
| `PUT /athlete/{id}/wellness/{date}` | optional: write our `readiness`/`comments` back |
| `PUT /activity/{id}` | optional: set `icu_rpe`, `feel`, tags |
| `POST /activity/{id}/messages` | optional: post the ride analysis as a comment |

### 3.4 `EventEx` payload for a planned workout
```json
{
  "category": "WORKOUT",
  "start_date_local": "2026-10-06T00:00:00",
  "type": "Ride",
  "name": "SS 3x12 — build wk2",
  "description": "Warmup\n- 10m ramp 50-70%\n\nMain Set 3x\n- 12m 88-92% 85-95rpm\n- 4m 50%\n\nCooldown\n- 8m 50%",
  "indoor": false,
  "external_id": "cyp:season-2026a:2026-10-06:1",
  "tags": ["cyp", "sweetspot", "build"],
  "target": "POWER",
  "moving_time": 4320,
  "color": "orange"
}
```
intervals.icu parses `description` into `workout_doc` server-side and computes
`icu_training_load`, `joules`, `icu_intensity`; we read those back to verify the plan's load
matches our target (±5 %). Supported categories: `WORKOUT, RACE_A/B/C, NOTE, PLAN, HOLIDAY, SICK,
INJURED, SET_EFTP, FITNESS_DAYS, SEASON_START, TARGET, SET_FITNESS`. Useful extras:
`training_availability` + `max_training_time` on NOTE events let the athlete tell the planner
"limited day" from the calendar itself.

### 3.5 Workout text grammar (what `renderer.py` emits)
```
<Section title> [Nx]           # optional repeat count on the title line
- <duration|distance> <target> [cadence]
```
- Duration `10m`, `30s`, `1h2m30s`, `5'`, `30"`. Distance `500mtr`, `2km` (`m` = minutes!).
- Power targets: `75%`, `95-105%` (of FTP), `220w`, `200-240w`, `Z2`, `Z3-Z4`, `60% MMP 5m`.
- HR targets: `70% HR`, `95% LTHR`, `Z2 HR`. Cadence: `90rpm`, `90-100rpm`.
- Ramps: `10m ramp 50%-75%`. Free ride (disables ERG): `20m freeride`.
- Repeats: `Main Set 4x` title or a standalone `5x` line; blank line before and after a repeat
  block; **no nesting**. Text before the first duration becomes the on-device cue.
- Timed cues inside a step: `- Settle in^120 Lift cadence <!> 10m ramp 25-75%`.
- Do not mix power/HR/pace targets in one workout (renders badly).
Reference: forum "Workout Builder Syntax Quick Guide" (t/123701), zonepace.cc/intervals-workout-format.

### 3.6 Activity matching Strava ↔ intervals.icu
Implemented in `ingest/matcher.py` (`Matcher.rematch_all`, idempotent, run after every sync):

1. **`strava_id`**: icu `Activity.strava_id` when present, or — for Strava-origin stubs — the
   numeric icu `id`, which *is* the Strava id. The Strava sync upserts by `strava_id`, so when
   icu already carried the id both sources land on one row. When the stub arrived without the
   id (or Strava synced first) the matcher merges the Strava row onto the icu row.
2. **`time_window`**: no id in common, `start_utc` within ±120 s **and** `moving_time` **or**
   `elapsed_time` within ±2 %. icu and Strava compute moving time differently on the same FIT
   file (observed up to 5 % apart) while elapsed time is identical, hence the "or".
3. **`single_source`**: everything else.

Merge rules: the icu row is primary. Strava-only data (raw payload, segment efforts, zones,
summary columns icu left `NULL`) is copied over; for a stub, Strava's summary wins outright.
The Strava Parquet is re-pointed to the icu row **only if icu has no streams**; otherwise it is
deleted — streams from two sources are never mixed into one ride (ADR-0003). The duplicate row
is deleted and the icu row is marked `pending_analysis`. Both ids and `match_method` are stored.

## 4. Failure handling
- All clients: exponential backoff on 5xx (max 3), honour `Retry-After` on 429, circuit-break a
  source for the rest of the run after 3 consecutive failures; other stages continue.
- Strava 15-min window exhaustion → sleep to boundary (existing behaviour) unless
  `--no-wait`, in which case persist cursor and exit 0 for the next run.
- Publish is all-or-nothing per run: diff computed → `bulk` upsert → read-back → verify
  `icu_training_load` within tolerance → `publish_log.status=verified`; any mismatch →
  `status=needs_review`, no retries without a human (`cyp plan --apply --confirm-write` after fixing the cause).
