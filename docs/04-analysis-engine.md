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
- Status + next (the fields the athlete already reads in `RIDE.LOG`):
  `status ∈ {FRESH, NORMAL, BLUNTED, OVERREACHED}`, `next ∈ {REST, EASY, AS_PLANNED, UPGRADE}`.

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
- **TID**: weekly polarization index, Z1/Z2/Z3 distribution vs the block's target model.
- **Climb repeats**: per fingerprint, best time, best VAM, W/kg trend, pacing pattern (from
  `breakthrough` limiter logic).
- **Weight / W/kg**: from icu wellness weight; smoothed 7-day.

## 4. Readiness (`readiness_daily`)
Inputs (each z-scored against the athlete's trailing 30/60-day baseline, missing → ignored, weights
renormalised):
- HRV (rMSSD, ln-transformed) z, resting HR z, sleep duration + score z (from icu wellness)
- TSB (icu), ramp rate, yesterday's TSS vs plan (the plan = the WORKOUT events the calendar showed, else our live proposal)
- Yesterday's ride durability: decoupling, HR lag, HR blunting (low HR for power → `BLUNTED`)
- Subjective: soreness, fatigue, stress, mood, injury, `SICK`/`INJURED` calendar events

Output: score 0–100, status, recommendation, explanation. Rules first (sick/injured → REST;
HRV z < −1.5 and RHR z > 1.5 → REST; BLUNTED yesterday → EASY), then a weighted score. Fully
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
