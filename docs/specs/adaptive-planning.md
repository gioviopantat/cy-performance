# adaptive-planning
Status: accepted · 2026-10-17

Design source: [docs/research/adaptive-planning-review.md](../research/adaptive-planning-review.md)
(phases, options, literature). Phase 0 part 1 (the overdone loop) shipped as ADR-0017.

## Goal
The plan gets more personal every week and raises or lowers intensity at the right time,
from what each athlete actually did and how their body responded, explained in plain zh-TW in
the reports, deterministic and replayable (ADR-0004), verdicts on A/B evidence (ADR-0011).

## Non-goals
- No fitted dose-response model driving load (review Phase 4: not now; maybe after ≥ 3 tests,
  report only).
- No automatic FTP change (stays a human step).
- No new weekly feedback question yet (dad would not answer; revisit with the web UI).

## Decisions (made by Lead for Roger, 2026-10-17: "我沒想法，請你協助我優化"; Roger may override)
| # | Question (review §7) | Decision |
|---|----------------------|----------|
| D1 | Overdone rule | A + C shipped (ADR-0017); B (weekly accumulation) joins the progression gate below |
| D2 | Easy days | Planned from the athlete's own habit: median load of their last 28 days' non-hard days (≥ 6 days, shrunk to the template default, bounds default × [1, 1.8]), as Z2 60–90 min with an explicit IF cap 0.70; only when readiness ≥ AS_PLANNED and the week is not ahead of target. Masters (≥ 60): the habit too (Roger, 2026-10-17), IF cap 0.67 (plain Z2, nothing harder) |
| D3 | Gate automation | Fully automatic (no approval UI exists yet), conservative bounds, every change explained in the weekly digest and its email; dad's profile always takes the conservative branch on ties |
| D4 | Weekly feedback question | Not now |
| D5 | Learned parameters | New table `learned_params(athlete, param, value, n, as_of, algo_version, source)`; priority yaml > learned > default |
| D6 | Dad's intensity | Revised by Roger 2026-10-17: his easy days follow his habit (D2), intensity stays conservative (IF cap 0.67, masters rules for hard days unchanged) |
| D7 | Personal dose-response model | No |
| D8 | Validation | A read-only `cyp plan replay --from --to` first; the gate ships behind a flag that turns on only after the replay acceptance below passes |

## Design (phases, each its own commits)
### Phase 0b · record plan vs actual, replay
- Write `planned_workouts.compliance` each morning for yesterday: `{actual_tss, ratio,
  reference_tss, intent_verdict, overdone, rpe, feel, rpe_delta, skipped}` (intent verdict from
  `ride_intent`, the same function the reports use; moved below `reports` if needed so planning
  can call it without a layer violation).
- `cyp plan replay --from YYYY-MM-DD --to YYYY-MM-DD [--json]`: re-runs readiness and the
  planner day by day on stored data; prints per-week planned vs actual, overdone / skipped
  counts, gate outcomes. Writes nothing.

### Phase 1 · personal scales (flag `plan.personal_scales`, default on)
- `rpe_offset[class]`: median(RPE − expected) over 90 days, ≥ 5 per class, shrink k = 5, ±2;
  readiness's RPE surprise uses it.
- `feel_baseline`: median feel over 90 days (≥ 8); a feel counts as bad only ≥ 2 worse than it.
- `easy_day_tss` (D2).

### Phase 2 · weekly progression gate (flag `plan.progression_gate`, default off until D8 passes)
Each Monday (in the 05:30 run) decide next week from last week, per review §5 Phase 2:
- Up: key sessions ≥ 2/2 achieved, mean personal RPE delta ≤ 0, HRV 7-day in band or above,
  ≤ 1 EASY/REST day, completion 0.85–1.15 → progression +1 step (two "up" weeks in a row with
  RPE delta ≤ −1 → one extra step; at most +2 extra per block).
- Down: ≥ 2 key sessions missed with RPE delta ≥ +2, or HRV 7-day below band ≥ 5 days, or ≥ 3
  EASY/REST days → progression −1 step and week TSS × 0.85.
- Early recovery week: two "down" weeks, or "down" with subjective fatigue z ≥ +1.5.
- Back from a break ≥ 10 days without riding (dad 10/16–10/25): first week hold and −1 step;
  > 21 days −2 steps.
- Hold otherwise. Weekly accumulation (D1-B): a week already ≥ 15 % over target by Friday
  eases the remaining easy days by the overshoot share.
- Every decision is an Explanation with its inputs; the weekly digest gets a section "下週為什麼
  這樣排" quoting it.

### Phase 3 · per-block learned parameters (later, its own spec revision)
ramp target, 3:1 vs 2:1, recovery-week cut, intensity preference, checkpoint handling, per
review §5 Phase 3; proposals stored in `learned_params`, applied at the next block.

## Safety
No new write path: plans still reach the calendar only through `publish_plan()` with the write
guard. Guardrails run after every change. Flags allow turning each phase off per profile.

## Acceptance
- Phase 0b: replay 2026-10-06..16 shows overdone days Roger ≤ 1 (was 7), dad 0 (was 4); the
  compliance column filled for every planned day from the next morning on.
- Phase 1: Roger's easy days in the replay are planned within ±20 % of what he actually rides;
  dad's are unchanged.
- Phase 2 (before turning the flag on): replay since 2026-05 (both athletes) — no week where
  the gate raises while readiness had ≥ 3 EASY/REST days; the gate never exceeds guardrails;
  planned weekly TSS within ±10 % of the season target in "hold" weeks. Results in the PR.
- `uv run poe check` green; ADR per phase; docs/05, glossary, 09-features, 01, LikeC4 updated.

## Docs to update
`docs/05-training-engine.md`, `docs/glossary/readiness_v1.md`, `docs/glossary/guardrails.md`,
`docs/09-features.md`, `docs/01-architecture.md`, `docs/03-data-model.md` (learned_params,
compliance), ADR-0018 (progression gate), README/CLAUDE.md (`plan replay`).
