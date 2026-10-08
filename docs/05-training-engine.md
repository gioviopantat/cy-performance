# 05 · Training engine (planner)

The planner turns **athlete state + goals + constraints** into a **rolling 14-day horizon of
concrete workouts** inside a periodized season, and re-plans every day. It is deterministic,
simulatable, and bounded by guardrails ([ADR-0004](adr/0004-deterministic-planner.md)).

## 1. Inputs
| Input | Source |
|-------|--------|
| `AthleteProfile`: FTP, weight (→ W/kg), LTHR, max HR, zones, CP/W′/Pmax, eFTP | icu sport settings + `power_curve_snapshots` + `athlete_settings_history` |
| `FitnessState`: CTL/ATL/TSB, ramp rate, ACWR, monotony | icu wellness + `fitness_daily` |
| `Readiness` for today | `readiness_daily` |
| Goals: `RACE_A/B/C`, targets (e.g. FTP 280, 4.4 W/kg, climb X under Y min) | icu calendar + `goals` |
| Availability: per-weekday max minutes, indoor/outdoor, hard "no" days; overrides from icu NOTE events with `training_availability` / `max_training_time` | `config/athlete.yaml` + icu events |
| Limiters: from `longitudinal/limiters.py` (e.g. "fades after 40 min at threshold", "VO2 ceiling", "poor durability > 1 500 kJ") | analysis |
| Compliance history (what actually got done) | `planned_workouts.compliance` |
| Other-sport load (yoga, strength, hiking, trail run) | icu activities with `icu_training_load` |

## 2. Season → blocks → weeks → days

### 2.1 Season skeleton (`season.py`)
Given the next `RACE_A` date (or a target date for an FTP/W/kg goal) and current CTL:
1. Work backwards: **taper** (7–10 d) ← **peak/specialty** (3–4 wk) ← **build** (2 × 4 wk) ←
   **base** (remaining, min 4 wk). Shorter horizons compress from base first.
2. Each block gets a load pattern (`3:1` default, `2:1` if the athlete's readiness history shows
   frequent BLUNTED weeks) and a **CTL ramp target**: base +3–5/wk, build +4–6/wk, specialty
   0–+2, taper −10–15 % ATL with CTL held.
3. Weekly `target_tss = f(ctl_now, ramp_target)` via the PMC equations (closed form); `target_hours`
   from the block's intensity factor profile.
4. Block focus by phase and limiter:

| Phase | Primary stimulus | HIT sessions/wk | TID model |
|-------|------------------|-----------------|-----------|
| base | Z2 volume, cadence, strength-endurance, durability long rides | 1 (SS/tempo) | pyramidal 80/15/5 |
| build | sweetspot → threshold progression, long rides with late-ride intervals (durability) | 2 | pyramidal 75/15/10 |
| specialty | goal-specific: climb repeats at race power, VO2 (for GC-style aim: 4–8 min efforts), over-unders | 2–3 | polarized/pyramidal 75/5/20 |
| taper | sharpen: short VO2 touches, openers, volume −40–60 % | 1–2 short | |
| recovery week | volume −40 %, intensity −1 session | 0–1 | |

### 2.4 Season type `ftp_target` (the athlete's current season)
No race means no taper and no specialty phase. The season is a **threshold-development
progression with scheduled tests**, 26 weeks from 2026-10-05 to 2027-04-02:

| Weeks | Block | Aim | Checkpoint |
|-------|-------|-----|------------|
| 1–8 | base (3:1 ×2) | raise CTL toward the 15 h ceiling at low intensity; 1 SS session/wk; long outdoor rides 3–4 h with late-ride tempo for durability | wk 4 ramp test (baseline), wk 8 20-min test → expect ≈ 260–265 |
| 9–16 | build (3:1 ×2) | SS → threshold progression (3×12 → 2×20 → 3×20 @ 95–100 %); 2 HIT/wk; over-unders from wk 13 | wk 12 ramp test, wk 16 20-min test → ≈ 275–285 |
| 17–24 | threshold / VO2 (3:1 ×2) | lift the ceiling: VO2 4×5 → 5×5 @ 110–115 % alternating weeks with threshold 2×20 @ 100–102 %; long rides hold | wk 20 ramp test, wk 24 20-min test → ≈ 290–300 |
| 25–26 | test week + consolidation | 5-day mini-taper, 20-min test (outdoor climb or Rouvy), then re-baseline the next season | FTP ≥ 300 or honest re-target |

Rules specific to this season type:
- **FTP is re-estimated only from tests and icu eFTP** at checkpoints; intermediate targets are
  expectations for the report, not inputs to the plan. If a checkpoint misses by > 3 %, the next
  block repeats its first 2 weeks instead of progressing.
- **Volume ceiling**: planner caps at 15 h; expected steady state 12–14 h in base, 11–13 h in
  build (intensity up, hours down).
- **Outdoor-first**: every template has an outdoor rendering (longer steady targets, HR fallback
  cues on climbs). Indoor rendering is chosen only when the icu `weather-forecast` for the slot
  shows rain probability ≥ 60 % / temperature out of range, or for the ramp/20-min tests.
- **Feasibility note**: +20 % FTP in 26 weeks is ambitious for a rider already at 3.9 W/kg; the
  plan is built to find out quickly (tests every 4 weeks) rather than to assume it.

### 2.2 Week template (`WeekTemplate`)
Deterministic allocation of `target_tss` across available days:
- Fixed long ride on the day with most availability (weekend); never adjacent to the other HIT day
  unless block = specialty and readiness ≥ NORMAL.
- HIT days separated by ≥ 48 h; rest/recovery day after the long ride.
- Strength/yoga are **not planned**. Their actual load still enters the PMC via icu, and a heavy
  strength session detected yesterday downgrades today's HIT to Z2.
- Residual TSS → Z2 endurance of lengths that fit availability; if residual < 25 TSS → rest.

### 2.3 Daily workout (`planner.py`)
For each day in the horizon, choose a template from `library/` by `(phase, slot_role, limiter
bias, indoor/outdoor, available_minutes)`, then **parameterise**: durations and %FTP from the
block's progression index (week k of n), scaled so the template's predicted TSS matches the
slot's target within ±8 %. Render to icu text via `renderer.py`.

Progression examples: SS 3×10 @88 % → 3×12 → 3×15 → 2×20 @90 %; VO2 5×3 @115 % → 5×4 → 6×4 →
4×5 @112 %; over-unders 3×(2 min 95 % / 1 min 105 %) ×3 → ×4.

### 2.6 Personalized choice (`menus.py`, `planner.py`)
Spec: [specs/personalized-planning.md](specs/personalized-planning.md). Flags `plan.goal_menus`,
`plan.variety`, `readiness.ride_feel`.

- **Emphasis**: a `distance` goal not yet past means `endurance` (tempo, climb repeats,
  low-cadence SFR, steady long rides with a finish ride every other week (finish rides tried
  first, rotation only within the group), VO2 at most every other week in the threshold phase);
  otherwise `ftp` (sweet spot → threshold → VO2, with research-backed alternatives such
  as Seiler 4×8 and Rønnestad 30/15).
- Every stage has ≥ 2 alternatives, ranked by limiter bias × `planner.intensity`
  (easy / moderate / hard). With `plan.variety`, equally ranked alternatives rotate by week
  (never the over-distance ride).
- `planner.hit_days` orders the weekdays for hard sessions. With `plan.athlete_rules` on and the
  athlete ≥ 60 (same reference date as §2.5), a second hard day 48 h after the first gets a
  lighter tempo session (`menus.LIGHT_HIT`). Notes are added only after a slot is filled.
- With `plan.goal_menus` off, `hit_days` and `intensity` are ignored: the plan equals the v1
  planner (golden snapshot `tests/planning/test_golden.py`).
- Post-ride RPE / feel (intervals.icu or the web 騎完感受; per field the web answer wins) of the
  day's longest rated ride is part of readiness' "ride" component, judged against that ride's
  class plus +1 expected RPE per hour beyond 3 h (≤ +2). RPE ≥ 3 above that, or feel 5, caps
  the next day at EASY, and the daily re-plan turns hard work into Z2. Such verdicts carry
  `readiness_v1.1`.

### 2.5 Athlete rules and distance goals (`athlete_rules.py`, `season.py`)
Spec: [specs/age-health-and-distance-goals.md](specs/age-health-and-distance-goals.md).
Applied once in `AppContext.athlete_config()`, so every planner entry point sees the same
effective config. Flags `plan.athlete_rules` and `plan.long_ride_progression`.

- **Age** (`athlete.birth_year`, ≥ 60): defaults `load_pattern 2:1`, ramp cap 4/4/3 CTL per
  week, ≤ 2 hard sessions per week. Values written in `athlete.yaml` win; `cyp profile add`
  leaves `load_pattern` and the ramp test out for athletes ≥ 60 so the defaults apply. With the
  flag off, `birth_year` and `health_flags` are dropped from the effective config.
- **Health screen** (`athlete.health_flags`, any entry): ≤ 1 hard session per week, no ramp or
  20-min tests, no final test, and a zh-TW note to get medical clearance. These are hard caps
  that override written values. `[]` means screened with no issues.
- **Distance goal** (goal `kind: distance`, `target: {km, kmh?}`, plus
  `availability.long_ride_max_minutes`):
  - loading weeks: the long ride grows +15 min per loading week from the configured weekday
    minutes up to the ceiling (multiples of 15). The planner plans exactly that long: a menu
    ride of that length at its normal progression, else `long_ride_distance` sized to it;
  - recovery weeks: 70 % of the configured minutes;
  - the last loading week of each base/build/threshold block is the **over-distance ride**
    (template `long_ride_distance`, steady low Z2): the goal's estimated duration
    (km ÷ km/h, default 22 km/h), at most 1.2 × the ceiling and at most +60 min over the
    longest long ride before it;
  - the test phase is untouched, and the guardrails still apply (long ride ≤ 1.6 × the longest
    of the last six weeks, no HIT the next day).
- Not implemented yet (spec follow-ups): HR-medication rendering (power/RPE only) and the medical
  note in the weekly report.

## 3. Daily adaptation loop (`jobs/daily.py`)
```
sync (icu + strava) → analyze new rides → compliance of yesterday's slot → readiness(today)
→ replan(horizon = today .. today+13):
    1. Yesterday done as planned?       → keep progression index
       Partial/skipped HIT?             → re-slot that HIT within 72 h if a day frees; else drop (never stack two HIT)
       Over-done (TSS > 130 % plan)?    → pull next day to recovery, lower week residual
    2. Readiness today:
       REST        → today = rest, push HIT ≥ 48 h
       EASY        → today ≤ Z2, cap 60 % of planned TSS
       AS_PLANNED  → unchanged
       UPGRADE     → only if TSB > −10 and no HIT yesterday: allow +1 step of progression, never a new HIT
    3. Load safety: simulate PMC through horizon; if ramp > cap, ACWR > 1.5, or TSB < floor
       (−30 base/build, −20 specialty), remove lowest-value TSS until within bounds
    4. Calendar collisions: athlete-created events (no `cyp:` external_id) win; HOLIDAY/SICK/INJURED → no workouts; NOTE with `training_availability=LIMITED` + `max_training_time` → cap
    5. Only days ≥ today are mutable; today is mutable until 10:00 local (the ride usually starts 05:30–11:00) — configurable
→ diff vs published → guardrails → publish (apply mode) or write diff (propose mode) → report
```

## 4. Weekly review (`jobs/weekly.py`, Monday)
- Compliance %, TSS/hours vs target, TID vs model, readiness distribution.
- FTP/eFTP: if icu eFTP or our CP fit ≥ 3 % above FTP for 2 consecutive weeks and a recent
  ≥ 20 min effort supports it → propose `SET_EFTP` (never auto-set FTP in v1; write a NOTE +
  report line). Schedule a test (ramp or 20 min) at block boundaries when evidence is weak.
- Block progression: advance week index; if 2 of last 3 weeks were `BLUNTED`/under-complied,
  insert a recovery week early and lower next block ramp by 1/wk.
- Re-render next week fully; horizon beyond 14 d stays as week-level targets only.

## 5. Guardrails (`guardrails.py`, hard, applied after every planner or LLM proposal)
| Rule | Default |
|------|---------|
| CTL ramp per 7 d | ≤ 6 (base/build), ≤ 3 (specialty), configurable |
| TSB floor | −30 base/build, −20 specialty, ≥ +5 race week |
| HIT sessions / wk | ≤ 2 base/build, ≤ 3 specialty |
| HIT spacing | ≥ 48 h; none the day after long ride > 150 TSS |
| Weekly TSS vs 4-wk mean | ≤ +15 % (recovery weeks exempt downward) |
| Single ride | ≤ athlete max minutes for that weekday; ≤ 1.6 × longest ride in last 6 wk |
| Rest days | ≥ 1 / wk; ≥ 2 if readiness < 40 twice in week |
| Readiness | SICK/INJURED → rest until cleared; REST overrides everything |
| Publishing | only `external_id` prefix `cyp:`; never delete/alter athlete events; max 20 events/run; verified load within ±10 % of the load intervals.icu should compute (freeride steps counted at `ICU_FREERIDE_PCT` = 62 % FTP, as icu does; our own TSS keeps the template's lower descent value) |

Violations are logged with the rule id; the planner removes lowest-value items (by
`value = goal_relevance × progression_need`) until clean, else leaves the day empty and flags
`needs_review`.

## 6. Workout library (`planning/library/*.yaml`)
```yaml
id: ss_3x_n
version: 1
name: "Sweet Spot {reps}x{work_min}"
intent: sweetspot
phases: [base, build]
requires_power: true
indoor_ok: true
outdoor_ok: true
params:
  reps: {min: 2, max: 4}
  work_min: {min: 8, max: 20}
  pct: {min: 86, max: 92}
  rest_min: 4
steps:
  - {cue: Warmup, kind: ramp, duration: 10m, lo: 50, hi: 70}
  - repeat: "{reps}"
    steps:
      - {kind: work, duration: "{work_min}m", lo: "{pct}", hi: "{pct}+3", cadence: "85-95rpm"}
      - {kind: rest, duration: "{rest_min}m", lo: 50, hi: 55}
  - {cue: Cooldown, kind: steady, duration: 8m, lo: 50, hi: 50}
tss_model: closed_form   # TSS = Σ dur_h × IF² × 100 using mid of each step
```
Initial set (≈ 25 templates): recovery spin; Z2 endurance (60–240 min, with/without cadence
work); tempo; SS 3×n, 2×20, over-unders; threshold 2×15/3×12; VO2 5×3, 4×5, 30/30s, 40/20s;
anaerobic 8×1; long ride with late SS/threshold blocks (durability); climb repeats at goal power
(outdoor, HR fallback); ramp test; 20 min test; openers; race-day. Each has a `hr_fallback`
rendering for the no-power scenario (Edge 530 rides).

## 7. Optional LLM layer
- **Narrator**: structured daily/weekly facts → coach note (zh-TW). No numeric invention; prompt
  includes the exact figures to quote.
- **Reviewer** (off by default): receives athlete state + proposed horizon + guardrail list, may
  return `[{date, action: swap|shorten|drop|add_note, template_id?, reason}]`. Applied only if
  every edit passes `guardrails`, recorded in `plan_revisions(source='llm')`, and capped at 2
  edits/day. Rationale: the LLM is good at the *coach's judgment call* ("he complained about
  heat and legs in three ride notes this week"), the rules are good at the arithmetic.
- Inputs exclude raw Strava streams/text (see 02 §2.4).

## 8. Simulation and validation
A simulator command (planned; today the tests in `tests/planning/` play this role) runs the full
planner with a Banister responder (fitness τ 42, fatigue τ 7, k1/k2 fitted from the athlete's
history where possible), random compliance noise (85 % done, 10 % partial, 5 % skipped) and
synthetic readiness. Assertions: converges to CTL target ±5; zero guardrail breaches after
repair; no two HIT within 48 h; horizon stable (day-to-day churn ≤ 20 % of events). This is the
regression suite for every planner change.

## 9. Suggested climbs for outdoor sessions

`location.climbs` in `config/athlete.yaml` lists local climbs with the athlete's recorded ascent
times. For outdoor workouts whose longest hard step is ≥ 8 min, `planning/routes.py` adds one
description footer line `建議路段：…`: the shortest climb whose `minutes_min` holds the step in
one ascent, filtered by the session intent (`good_for`). When no climb is long enough, the
longest one is named with a hint to turn at the top or finish on the flat.
