# ADR-0004 · Deterministic rule/optimizer planner; LLM limited to narration and bounded proposals

**Status**: accepted · 2026-10-02

## Context
The plan is written automatically to a calendar that pushes workouts to the athlete's head unit.
Mistakes cost real fatigue or injury. The athlete wants a serious, trustworthy system.

## Decision
- All load targets, workout selection and parameters come from explicit rules + closed-form PMC
  arithmetic + a small greedy repair loop, versioned and unit-tested, with a simulation harness.
- Hard guardrails run after *any* proposal, including human or LLM edits.
- The LLM (Claude, opt-in) may write the coach narrative from structured facts, and may propose
  bounded edits (swap/shorten/drop/note, ≤ 2/day) that re-enter the guardrails and are logged.
  It never computes TSS, FTP, zones or durations.
- Default mode is `propose` (diff only). `apply` is an explicit opt-in after M4 validation.

## Consequences
- Replayable, explainable plans; regressions caught by simulation.
- Less "magic" than an end-to-end LLM coach; narrative layer recovers the human feel.
- Template library quality becomes the main lever for plan quality; invest there.
