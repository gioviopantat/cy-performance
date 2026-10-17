# ADR-0017 · An overdone day eases the next one; it does not force a recovery spin

**Status**: accepted · 2026-10-17 (decided by Roger: "超課表隔天就一定 45 恢復騎，這樣很不智能")

## Context
"Yesterday's load > 130 % of its plan → today becomes a 45-min recovery spin" was applied twice
(planner `adapt.apply_yesterday` and readiness, which also capped the day at EASY and scored a
`load_vs_plan` sub-signal) and compared against the *adapted* plan. Once a day was a 19-TSS
spin, any normal easy ride was "overdone", so the next day became a spin again. Replay of
2026-10-06..16 (docs/research/adaptive-planning-review.md §0): Roger 7 of 9 ridden days judged
overdone, dad 4 of 5; plans the athletes did not follow and weekly load far below target.

## Decision
1. **Judge against the slot's own target**: the season plan's load for that day before any
   adaptation (or the stored plan, whichever is larger).
2. **Overdone needs factor and margin**: actual > max(1.3 × target, target + 40 TSS)
   (`adapt.is_overdone`, shared by planner and reports).
3. **Ease, don't replace**: the next day loses 50 % of the excess, keeps at least 60 % of its
   planned load, and a hard session becomes endurance; only an excess above 100 TSS caps it at
   an easy hour of Z2 (~45 TSS). A recovery spin comes only from readiness (body signals).
4. **Readiness judges the body only**: the load-vs-plan cap and sub-signal are removed (TSB
   already carries load); RPE and feel stay (readiness_v2.2 / v2.3).

## Consequences
- The loop is gone (replay 2026-10-17: Roger's Saturday goes from a 45-min spin to the planned
  Z2 120 min after a 74-TSS easy ride).
- 1.3, 40, 50 %, 60 %, 100 and 45 are our choices (grade C, `core/evidence.py`
  `planning.overdone_easing`, proposed); the weekly progression gate
  (docs/specs/adaptive-planning.md) will be the place that learns from completion over weeks.
