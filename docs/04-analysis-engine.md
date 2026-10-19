# 04 · Analysis engine

Pure functions: `(DataFrame streams, Activity, AthleteProfile, history) -> metrics`. No I/O, no
network. Everything versioned by `algo_version`; bumping it marks all `activity_metrics` stale.

## 1. Port from `strava-analyis`
The sibling project already implements and tests most per-ride metrics. Port (not import) into
`cyp/analysis/ride/` with polars-or-pandas DataFrames and the new `AthleteProfile`:

| Existing | New home | Change |
|----------|----------|--------|
| `metrics.streams_to_df`, `resample_power_1hz` | `ride/frames.py` | resample **all** streams to 1 Hz once at ingest, store Parquet |
| `metrics.normalized_power/intensity_factor/training_stress_score/power_curve/time_in_zones` | `ride/power.py` | zones from icu sport settings |
| `metrics.hr_drift_detail`, `pacing_variability`, `efficiency_factor` | `ride/durability.py` | add HR lag (already in `RIDE.LOG`), add aerobic decoupling per icu definition for cross-check |
| `segments.detect_climbs/detect_efforts` | `ride/climbs.py`, `ride/efforts.py` | add VAM, W/kg, grade bands, climb fingerprint for repeat-climb comparison |
| `estimate.estimate_power_series`, `hr_tss`, `estimate_ftp/lthr/max_hr/resting_hr` | `ride/estimate.py` | keep for the ~16 % of rides without power (Edge 530 / HRM-only) |
| `compare.baseline_comparison`, `official_segment_prs` | `longitudinal/compare.py` | baselines keyed by (intent, duration band) not just global |
| `trends.pmc/weekly_load/ef_trend/segment_leaderboard/pr_timeline` | `longitudinal/*` | PMC becomes *simulation*; icu is the ledger |
| `breakthrough.*` (segment limiter classification + prescription) | `longitudinal/limiters.py` | its `prescribe()` output becomes a **planner input** (limiter → template bias) |

## 2. Per-ride metrics (`activity_metrics`)
- Power: NP, IF, TSS, VI, kJ, power curve at {1,5,15,30,60,120,300,600,1200,1800,3600,5400} s,
  W/kg versions, time in 7 power zones, W′bal min if CP/W′ known (from icu `icu_pm_*` or our fit).
- HR: time in HR zones, HRR, EF (NP/avgHR), aerobic decoupling (first vs second half Pw:Hr),
  **HR lag** (cross-correlation lag between power and HR step responses), HR drift detail.
- Climbs: detected from smoothed altitude/grade; per climb: length, gain, avg/max grade, VAM,
  W/kg, HR, time; fingerprint (start latlng + length) to match repeats across rides.
- Efforts: ≥ 30 s above threshold bands; interval structure recognition (does the ride look like
  the planned workout?).
- Pacing: variability index, negative/positive split, surges count.
- Ride classification: `recovery | endurance | tempo | sweetspot | threshold | vo2 | race | mixed`
  from TIZ + IF, used for compliance and TID (training intensity distribution) stats.
- Status + next (the fields the athlete already reads in `RIDE.LOG`), from the ride's TSS only
  (ADR-0012, `ride-1.3.0`): TSS ≥ 250 → `OVERREACHED` (next `EASY`, `REST` at ≥ 300); TSS ≥ 150
  → `NORMAL` / `EASY`; else `NORMAL` / `AS_PLANNED`. Decoupling and HR lag are reported (and
  decoupling feeds the durability trend) but never set the status or the next day: in
  functional overreaching HR at a given power is *lower* and decoupling shrinks (Roete 2021,
  Le Meur 2013), and no study supports an HR-lag cut-off. Rows analysed before ride-1.3 may
  still read `FRESH` / `BLUNTED`.

## 3. Longitudinal
- **PMC replay** (`ctl_sim/atl_sim`, τ = 42/7, seeded from icu on `season.start_date`) over icu
  `icu_training_load` for *all* sports → must track icu ±1. Used by the planner to simulate
  candidate plans forward.
- **Load safety**: ramp rate (CTL Δ per 7 d), ACWR (7:28, uncoupled), Foster monotony and strain.
- **Power-duration model**: 2-parameter CP/W′ and 3-parameter (Pmax) fits on 42/90-day best
  efforts; compare with icu eFTP and `mmp-model`. Flag FTP change proposals (≥ 3 % for ≥ 2 weeks).
- **Durability trend**: decoupling and EF at matched intensity vs accumulated kJ (e.g. EF after
  1 000 kJ), tracked per block — the athlete's stated goal (GC-rider level) is primarily a durability
  and W/kg problem.
- **TID** (`tid_v2`, ADR-0014): each ride counts as one session. A planned ride counts by its
  planned workout's intent (recovery/endurance/opener → low, tempo/sweetspot → mid,
  threshold/vo2/anaerobic/test → high). An unplanned ride is high only with ≥ 10 min Z5+ and ≥ 3
  sustained bouts (≥ 60 s of 30 s power over the Z5 floor, `pacing.z5_bouts`); else ≥ 20 min
  Z3+Z4 is mid; else low. Weekly session
  fractions are compared with the phase targets; time-in-zone fractions and the polarization
  index are kept as reference only. The `grey_zone` limiter still reads 4-week mid *time*.
- **Climb repeats**: per fingerprint, best time, best VAM, W/kg trend, pacing pattern (from
  `breakthrough` limiter logic).
- **Weight / W/kg**: from icu wellness weight; smoothed 7-day.

## 4. Readiness (`readiness_daily`)
`readiness_v2` (ADR-0013; `readiness_v2.1` when post-ride RPE / feel is an input). Inputs
(z-scored against the athlete's 60-day baseline, missing → ignored, weights renormalised):
- HRV: the 7-day rolling mean of ln(rMSSD) (today and the 6 days before, missing nights
  skipped, ≥ 3 nights) as `z7` against the baseline's daily values; inside ±0.5 SD (the
  smallest-worthwhile-change band of the HRV-guided trials) the component is 0, outside it
  `sign(z7)·(|z7| − 0.5)/0.5`, clipped ±2. When the present inputs weigh < 0.5 in total, the
  shortfall counts as a neutral 0 (`S = Σwz / max(Σw, 0.5)`, ADR-0016). Resting HR z, sleep duration + score z (icu wellness)
- TSB (icu), yesterday's TSS vs plan (the plan = the WORKOUT events the calendar showed, else our live proposal), post-ride RPE / feel
- Subjective: soreness, fatigue, stress, mood, injury, `SICK`/`INJURED` calendar events
- Not inputs since v2: yesterday's decoupling, HR lag and ride status (ADR-0012)

Output: score 0–100, status, recommendation, explanation. Rules first (sick/injured → REST;
HRV component ≤ −1.5, i.e. z7 ≤ −1.25, and RHR z > 1.5 → REST; RPE / feel far worse than
expected or load > 130 % of plan → at most EASY), then a weighted score. Fully
transparent and logged so it can be tuned against how the athlete actually felt (`feel`, `icu_rpe`).

## 5. Compliance
For each `planned_workout` with a matched activity (`paired_event_id` from icu, else same-day best
match): duration ratio, TSS ratio, interval-by-interval target hit rate (power within ±5 % for
≥ 80 % of the work step), classification match, verdict `done | partial | modified | skipped`.
Feeds the planner (missed HIT → reschedule or drop, never stack) and the weekly report.

## 6. Reports
Jinja2 → Markdown and HTML into `data/reports/`. Daily: yesterday's ride analysis, readiness,
today's workout with rationale. Weekly: load vs plan, TID, PDC changes, durability, next week.
Optionally mirrored to an icu NOTE and the Strava description footer. The LLM narrator, when
enabled, rewrites the *same structured facts* in coach voice (zh-TW), never adding numbers.

## 7. Data quality: unreliable power

`data_quality.power_unreliable` in `config/athlete.yaml` lists power meters whose power is not
trusted up to a date: `{until, power_meter_serial (null = every meter), reason_zh}`. The old
`power_zeros_excluded_until` still works as a serial-less alias. Logic: `cyp.core.data_quality`.

- A ride with measured power on/before `until` is flagged when its icu `power_meter_serial`
  matches, or, without a serial, when its bike carried that meter before `until`.
- Flagged rides keep HR, time, distance and elevation. Their power is excluded from power
  curves, CP/W′ fits, cyp PDC snapshots, FTP evidence and proposals, climb power and W/kg
  trends, baselines and limiters. The flag and rule are stored in
  `activity_metrics.comparison` (`power_unreliable`, `power_unreliable_rule`) and quoted in the
  ride Explanation.
- Daily loads up to the latest `until` use our own TSS instead of icu's ledger (icu computed
  them from the same files), and icu's eFTP is not used while its lookback contains flagged
  rides.

Example: a crank meter reads ≈20 % high until a fix date (the same climb at the same HR gives
~15 % more watts before than after, and its W/bpm is ~20 % above a second meter's in the same
month, equal afterwards). Real entries live in the athlete's private `athlete.yaml`.

### Evidence for downward FTP judgements

"Did not ride hard" is not "cannot ride hard". The `sustained_power` limiter and any downward
FTP proposal need a near-maximal attempt in the last 42 days: a test, or ≥ 15 min at ≥ 100 %
FTP or ≥ 98 % LTHR. Sweet spot does not count. Without it the limiter reports
`insufficient_data` and the proposal is withheld. A downward proposal is also floored at
0.95 × the best 20-min power of the window.

## 8. Per-ride report models (docs/specs/ride-report.md)

Pure models behind the per-ride PDF. `reports/ride_facts.py` assembles `reports/ride_model.py`
from persisted results; the sections live in `reports/ride_intent.py` (2), `ride_goal.py` (3)
and `ride_sections.py` (4-7, series), sharing `ride_common.py`. Every threshold is a named
constant quoted, with its value, in the section's Explanation; athlete-facing text lives in
`reports/i18n/ride_report.zh-TW.yaml`. The planned stimulus comes from
`ResolvedWorkout.stimulus()` (`planning/templates.py`).

- **Intent v1.1** (`analysis/ride/intent.py`, `ride_intent_v1.1`, ADR-0025). *Work* intents
  (tempo … VO2): minutes of 30 s power in `[lo, hi × 1.10]` (band = the work band with the
  most planned time); reps are blocks of power ≥ `lo` that survive dips (≤ 120 s of easier
  pedalling, counted as rep time) and *excused* interruptions (≤ 120 s, mostly stopped /
  coasting / descending ≤ −2 %, not rep time), bridged only while the block is short of one
  counted rep so planned rests are never bridged; a block counts when its mean is ≥ `lo` and it
  holds 80 % of the rep length, and a block with that mean holding ≥ 50 % of a rep adds its
  minutes. Score = max(minutes ratio, reps ratio, rep minutes ratio); ≥ 0.9 achieved, ≥ 0.5
  partial. *Ceiling* intents (recovery,
  endurance) on 90 s power: time above `hi × 1.10` > 10 % → partial, > 25 % → missed; endurance
  with NP below the band floor is at most partial. No power band → load ratio decides; no
  trusted power → `no_data`.
- **Outdoor context** (`analysis/ride/context.py`, `outdoor_context_v1`): stops (not moving
  ≥ 15 s inside the ride), coasting % (moving seconds at 0 W), `temp_c` average / max, and
  caveats: heat ≥ 25 °C *and* ≥ 3 °C above the athlete's median of the last 28 days' outdoor
  rides (absolute floor only with < 3 such rides); ≥ 4 stops/h or ≥ 15 stopped min (interval
  structure, VI unreliable); coasting ≥ 25 % (average power understates the work).
- **Goal projection** (`analysis/longitudinal/goal.py`, `goal_projection_v2`): Theil–Sen line
  through the days the FTP estimate changed (last 8 weeks, not the forward-filled series); a
  date only with ≥ 4 weekly medians, ≥ 3 raw points, ≥ 3 week-over-week rises, a rising last
  half-window, Kendall τ ≥ 0.5, slope ≥ 0.25 W/week and the estimate level ≤ 5 % below the FTP
  setting. A date more than 26 weeks after the goal date is reported as "after the target date"
  without a date. The shift is the date change since the previous ride's projection.
- **Limiter minutes** (`reports/ride_sections.py`): 30 s power in % FTP per limiter (VO2
  ≥ 106 %, sustained 88-105 %, aerobic volume 56-76 %, grey zone 76-88 % where fewer is
  better), durability = moving minutes after 1 000 kJ.
