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
1. Keep the targets; compare them with **session** fractions. Each ride counts once:
   - **Planned rides** (Roger, 2026-10-17) count by the intent of the planned workout they
     executed. Pairing is the same as the ride report: the slot matched to the ride, else the
     day's first live slot for the day's longest ride. Intents map as recovery / endurance /
     opener → low, tempo / sweetspot → mid, threshold / vo2 / anaerobic / test → high. This
     is the session-goal method of the literature.
   - **Unplanned rides** fall back on what was ridden:
     - high only with ≥ 10 min in Z5+ **and** ≥ 3 sustained bouts (30 s power at or above the
       Z5 floor for ≥ 60 s, stored as `pacing.z5_bouts` since ride-1.3.0), so scattered climb
       spikes do not make a hard session;
     - else ≥ 20 min Z3+Z4 → mid;
     - else low.
     A ride whose power meter is flagged unreliable has no bouts and cannot be high.
2. Time-in-zone fractions, the polarization index and the pyramidal/polarized label stay in
   the output as reference only and are never compared with the targets.
3. `tid_v2`; the weekly explanation names the claim `time_in_zone_tid.phase_targets` (grade C)
   and reports both the mid and the high gap to the target.
4. The `grey_zone` limiter still reads the 4-week mid *time* share (> 30 %): it is about minutes
   ridden in tempo, not about how sessions are distributed.

## Consequences
- The weekly report shows sessions (e.g. "4 / 0 / 1 趟") against the target, time below it.
- A first version counted by the ride classifier (`vo2` once ≥ 15 % of time is in Z5+). Hilly
  outdoor rides qualified, and several of Roger's September weeks read 75–100 % high sessions.
  Planned intent plus the bout rule replaced it before release.
- A planned VO2 day ridden easy still counts as high: the session goal decides, as in the
  literature. The ride report's intent check (section 2) shows whether the stimulus was
  delivered.
- The 10 min / 3 bouts / 60 s / 20 min cut-offs are ours (grade C with the phase targets).
