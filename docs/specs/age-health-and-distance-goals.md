# Age, health screen and distance goals in the planner
Status: done except follow-ups (HR-medication rendering, weekly-report medical note) · 2026-10-07

## Goal
Use the onboarding answers the planner ignores today (docs/onboarding-questions.md, questions
1–3): an athlete's age and health screen shape recovery and intensity automatically, and a
distance goal ("ride 160 km without fatigue") grows the long ride towards it instead of only
chasing FTP.

## Non-goals
- No medical advice beyond "ask a doctor before hard efforts" when the screen says so.
- No route planning for the long ride (only its duration).
- No change to athletes who leave the new fields empty: their plans stay byte-identical.

## Design
- `athlete.yaml`:
  - `athlete.birth_year: int | null`
  - `athlete.health_flags: [heart_or_bp, symptoms_on_exertion, hr_medication, injury]` (empty list = screened, no issues; absent = not screened)
  - goal kind `distance` with `target: {km: 160, kmh: 24}` (`kmh` optional, default 22) alongside the FTP goal
  - `availability.long_ride_max_minutes` (optional ceiling the progression may reach, e.g. 300)
- `planning/athlete_rules.py` (layer 5, pure). It turns the fields above into **defaults**;
  explicit `planner:` values always win:

  | Condition | Defaults |
  |-----------|----------|
  | age ≥ 60 | `load_pattern 2:1`, `ramp_cap ≤ 4`, `hit_per_week ≤ 2`; with `plan.goal_menus`, a lighter second hard day 48 h after the first. Tests are only scheduled when written in athlete.yaml, and `cyp profile add` writes no ramp test (and no `load_pattern`) for athletes ≥ 60, so they get none unless asked for |
  | any health flag | HR-based targets in workout text, no maximal tests, `hit_per_week ≤ 1`, a zh-TW note in the weekly report recommending medical clearance |
  | `hr_medication` | power or RPE targets only (HR zones unreliable) |

- Long-ride progression (`planning/season.py`):
  - the long ride grows +15 min per loading week up to `long_ride_max_minutes` (minutes are
    multiples of 15; a ceiling below the configured long-ride minutes is raised to them);
  - the planner plans exactly that long (planner `_fill_long`: a menu ride of that length,
    else `long_ride_distance` sized to it), so the progression is real;
  - recovery weeks shorten it by 30 %;
  - one "over-distance" ride per block reaches the goal's estimated duration (km / `kmh`,
    default 22 km/h), capped at 1.2 × `long_ride_max_minutes` and at 60 min more than the
    longest long ride before it;
  - explained in zh-TW like every other plan change.
- Flags (ADR-0008):
  - `plan.athlete_rules` (default on: no effect without the new fields; off drops
    `birth_year` / `health_flags` from the effective config)
  - `plan.long_ride_progression` (default on: no effect without a distance goal)
- Layers: only `planning` and `settings` change. Docs: docs/05 §2 and §5, docs/09, docs/01 §5.5.

## Safety
Only lowers load or intensity relative to today's defaults, except the over-distance ride. That
ride is bounded by `long_ride_max_minutes` and the existing guardrails (TSB floor, ramp cap). It
writes nothing new to the calendar; publishing is unchanged.

## Acceptance
- A profile without the new fields plans exactly as before: snapshot test on the reference athlete.
- Reference athlete with `birth_year: 1950` (invented):
  - the season shows a 2:1 pattern and ≤ 2 HIT per week;
  - a profile created by `cyp profile add` gets no ramp test;
  - the guardrail simulation is clean.
- With a health flag:
  - ≤ 1 HIT per week;
  - no maximal test;
  - the weekly report carries the medical note.
- With a distance goal of 160 km and `long_ride_max_minutes: 300`:
  - the long ride grows from the availability value to 300 minutes across the season, by
    ≤ 60 min between consecutive loading weeks;
  - each block has exactly one over-distance ride;
  - the simulation is still guardrail-clean.
- `cyp profile add` asks questions 1–3 and writes the fields.

## Docs to update
docs/05 §2 and §5, docs/09, docs/01 §5.5, docs/onboarding-questions.md (mapping table), README.
