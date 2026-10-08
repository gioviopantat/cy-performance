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
| `plan.horizon` | on | | Replan the rolling horizon (`plan` stage). |
| `plan.climb_routes` | on | `plan.horizon` | Name a local climb from `location.climbs` in outdoor workout descriptions. |
| `plan.athlete_rules` | on | `plan.horizon` | `athlete.birth_year` / `athlete.health_flags` set safer defaults (age ≥ 60: 2:1, ramp and intensity caps; any health flag: ≤ 1 hard session, no maximal tests). With `plan.goal_menus` also on, athletes ≥ 60 get the lighter second hard day 48 h after the first. Off: `birth_year` / `health_flags` are dropped from the effective config, so nothing sees them. No effect without those fields. |
| `plan.long_ride_progression` | on | `plan.horizon` | With a `distance` goal and `availability.long_ride_max_minutes`: the long ride grows +15 min per loading week and is planned exactly that long (a menu ride of that length, else `long_ride_distance` sized to it); one over-distance ride per block, ≤ 60 min longer than the longest before it and never rotated away. No effect without both. |
| `plan.goal_menus` | on | `plan.horizon` | Workout menus by goal emphasis (`ftp` / `endurance` for a distance goal), ≥ 2 alternatives per slot so the limiter bias and `planner.intensity` decide; `planner.hit_days`; finish weeks try finish rides first. Off: `intensity` and `hit_days` are ignored and the plan equals the v1 planner (`tests/planning/test_golden.py`). |
| `plan.variety` | on | `plan.horizon` | Rotate equally ranked alternatives by week index (deterministic), so consecutive weeks differ. |
| `readiness.ride_feel` | on | `analysis.rides` | Post-ride RPE / feel (intervals.icu fields or the web 騎完感受; per field the web answer wins) of the day's longest rated ride is a readiness input, judged against that ride's class and length (+1 expected RPE per hour beyond 3 h, ≤ +2); a hard-feeling easy ride lowers the next day's readiness and the planner adapts. Verdicts with these inputs carry `readiness_v1.1`. |
| `publish.calendar` | on | `plan.horizon` | Diff the plan against the intervals.icu calendar; **writes only when `planner.mode: apply`** and the write guard passes. |
| `api.calendar_write` | on | `publish.calendar` | The web UI's "run and write" button works for this profile (also needs `planner.mode: apply`, a confirmation and the write guard). |
| `strava.write_description` | off | `sync.strava` | Write a ride's RIDE.LOG into its Strava description (`cyp ride-log push`, the 紀錄 page). Needs the `activity:write` token scope, a confirmation, and the token owner = the ride owner; the athlete's own text is kept. |
| `notify.macos` | on | | macOS notification when a run fails (no-op on other systems). |

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
