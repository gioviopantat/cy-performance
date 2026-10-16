# ADR-0013 · Readiness reads HRV as a 7-day mean (readiness_v2)

**Status**: accepted · 2026-10-16 (decided by Roger after the 2026-10 literature review)

## Context
`readiness_v1` scored HRV as the z of *today's* ln(rMSSD) against the 60 days before, and
rested the athlete when that z was below −1.5 while resting HR was above +1.5 SD. The 2026-10
review ([research/2026-10.md](../research/2026-10.md) §2, §3 `readiness_v1.hrv_single_day`)
found that single nights carry little signal: Plews 2013 reports r = .72 between the weekly
mean ln(rMSSD) and fitness change against −.06 for single days, and every HRV-guided trial that
showed a benefit (Vesterinen 2016, Javaloyes 2019/2020; Manresa-Rocamora 2021 table 2) compared
a 7-day rolling mean with the baseline mean ± 0.5 SD. Plews 2014: three valid nights a week
are enough. Garmin's overnight HRV adds per-night noise (Dial 2025).

## Decision
`readiness_v2` (`readiness_v2.1` when post-ride RPE / feel is an input, same meaning as v1.1):
1. **Window**: the mean of ln(rMSSD) over today and the 6 days before; missing nights are
   skipped; fewer than 3 valid nights → no HRV component (weights renormalise, as for any
   missing input). Today's value is not required.
2. **Baseline**: the daily ln(rMSSD) values of the 60 days before today (≥ 14, as before);
   `z7 = (mean7 − mean_base) / sd_base`.
3. **Component** (weight 0.30, unchanged): 0 inside ±0.5 SD (the trials' normal band); outside
   it `sign(z7)·(|z7| − 0.5)/0.5`, clipped ±2. Each further 0.5 SD is one unit so the component
   keeps roughly the span of v1's single-day z.
4. **REST rule**: HRV component ≤ −1.5 (z7 ≤ −1.25) **and** resting-HR z > +1.5 → `REST`
   (status `OVERREACHED`). Same −1.5 line on the component scale, so it still means "HRV clearly
   suppressed and resting HR up today".
5. Yesterday's decoupling, HR lag and ride status are no longer inputs (ADR-0012). Everything
   else (sleep, TSB, subjective, load vs plan, RPE / feel, weights) is unchanged.
6. Stored verdicts carry `readiness_v2` / `readiness_v2.1`; v1 rows keep their version.

## Consequences
- One bad night no longer swings the score or forces rest; a real multi-day drop does. The
  7-day mean reacts later to an acute illness; resting HR, subjective scores and icu `SICK`
  events still catch that.
- The band mapping and the −1.25 SD rest line are our choices (grade C,
  `readiness_v1.hrv_single_day`, `readiness_v1.overreached_rule`); the trials only say "below
  the band → train easy".
- Removing the yesterday-ride signals removes a component that was usually a neutral 0. On days
  with few other inputs (no HRV, sleep or plan) the remaining ones, often TSB alone, now carry
  all the weight, so such days score lower. Replay: Roger's last 45 days (HRV, sleep and plans
  present) change 9 verdicts, none to `REST` (3 `REST` → `EASY` from single bad nights); his
  July days without wellness data and the dad profile gained `REST` days from that weighting,
  which ADR-0016 (shrinkage toward neutral) addresses.
