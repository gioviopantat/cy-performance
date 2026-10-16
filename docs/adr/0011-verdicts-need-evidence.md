# ADR-0011 · Verdicts need A/B evidence

**Status**: accepted · 2026-10-16

## Context
Every number we show comes from a model, but the models differ widely in how much science
stands behind them: critical power is physiological consensus, ACWR's injury prediction is
contested and comes from team sports, several thresholds are our own choices. Reports used to
state them all with the same confidence, e.g. "ACWR > 1.3: injury risk rises"
([specs/research-review.md](../specs/research-review.md)).

## Decision
1. Every claim a model relies on is graded A/B/C with citations in `src/cyp/core/evidence.py`,
   mirrored in each glossary file's `## 證據等級` table (a test keeps them equal).
2. A verdict or warning may rest on A/B claims; numbers backed only by C are still shown,
   labelled 參考（證據弱）, and never phrased as a conclusion.
3. Grades change only through the monthly literature review (`.claude/skills/research-review`),
   which proposes and never edits; the owner approves each change. A change that would alter a
   threshold or a planner rule needs its own ADR (ADR-0004).

## Consequences
- Reports are more honest and slightly more hedged.
- New models start as grade C with `status="proposed"` until reviewed.
- The registry is one more place to update when a model is added (enforced by tests).
