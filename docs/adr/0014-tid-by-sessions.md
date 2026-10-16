# ADR-0014 · TID phase targets count sessions

**Status**: accepted · 2026-10-16 (decided by Roger after the 2026-10 literature review)

## Context
`analysis/longitudinal/tid.py` compared each week's time-in-zone fractions (low Z1–Z2, mid
Z3–Z4, high Z5+) with phase targets 80/15/5 (base), 75/15/10 (build), 75/5/20 (threshold). The
2026-10 review ([research/2026-10.md](../research/2026-10.md) §3 `time_in_zone_tid.phase_targets`)
found 20 % *time* above threshold out of reach even for professionals (WorldTour riders spend
4–7 % of power time above CP, Mateo-March 2026; elite skiers about 10 % of time but 23 % of
sessions, Tønnessen 2014). The 75/5/20 shape comes from session-goal counting (Seiler &
Kjerland 2006); time-based and session-based shares differ about threefold in the mid/high
zones (Sylta 2014).

## Decision
1. Keep the targets; compare them with **session** fractions. Each ride counts once, in the
   zone of its dominant intensity: the ride classifier's `recovery` / `endurance` → low,
   `tempo` / `sweetspot` / `threshold` → mid, `vo2` / `race` → high. A `mixed` or unclassified
   ride (or one whose power meter is flagged unreliable) falls back on its time in zone:
   ≥ 10 min Z5+ → high, else ≥ 20 min Z3+Z4 → mid, else low.
2. Time-in-zone fractions, the polarization index and the pyramidal/polarized label stay in
   the output as reference only and are never compared with the targets.
3. `tid_v2`; the weekly explanation names the claim `time_in_zone_tid.phase_targets` (grade C)
   and reports both the mid and the high gap to the target.
4. The `grey_zone` limiter still reads the 4-week mid *time* share (> 30 %): it is about minutes
   ridden in tempo, not about how sessions are distributed.

## Consequences
- The weekly report shows sessions (e.g. "4 / 0 / 1 趟") against the target, time below it.
- Session counting follows the ride classifier, which calls a ride `vo2` once ≥ 15 % of its
  time is in Z5+. Replay of Roger's last 18 weeks: hilly outdoor rides often qualify, so
  several September weeks read 75–100 % high sessions where time-based high was 18–20 % (which
  had looked on target). The classifier's thresholds now matter more; they are unchanged here.
- Classification uses what was ridden, not the coach's intended session goal as in the
  literature.
