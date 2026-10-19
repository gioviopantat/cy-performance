# 09 · Feature flags

Registry: `src/cyp/core/features.py` · decision: [ADR-0008](adr/0008-feature-flags.md).
Check a profile: `cyp --profile <name> features`.

| id | default | requires | what it does |
|----|---------|----------|--------------|
| `sync.intervals` | on | | Pull activities, streams, wellness, sport settings from intervals.icu (autopilot `sync` stage). |
| `sync.strava` | on | `sync.intervals` | Strava segments/PRs and fallback streams. Off for profiles without Strava OAuth. Legacy switch: `STRAVA_ENABLED=false`. |
| `analysis.rides` | on | | Per-ride metrics, trends (PMC, CP/W′, FTP evidence, limiters), readiness (`analyze` stage). |
| `report.daily` | on | `analysis.rides` | Daily zh-TW report in `data/reports/daily/`. |
| `report.weekly` | on | `analysis.rides` | Weekly review report, built on Mondays. |
| `report.ride` | off | `analysis.rides` | Build the per-ride PDF for new rides (`cyp watch`). |
| `report.digest_weekly` | off | `analysis.rides` | Weekly zh-TW digest PDF under `data/reports/digest/`, built and (with `notify.email`) emailed by `cyp digest run` from Monday 07:00 for the ISO week just ended. Auto-sent only for weeks due after the job first saw the flag on; older weeks: `cyp digest weekly --period … --send`. |
| `report.digest_monthly` | off | `analysis.rides` | Monthly zh-TW digest PDF, same delivery, from the 1st 07:00 for the month just ended. |
| `plan.horizon` | on | | Replan the rolling horizon (`plan` stage). |
| `plan.climb_routes` | on | `plan.horizon` | Name a local climb from `location.climbs` in outdoor workout descriptions. |
| `plan.athlete_rules` | on | `plan.horizon` | `athlete.birth_year` / `athlete.health_flags` set safer defaults (age ≥ 60: 2:1, ramp and intensity caps; any health flag: ≤ 1 hard session, no maximal tests). With `plan.goal_menus` also on, athletes ≥ 60 get the lighter second hard day 48 h after the first. Off: `birth_year` / `health_flags` are dropped from the effective config, so nothing sees them. No effect without those fields. |
| `plan.long_ride_progression` | on | `plan.horizon` | With a `distance` goal and `availability.long_ride_max_minutes`: the long ride grows +15 min per loading week and is planned exactly that long (a menu ride of that length, else `long_ride_distance` sized to it); one over-distance ride per block, ≤ 60 min longer than the longest before it and never rotated away. No effect without both. |
| `plan.goal_menus` | on | `plan.horizon` | Workout menus by goal emphasis (`ftp` / `endurance` for a distance goal), ≥ 2 alternatives per slot so the limiter bias and `planner.intensity` decide; `planner.hit_days`; finish weeks try finish rides first. Off: `intensity` and `hit_days` are ignored and the plan equals the v1 planner (`tests/planning/test_golden.py`). |
| `plan.variety` | on | `plan.horizon` | Rotate equally ranked alternatives by week index (deterministic), so consecutive weeks differ. |
| `plan.personal_scales` | on | `analysis.rides` | Personal scales learned from the athlete's own history (ADR-0019, `analysis/personal_scales.py`): an RPE offset per ride class (90 d, ≥ 5 rides per class else all classes, shrunk n/(n+5), ±2), the usual post-ride feel (90 d, ≥ 8 answers) and the easy-day load (28 d of ridden non-hard days, ≥ 6, shrunk to the Z2-60 template, × [1, 1.8]). Readiness reads RPE against table + offset and a feel as bad only 2 points worse than the usual one (`readiness_v2.4`, never stricter); easy days below the habit are planned as 60–90 min Z2 near it, IF ≤ 0.70, except a today below AS_PLANNED, a week already ahead and athletes ≥ 60. `athlete.yaml planner.scales` wins over learned values. The morning run stores them in `learned_params`. Off: readiness and planner as before. |
| `plan.progression_gate` | off | `plan.horizon` | The weekly progression gate (ADR-0018, `planning/gate.py`): each Monday this week goes up (+1 progression step; two easy "up" weeks in a row: +1 extra, ≤ 2 per block), holds (no advance), goes down (−1 step, week TSS × 0.85), becomes an early recovery week, or after a ≥ 10-day break holds and steps back 1 (> 21 days: 2), from last week's key sessions, personal RPE delta, 7-day HRV band, EASY/REST days and completion; a week without any plan-vs-actual record follows the season. From Friday a week ≥ 15 % ahead of its plan eases its remaining easy days. Masters take the conservative branch. Explained in the plan (`plan.gate.<week>`) and the weekly digest / email ("下週為什麼這樣排"). Off until the replay acceptance passes and Roger agrees. |
| `readiness.ride_feel` | on | `analysis.rides` | Post-ride RPE / feel (intervals.icu fields or the web 騎完感受; per field the web answer wins) of the day's longest rated ride is a readiness input, judged against that ride's class and length (+1 expected RPE per hour beyond 3 h, ≤ +2); a hard-feeling easy ride lowers the next day's readiness and the planner adapts. Verdicts with these inputs carry `readiness_v2.1` (`readiness_v1.1` before ADR-0013). |
| `publish.calendar` | on | `plan.horizon` | Diff the plan against the intervals.icu calendar; **writes only when `planner.mode: apply`** and the write guard passes. |
| `api.calendar_write` | on | `publish.calendar` | The web UI's "run and write" button works for this profile (also needs `planner.mode: apply`, a confirmation and the write guard). |
| `strava.write_description` | off | `sync.strava` | Write a ride's RIDE.LOG into its Strava description (`cyp ride-log push`, the 紀錄 page). Needs the `activity:write` token scope, a confirmation, and the token owner = the ride owner; the athlete's own text is kept. |
| `strava.route_story` | off | — | The RIDE.LOG suggests a next spot and a stop worth revisiting from OpenStreetMap; points within 1 km of start and end never leave (ADR-0021, ADR-0022). |
| `llm.ride_poem` | off | — | Claude phrases the casual RIDE.LOG lines and writes the acrostic poem from the ride's facts (needs `ANTHROPIC_API_KEY`); validated, deterministic text on any failure (ADR-0021). |
| `strava.auto_description` | off | `strava.write_description` | The ride watcher writes each reported ride's RIDE.LOG into its Strava description right after the report (the flag is the per-profile confirmation, ADR-0020; every other gate of `strava.write_description` applies). Outcome in `ride_reports.strava_status`; one retry on the next tick, then given up. |
| `notify.macos` | on | | macOS notification when a run fails (no-op on other systems). |
| `notify.email` | off | | Email the report PDFs (`report.ride`, `report.digest_weekly`, `report.digest_monthly`) to `CYP_REPORT_EMAIL_TO` (copies to `CYP_REPORT_EMAIL_CC`, optional). Without any of those report flags it sends nothing. |

## Setting flags

```yaml
# profiles/<name>/athlete.yaml
features:
  sync.strava: false
  notify.macos: true
```

One-off override (not persisted): `CYP_FEATURES="publish.calendar=off" cyp run`.

## Adding a flag
1. Add a `Feature` to `REGISTRY` (after anything it `requires`).
2. Add a row above (`tests/test_docs.py` checks every id is documented).
3. Guard the code path with `ctx.features().enabled("<id>")`; if it is an autopilot stage, add
   it to `services/autopilot.py`.
4. Test both on and off.
