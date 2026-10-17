# ADR-0012 · Ride status without decoupling or HR lag

**Status**: accepted · 2026-10-16 (decided by Roger after the 2026-10 literature review)

## Context
The post-ride verdict (`RIDE.LOG` `STATUS` / `NEXT`, `analysis/ride/durability.py`) used two
heart-rate rules besides TSS: reliable aerobic decoupling ≥ 10 % → `OVERREACHED`, and HR lag
≥ 90 s → `BLUNTED` (next day `EASY`). Decoupling ≥ 5 % also capped the next day at `EASY`, a
low decoupling with a short lag made the ride `FRESH` (next day `UPGRADE` under 80 TSS), and
readiness scored yesterday's decoupling, HR lag and `BLUNTED` status.

The 2026-10 review ([research/2026-10.md](../research/2026-10.md) §1, §3) found the
decoupling rule points the wrong way: in functional overreaching HR at a given power is *lower*,
maximal HR is lower and HR recovery faster (Roete 2021 systematic review, Le Meur 2013,
Aubry 2015), so decoupling tends to shrink. High decoupling more likely means heat,
dehydration, low glycogen or a poor-durability day. No study links one ride's decoupling to the
next day, and no study uses a 90 s cross-correlation lag as a fatigue cut-off (fatigue changes
HR acceleration and recovery, Bellenger 2016).

## Decision
1. The ride's status and next-day recommendation come from its TSS only: ≥ 250 →
   `OVERREACHED` (next `EASY`, `REST` at ≥ 300); ≥ 150 → `NORMAL` / `EASY`; else `NORMAL` /
   `AS_PLANNED`. The rules `FRESH`, `BLUNTED`, "decoupling ≥ 10 % → OVERREACHED" and
   "decoupling ≥ 5 % → EASY" are removed; so is the `UPGRADE` a `FRESH` ride could give.
2. Decoupling stays a durability signal: computed, stored, shown with Friel's 5 % as a
   practitioner reference line, and fed to the durability trend. HR lag stays displayed.
   Neither changes the status or tomorrow's session.
3. Readiness no longer scores yesterday's decoupling, HR lag or status (part of
   `readiness_v2`, ADR-0013).
4. Ride algorithm `ride-1.3.0`: every stored ride is re-analysed on the next run; readiness
   verdicts are recomputed with their own version.

## Consequences
- Fewer next-day downgrades from a hot or long ride, and no more `UPGRADE` from a ride that
  merely looked steady. Replay over Roger's last 120 days: 20 of 124 rides change verdict
  (8 `EASY` → `AS_PLANNED` from 5–9 % decoupling, 4 `OVERREACHED` → `NORMAL` from 10–17 %
  decoupling on 52–84 TSS rides, 8 `FRESH` → `NORMAL`).
- Older rows may still read `FRESH` / `BLUNTED` until re-analysed; the types keep both values.
- The TSS cut-offs are still our own (grade C, `decoupling_hr_lag.ride_status_rules`).
- Supersedes nothing; ADR-0004 (deterministic planner) and ADR-0011 (verdicts need evidence)
  still apply.
