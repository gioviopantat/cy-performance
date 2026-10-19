# ADR-0025 · Ride intent: a short ease-off stays inside the rep; cut-short reps count minutes

**Status**: accepted · 2026-10-19 (Roger's ride 232: sweet spot 3x16, two sets in 32 °C, the
third skipped for the heat; graded "missed, 0/3 sets"). Revises `ride_intent_v1` to
`ride_intent_v1.1`.

## Context
Set 2 was 15.8 min at a mean 234 W (band 225–252 W). One 36 s stretch at 155 W (below the old
dip floor of 80 % × 225 = 180 W) split it into an 11.8-min and a 4-min block; neither held
80 % of 16 min, so no rep counted. Set 1, 10.7 min at 248 W before the heat ended it, gave
nothing either. Score: 17/48 in-band minutes = 36 %, "missed".

## Decision
- A dip of up to 120 s inside a block bridges whatever its power (the 80 % floor is removed).
  It counts as rep time, so the block's mean (which must stay ≥ the band floor) carries it.
  Bridging still stops once the block holds 80 % of a rep, so planned rests never join reps.
- A block whose mean holds the band and that lasts ≥ 50 % of a rep adds its minutes to a
  third score term, rep minutes / planned minutes (capped at the plan).
- Ride 232 now: 1 rep counted, 26.5 of 48 rep minutes, score 55 %, "partial".

## Consequences
- Outdoor rep workouts with traffic and short descents are judged by what the athlete held
  over the set, not by its worst 36 s.
- A ride of short surges cannot pass as a rep workout: blocks need the band's mean and half a
  rep's length.
