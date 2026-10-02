# Workout template library (`planning/library/*.yaml`)

One YAML file per template, `id` == file stem. Templates are **parameterised**: the planner
picks a template by `(phase, slot_role, limiter bias, indoor/outdoor, available_minutes)`,
chooses parameter values inside the declared ranges from the block's progression index, then
`renderer.py` expands the steps into intervals.icu workout text (docs/02 §3.5). Loaded into the
`workout_templates` table at startup (docs/03). Edits to a template must bump `version`.

The library is tuned for the current `ftp_target` season (docs/05 §2.4): FTP 250 W → 300 W in
26 weeks, 70 kg, ≤ 15 h/wk, outdoor-first, no race. Every template therefore carries an
`outdoor_rendering` block and (where it makes physiological sense) an `hr_fallback` for rides
without a power meter.

## 1. Top-level schema

| key | type | required | meaning |
|-----|------|----------|---------|
| `id` | str | yes | snake_case, equals the file name without `.yaml` |
| `version` | int | yes | bump on any change to steps/params |
| `name` | str | yes | English display name; may contain `{param}` placeholders |
| `name_zh` | str | yes | zh-TW display name, same placeholders |
| `purpose_zh` | str | yes | one line: what the session trains |
| `intent` | enum | yes | `recovery \| endurance \| tempo \| sweetspot \| threshold \| vo2 \| anaerobic \| test \| opener` (matches `activity_metrics.classification`; `opener` is new) |
| `phases` | list | yes | subset of `base, build, threshold, test` (the `ftp_target` season blocks; `test` = test week / consolidation) |
| `slot_roles` | list | yes | subset of `long_ride, hit, endurance, recovery, test, opener`; the week template fills slots by role |
| `requires_power` | bool | yes | `false` only if the primary steps are HR/RPE based |
| `indoor_ok`, `outdoor_ok` | bool | yes | at least one must be true |
| `params` | map | yes | see §2; may be empty `{}` |
| `progression` | list | no | param names in the order the planner increments them as the block progresses (first = first to grow). Omit for fixed templates |
| `steps` | list | yes | see §3; targets in **% FTP** |
| `outdoor_rendering` | map | yes if `outdoor_ok` | see §4 |
| `hr_fallback` | map or `null` | yes | see §5; `null` means "no sensible HR version" and a `reason_zh` sibling key `hr_fallback_reason_zh` must be present |
| `test` | map | only for tests | `{protocol, ftp_estimate, notes_zh}` free-form, surfaced in the workout description |
| `tss_model` | enum | yes | only `closed_form` in v1 (see §6) |
| `explain_zh` | str | yes | 1–2 sentences: what it trains and why it belongs in its phase(s). Copied verbatim into `Explanation.because[0].text_zh` for the planned day |

## 2. Parameters

```yaml
params:
  reps:     {min: 2, max: 4, default: 3, step: 1}   # ranged, integer
  pct:      {min: 86, max: 92, default: 88}         # ranged, step defaults to 1
  rest_min: 4                                       # scalar = fixed constant, still substitutable
```

- Ranged params are integers (minutes, reps, % FTP). `default` is the value used for the
  rendered-example and for `duration_range_s`/TSS estimates in `workout_templates`.
- The planner must clamp to `[min, max]`; a value outside the range is a validation error, not a
  silent clamp (ADR-0004: deterministic and loud).
- `progression` lists which params grow first. Example `ss_3x_n`: `[work_min, reps, pct]` means
  3×8 → 3×10 → … → 3×20 before reps or % move. The planner scales the chosen values so the
  template's closed-form TSS lands within ±8 % of the slot target (docs/05 §2.3).

## 3. Steps

A step is either a **leaf** or a **repeat block**. Repeat blocks contain leaves only
(**no nesting**, because the icu grammar has none). Multi-set interval sessions are written as
several top-level repeat blocks separated by top-level rest leaves (see `over_unders_3x_n`).

```yaml
steps:
  - {cue: Warmup, kind: ramp, duration: 10m, lo: 50, hi: 70}
  - repeat: "{reps}"
    cue: Main Set
    steps:
      - {kind: work, duration: "{work_min}m", lo: "{pct}", hi: "{pct}+4", cadence: "85-95rpm"}
      - {kind: rest, duration: "{rest_min}m", lo: 50, hi: 55}
  - {cue: Cooldown, kind: steady, duration: 8m, lo: 50, hi: 50}
```

Leaf keys:

| key | required | notes |
|-----|----------|-------|
| `cue` | no | section title on the device (icu: text before the first duration). A top-level leaf with a `cue` starts a new section; subsequent cue-less top-level leaves join it |
| `kind` | yes | `steady \| ramp \| work \| rest \| freeride`. `work` and `rest` are semantically tagged for compliance scoring (interval hit-rate is computed over `work` steps only); `steady` is everything else at a fixed target |
| `duration` | yes | `<expr><unit>` with unit `m` (minutes) or `s` (seconds). `expr` is an integer expression over params: `"{work_min}m"`, `"{total_min}-{tempo_min}-35m"`, `"30s"` |
| `lo`, `hi` | yes except `freeride` | % FTP (or % LTHR inside `hr_fallback`). Integer or expression string (`"{pct}+4"`). `lo == hi` renders as a single value. For `ramp`, `lo` is the start and `hi` the end |
| `cadence` | no | icu cadence token, `"90rpm"` or `"85-95rpm"` |
| `tss_assume` | `freeride` only | % FTP the closed-form TSS model assumes for the step (e.g. 105 for a 20-min test effort, 45 for a descent) |

Repeat block keys: `repeat` (int or expression string), `cue` (section title; the renderer appends
` {n}x`), `steps` (leaves only).

**Expression grammar**: substitute every `{name}` with the param value, then evaluate `+ - * /`
on integers with the usual precedence (`*` `/` before `+` `-`, left to right; **no parentheses**,
keep it trivial), round to nearest integer. Durations are converted to seconds internally.
Example: `"{total_min}-{ss_reps}*{ss_min}-{ss_reps}*5-35m"` with 210/2/15 → 210−30−10−35 = 135 min.
`{name}` placeholders are also substituted in `name`, `name_zh`, `outdoor_rendering.note*`.

## 4. `outdoor_rendering`

Outdoors there is no ERG, cadence is terrain-bound and steady targets are unrealistic as single
numbers. The block tells the renderer how to adapt the same steps:

```yaml
outdoor_rendering:
  note: "Pick a 15-20 min climb or flat road without junctions for the work steps."
  note_zh: "工作段請選 15–20 分鐘沒有路口的坡或平路。"
  widen_pct: 3            # work/steady leaves: lo-3 .. hi+3 (clamped >= 30)
  rests_as_freeride: true # rest leaves render as "4m freeride" (and TSS assumes 50 %)
  ramps_as_range: true    # warmup ramps render as "10m 50-70%" instead of "10m ramp 50-70%"
```

`indoor: false` goes on the icu event. Steps with per-step outdoor behaviour that the three
switches cannot express (e.g. descents) are written explicitly as `freeride` leaves in `steps`
with a `tss_assume`, and the template sets `indoor_ok: false` if it has no indoor meaning
(`climb_repeats_goal_power`).

## 5. `hr_fallback`

A complete alternative step list in **% LTHR** used when the planner knows the ride will be
on the Edge 530 without power (`AthleteProfile.lthr` required). Mirrors the power steps
one-to-one so compliance can still match the structure. The renderer emits either the power
version or the HR version — never both in one workout (icu renders mixed targets badly).

```yaml
hr_fallback:
  note_zh: "心率落後功率約 60–90 秒，間歇前 1–2 分鐘以 RPE 控制。"
  steps:
    - {cue: Warmup, kind: ramp, duration: 10m, lo: 60, hi: 75}
    - repeat: "{reps}"
      cue: Main Set
      steps:
        - {kind: work, duration: "{work_min}m", lo: 90, hi: 96}
        - {kind: rest, duration: "{rest_min}m", lo: 60, hi: 70}
    - {cue: Cooldown, kind: steady, duration: 8m, lo: 60, hi: 68}
```

Approximate % LTHR bands used throughout the library (Coggan/Friel HR zones): recovery < 68,
Z2 69–83, tempo 84–94, sweet spot 90–97, threshold 97–103, VO2 ≥ 104 (not prescribable by HR —
VO2 and shorter templates set `hr_fallback: null`).

HR fallback TSS is **hrTSS** on icu's side; our closed-form estimate for the HR version uses the
same % as if it were % FTP (crude, flagged `confidence: low` in the Explanation).

## 6. `tss_model: closed_form`

```
TSS = Σ_steps (dur_s / 3600) × (mid / 100)² × 100
mid = (lo + hi) / 2          for steady/work/rest/ramp (ramp mid is the time-average of a linear ramp)
mid = tss_assume             for freeride
IF  = sqrt(TSS / (total_h × 100))
```

Example `ss_3x_n` defaults (3×10 @ 88–92 %, rest 4, warmup 10 m ramp 50–70, cooldown 8 m 50 %):
warmup 6.0 + work 3 × 13.5 + rest 3 × 1.8 + cooldown 3.3 ≈ **55 TSS**, 60 min, IF 0.74.
`renderer.py` recomputes this, and `publish` verifies icu's `icu_training_load` is within ±10 %.

## 7. Rendering to intervals.icu text

Rules (`docs/02 §3.5`):
1. Each section = title line (`cue`, plus ` Nx` for a repeat) followed by `- ` step lines.
2. Step line: `- <duration> <target>[ <cadence>]`. Duration `10m`, `30s`, `1h30m`.
3. Target: `88-92%` / `50%` for power leaves; `ramp 50-70%` for ramps; `freeride`; HR version
   `90-96% LTHR` / `62% LTHR`.
4. **Blank line before and after every repeat block.** No nested repeats.
5. Never mix power and HR targets. Never mix `%` and `w`.
6. Minutes are `m`; `mtr` is metres — never emit distance.
7. The footer (2–3 lines「為什麼排這個」+ report link, docs/07 §3) is appended by `publish`, not
   by the renderer, after a blank line, prefixed `#` so icu ignores it as a step.

### Example 1 — `ss_3x_n` indoor, `reps=3 work_min=12 pct=88 rest_min=4`

```
Warmup
- 10m ramp 50-70%

Main Set 3x
- 12m 88-92% 85-95rpm
- 4m 50-55%

Cooldown
- 8m 50%
```
Closed-form TSS ≈ 63, 64 min, IF 0.77. Event: `type: Ride`, `indoor: true`, `target: POWER`,
`name: "Sweet Spot 3x12"`, `tags: [cyp, sweetspot, build]`.

### Example 2 — `over_unders_3x_n` outdoor, `cycles=3 under_pct=93 over_pct=105`
(`widen_pct: 2`, `rests_as_freeride: true`, `ramps_as_range: true`)

```
Warmup
- 12m 50-72%
- 3m 83-94%
- 3m 48-57%

Set 1 3x
- 2m 91-98% 85-95rpm
- 1m 103-112% 90-100rpm

Recover
- 5m freeride

Set 2 3x
- 2m 91-98% 85-95rpm
- 1m 103-112% 90-100rpm

Recover
- 5m freeride

Set 3 3x
- 2m 91-98% 85-95rpm
- 1m 103-112% 90-100rpm

Cooldown
- 10m 48-57%
```
Each set is its own top-level repeat block (three sets, no nesting). `widen_pct` applies to
every `work`/`steady` leaf (including the warmup's steady leaves); `rests_as_freeride` only to
`rest` leaves (the 3 m 50–55 % in the warmup is `steady`, so it stays a range). `indoor: false`.
Closed-form TSS ≈ 66, 65 min.

### Example 3 — `long_ride_late_tempo` HR fallback, `total_min=210 tempo_min=30`
(Edge 530, no power; targets in % LTHR, nothing else in the workout)

```
Warmup
- 15m ramp 60-78% LTHR

Endurance
- 145m 70-83% LTHR 85-95rpm

Late Tempo
- 30m 84-92% LTHR 85-95rpm

Finish Easy
- 20m 65-75% LTHR
```
`target: HR` on the event; icu computes hrTSS. Our Explanation marks `confidence: low` for the
load estimate. (The power version of the same session: `- 145m 62-75% 85-95rpm`, `- 30m 80-86%`,
closed-form TSS ≈ 168, 210 min, IF 0.69.)

## 8. Validation (what the loader enforces)

- Every `{param}` in `name`, `name_zh`, `steps`, `hr_fallback` exists in `params`.
- Rendering with `default` values produces ≥ 1 `work`/`steady` step and a positive duration.
- `phases ⊆ {base, build, threshold, test}`, `intent` in the enum, `indoor_ok or outdoor_ok`.
- No repeat block contains a repeat block.
- `hr_fallback` steps, when present, contain no `cadence`-free `freeride` ambiguity: a freeride
  leaf is allowed in HR fallback (descents).
- `tss_model == closed_form`.

A schema check belongs in `tests/planning/test_library.py` (owned by the code skeleton). The
30 templates in this folder were checked against the rules above with a throwaway script when
written: all parse, render indoor/outdoor/HR without nested repeats, HR fallback durations equal
the power version, and the closed-form TSS values are the ones in the table below.

## 9. Template index (default params, closed-form TSS, indoor rendering)

| id | intent | phases | slot | default | TSS | min | IF |
|----|--------|--------|------|---------|-----|-----|----|
| recovery_spin | recovery | all | recovery | 45 min 45–55 % | 19 | 45 | 0.50 |
| z2_endurance_60 | endurance | base build threshold | endurance | Z2 62–75 % | 43 | 60 | 0.66 |
| z2_endurance_90 | endurance | base build threshold | endurance | Z2 62–75 % | 65 | 90 | 0.66 |
| z2_endurance_120 | endurance | base build threshold | endurance, long_ride | Z2 62–75 % | 88 | 120 | 0.66 |
| z2_endurance_180 | endurance | base build threshold | long_ride | Z2 62–75 %, outdoor only | 132 | 180 | 0.66 |
| z2_endurance_240 | endurance | base build | long_ride | Z2 62–75 %, outdoor only | 179 | 240 | 0.67 |
| z2_cadence_90 | endurance | base build | endurance | Z2 + 4 × 5 m @ 100–110 rpm | 66 | 90 | 0.66 |
| z2_cadence_120 | endurance | base build | endurance, long_ride | Z2 + 5 × 5 m @ 100–110 rpm | 89 | 120 | 0.67 |
| z2_cadence_180 | endurance | base build | long_ride | Z2 + 6 × 5 m @ 100–110 rpm | 134 | 180 | 0.67 |
| long_ride_late_tempo | endurance | base build | long_ride | 210 min, last 30 min @ 80–86 % | 168 | 210 | 0.69 |
| long_ride_late_ss | endurance | build threshold | long_ride | 210 min, 2 × 15 @ 88–92 % late | 172 | 210 | 0.70 |
| tempo_2x20 | tempo | base | hit | 2 × 20 @ 78–85 % | 59 | 70 | 0.71 |
| ss_3x_n | sweetspot | base build | hit | 3 × 10 @ 88–92 % (8–20 min, 2–4 reps) | 55 | 60 | 0.74 |
| ss_2x20 | sweetspot | base build | hit | 2 × 20 @ 88–92 % | 72 | 73 | 0.77 |
| ss_3x20 | sweetspot | build | hit | 3 × 20 @ 88–92 % | 97 | 93 | 0.79 |
| over_unders_3x_n | threshold | build threshold | hit | 3 × 3 × (2 m 93–96 / 1 m 105–110) | 66 | 65 | 0.78 |
| threshold_3x12 | threshold | build | hit | 3 × 12 @ 95–100 % | 81 | 79 | 0.78 |
| threshold_2x15 | threshold | build threshold | hit | 2 × 15 @ 96–101 % | 70 | 68 | 0.79 |
| threshold_2x20 | threshold | build threshold | hit | 2 × 20 @ 97–101 % (95–102 range) | 89 | 82 | 0.81 |
| threshold_3x20 | threshold | threshold | hit | 3 × 20 @ 95–99 % | 121 | 110 | 0.81 |
| vo2_5x3 | vo2 | threshold | hit | 5 × 3 @ 115–120 % | 59 | 59 | 0.77 |
| vo2_4x5 | vo2 | threshold | hit | 4 × 5 @ 110–115 % | 69 | 69 | 0.77 |
| vo2_5x5 | vo2 | threshold | hit | 5 × 5 @ 108–112 % | 79 | 79 | 0.78 |
| vo2_30_30_2sets | vo2 | threshold | hit | 2 × 12 × (30 s 125–130 / 30 s 50) | 57 | 57 | 0.77 |
| vo2_40_20 | vo2 | threshold | hit | 3 × 8 × (40 s 120–125 / 20 s 55) | 65 | 60 | 0.80 |
| anaerobic_8x1 | anaerobic | threshold | hit | 8 × 1 m @ 140–150 % | 56 | 63 | 0.73 |
| climb_repeats_goal_power | threshold | build threshold | hit | 4 × 7 m @ 110–115 %, outdoor only | 88 | 90 | 0.76 |
| ramp_test | test | base build threshold test | test | 1-min steps +8 %/min from 46 %, indoor only | 52* | 38* | — |
| ftp_test_20min | test | base build threshold test | test | 5 m opener + 20 m TT (freeride) | 72 | 71 | 0.78 |
| openers | opener | test | opener | 3 × 1 m fast pedal, 3 × 1 m @ 108 %, 1 × 30 s sprint | 34 | 50 | 0.63 |

\* ramp_test lists steps up to 182 % so the file is complete for a stronger athlete; the rider
stops when a step fails (expected around 134–150 %, ≈ 25–28 min and ≈ 35–40 TSS). The loader
recomputes every value from the YAML; this table is documentation, not a source of truth.
