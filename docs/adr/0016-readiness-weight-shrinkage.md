# ADR-0016 · Readiness shrinks sparse days toward neutral

**Status**: accepted · 2026-10-17 (decided by Roger after the readiness_v2 replay)

## Context
Readiness drops missing inputs and renormalises the weights of the ones present
(`S = Σ wᵢzᵢ / Σ wᵢ`). In v1 the "yesterday's ride" component (weight 0.20) was almost always
present and usually exactly 0, because its HR-lag part was 0 for any lag ≤ 45 s. That neutral 0
diluted the other inputs. ADR-0012 removed decoupling and HR lag from readiness. On days without
a plan or post-ride feel there is now no ride component, and the inputs left, often TSB alone
(weight 0.15, grade C), carry the whole score.

The replay over 120 days (2026-06-19..10-16) showed the effect: REST days went 6 → 20 for Roger
and 23 → 34 for his dad. Every new REST day had only TSB, or TSB with resting HR (and sleep).
Example: Roger on 2026-07-13 had only TSB ≈ −39 (component −1.29): v1 scored 36 (EASY), v2
18 (REST). Dad on 2026-09-09 had resting HR +2 SD and TSB component −0.54: EASY → REST.

Options considered:
- (a) Reinstate a neutral 0 ride component. This writes an unmeasured signal into the rules.
- (b) Allow no REST from the score without HRV or sleep. This is a new rule, and RHR + TSB days
  would still rest.
- (c) Shrink toward neutral.

## Decision
`S = Σ wᵢzᵢ / max(Σ wᵢ, 0.5)`. When the present components weigh at least 0.5, nothing changes.
Below that, the shortfall counts as a neutral 0: TSB alone at −1.29 gives S = −0.39 (40,
EASY). The explanation says how much was treated as neutral. The claim
`readiness_v1.missing_shrinkage` is grade C (proposed): 0.5 is our choice. The algorithm stays
`readiness_v2` (unreleased when this was decided).

## Consequences
- One grade-C input can no longer force REST on its own; a sparse day reads closer to "as
  planned / easy".
- A truly bad sparse day (e.g. resting HR far up with nothing else) is also pulled toward the
  middle. The hard REST rule (HRV and RHR) and `SICK` events are unaffected.
- Days with HRV, sleep or a plan (Σw ≥ 0.5) score exactly as before.
