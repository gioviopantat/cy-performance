# ADR-0019 · Personal scales: RPE, feel and easy days are read on the athlete's own scale

**Status**: accepted · 2026-10-17 (decided by Lead for Roger, spec adaptive-planning D2, D5;
Roger: "我沒想法，請你協助我優化")

## Context
Readiness compared every rating with one table (`EXPECTED_RPE`: Z2 = 3) and one feel rule
(5/5 = bad). Roger rates a 2-hour Z2 a 1; dad rates almost every ride a 5
(docs/research/adaptive-planning-review.md §2.2). The same table misreads both. The planner also
planned easy days the athletes never ride (a 45-min spin; Roger's easy days are 60–140 min at
IF .62–.81), so the plan-vs-actual record says "overdone" for ordinary days.

## Decision
1. **Learn three descriptive scales** from the athlete's own history (`analysis/personal_scales.py`,
   flag `plan.personal_scales`, on by default), each with a minimum amount of data, shrinkage
   towards the default and hard bounds:
   - `rpe_offset[class]`: median(RPE − table) over 90 days, ≥ 5 rated rides per class (else the
     all-class median), shrunk n/(n + 5), clipped ±2. Readiness expects table + offset; a
     class outside the table (`mixed`) gets no offset (its expectation is already a guess).
   - `feel_baseline`: 90-day median feel (≥ 8 answers). A feel is bad only 2 points worse than
     it, and never stricter than the fixed rule (the baseline only relaxes).
   - `easy_day_tss` (D2): median load of the last 28 days' ridden non-hard days (≥ 6), shrunk to
     the Z2-60 template (k = 5), within default × [1, 1.8].
2. **Priority**: `athlete.yaml planner.scales` > learned > default (a written `all` offset is
   also the fallback of thin classes). Readiness reads the scales learned before the rated
   ride (the day before), so a ride never sets its own baseline. The morning run stores the
   values in the new table `learned_params(athlete, param, value, n, as_of, algo_version,
   source)` (D5); replays learn them as of each replayed day.
3. **Easy days**: an endurance / recovery day below the learned easy-day load becomes a 60–90
   min Z2 ride near it with an explicit IF cap 0.70 — not today when readiness is below
   AS_PLANNED or another adaptation changed today, not in a week whose ridden load already
   exceeds its plan to date, never below what was planned, and never for athletes ≥ 60 (their
   problem is intensity, not length; D2/D6). Guardrails run afterwards.
4. Readiness verdicts that used a non-zero offset or a learned feel baseline carry
   `readiness_v2.4`; each explanation says which personal baseline it used
   (`plan.learn.easy_day_tss.<date>` for easy days).

## Consequences
- Same inputs → same values (ADR-0004); with the flag off readiness and the planner are exactly
  as before (tests/services/test_adaptive_flags.py).
- All thresholds (90 d, 5, k = 5, ±2, 2 feel points, 28 d, 6 days, × 1.8, IF 0.70, 60–90 min) are
  ours: grade C, `personal_scales.rpe_offset`, `personal_scales.feel_baseline`,
  `planning.easy_day_habit` (proposed) in `core/evidence.py`.
- Offsets are learned per ride *classification*; a misclassified ride (dad's steady rides often
  come out tempo / mixed) moves the offset of the wrong class. Replay 2026-10-06..16: dad's
  learned offsets are 0 (n = 13), Roger's −1.0 (endurance, n = 6) / −1.2 (all, n = 8).
- The easy-day pool tops out at 90 min (≈ 66 TSS); a learned habit above that cannot be met by
  widening alone (Roger: 78 TSS learned) — the residual distribution already plans his easy days
  at 65–93 TSS.

## Amendment · 2026-10-17
Roger decided dad's easy days follow his habit too: athletes ≥ 60 now get the easy-day habit
with a lower cap, IF ≤ 0.67 (`adapt.MASTERS_EASY_DAY_IF_CAP`: plain Z2, nothing harder);
masters rules for hard days are unchanged.
