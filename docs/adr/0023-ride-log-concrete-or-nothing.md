# ADR-0023 · The RIDE.LOG says something concrete or nothing

**Status**: accepted · 2026-10-18 (Roger, on the ADR-0022 posts: "這內容有點弱智…有趣一點…這樣很不
明確"). Supersedes the content types of ADR-0022; its "suggestions, not a diary" rule stays.
The poem is off for Roger (`llm.ride_poem: false`).

## Context
The ADR-0022 lines were vague ("那段常爬的坡", "十月很好騎") because the report's climbs carry a
generated label instead of a name, and the season and tip lines fit any ride.

## Decision
- Every line carries a real name or number:
  - `climb`: only a climb that matches one of the athlete's configured climbs
    (`location.climbs`, distance within 12 %, grade within 1 point), with its rank among their
    ascents and the gap to their fastest ("1 分 52 秒"); a fastest ascent says so.
  - `next_spot`: the OSM place, the compass direction and distance from the turnaround, its
    elevation when it is a tagged peak.
  - `stop`: the café / drink shop where the ride stopped.
  - `weather`: only below 12 °C or from 30 °C, with the temperature.
- The `season` and `tip` types are removed. A ride with none of these (an indoor ride, a ride
  with no route lookup) gets no RIDE.LOG: the watcher leaves Strava alone (`strava_unchanged`).
- The report's climb section carries `distance_km` and `grade_pct` for the matching.

## Consequences
- Fewer posts, each one specific. Stories are versioned `ride_story_v3`.
- Naming a climb needs the athlete's climbs in athlete.yaml (Roger has five; dad none yet).
