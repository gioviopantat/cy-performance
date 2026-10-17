# ADR-0018 · A weekly progression gate decides when intensity goes up, holds or comes down

**Status**: accepted, shipped **off** (flag `plan.progression_gate`) · 2026-10-17 (decided by
Lead for Roger, spec adaptive-planning D1-B, D3, D8; Roger: "我沒想法，請你協助我優化")

## Context
The season skeleton advances every loading week's progression index by one step whatever
happened; daily readiness only changes today (an UPGRADE does not carry into next week). Over
weeks nothing learned from completion, so a good week and a bad week led to the same next
week (docs/research/adaptive-planning-review.md §1, §5 Phase 2). The best evidence for when to
push and when to back off is adjusting load by recovery state (grade B: Kiviniemi 2007/2010,
Nuuttila 2022's down / hold / up model); the exact thresholds are ours.

## Decision
1. **Each Monday** the gate (`planning/gate.py`) decides the week that starts that day from the
   week that just ended, from stored data only: key sessions (hard / test / long ride slots and
   their `planned_workouts.compliance`), the mean personal RPE delta (ADR-0019 offsets), the
   7-day HRV band (ADR-0013), readiness EASY/REST days, completion (load / the week's target)
   and wellness fatigue z.
2. **Rules** (first match): back from a break (≥ 10 days without riding → hold and −1 step;
   > 21 days → −2); down (≥ 2 key sessions missed with RPE delta ≥ +2, or HRV below band ≥ 5
   days, or ≥ 3 EASY/REST days → −1 step and TSS × 0.85), turned into an early recovery week
   when the week before was also down or fatigue z ≥ 1.5; up (≥ 2 key sessions all achieved,
   RPE delta ≤ 0, HRV within or above band, ≤ 1 EASY/REST day, completion 0.85–1.15 → +1 step,
   the season's normal advance; two easy ups in a row, both RPE delta ≤ −1 → +1 extra, ≤ 2 per
   block); hold otherwise (no advance, the season's TSS).
3. **Bounds**: the progression index stays within [1, season index + extra steps taken in the
   block] and resets at each block start; season recovery and test weeks are planned as
   scheduled. Guardrails run after every change, as before.
4. **Fully automatic** (D3: no approval UI yet), conservative for masters (missing RPE / HRV or
   a value exactly on an "up" line does not raise; no extra steps).
5. **Weekly accumulation** (D1-B): from Friday, a week whose load so far is ≥ 15 % above its plan
   so far eases its remaining easy days by that share (≥ 60 % of their load kept).
6. **Explained**: each decision is an Explanation `plan.gate.<monday>` (in the plan revision and
   as the week's adaptation) and the weekly digest / its email gets a section quoting it
   (`WeeklyDigest.plan_reason`); accumulation days `plan.gate.accumulation.<date>`.
7. **Deterministic chain**: the decisions are recomputed every morning from the season start
   (each finished week's target as the planner set it), so a replay gets what the live run got.
8. **Ships off.** It turns on per profile only after the replay acceptance (spec D8) passes and
   Roger agrees.

## Consequences
- Replay 2026-05-01..10-16 with a season moved to 2026-05-04 (both athletes, read-only,
  `cyp plan replay --season-start 2026-05-04`, `CYP_FEATURES=plan.progression_gate=on`):
  never raised while readiness had ≥ 3 EASY/REST days, never put the index above its bound,
  added no guardrail violation; but it **never raised at all** (Roger: hold 17, down 3, early
  recovery 3; dad: hold 8, down 6, early recovery 5, back from break 4). Before 2026-10 there was
  no plan the athletes followed, so key sessions are rarely "achieved"; holds therefore keep the
  progression at step 1 all season. The "hold weeks within ±10 % of the season target"
  criterion fails with and without the gate (guardrails and daily adaptations, not the gate).
  Results are in the PR; the lead decides with Roger.
- All thresholds are grade C (`core/evidence.py` `planning.progression_gate`, proposed).
- Not done here: re-planning the block's 3:1 rhythm after an early recovery week (the next
  season recovery week still comes on schedule), Phase 3's per-block parameters.
