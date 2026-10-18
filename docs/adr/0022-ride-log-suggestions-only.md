# ADR-0022 · The Strava RIDE.LOG gives suggestions, not a diary

**Status**: accepted · 2026-10-17 (Roger: "不要幫我寫任何敘事的話 不要幫我寫日記 寫你的建議就好
然後不用太認真"). Supersedes the content types, the summary line and the district lookup of
ADR-0021; its narration rules, privacy zone and validation stay. Content types superseded by
ADR-0023.

## Context
The first live posts (rides 226, 228, 229) told the ride back to the athlete: where it went, the
distance, "跟朋友一起". The athlete knows what he rode; the text guessed wrong (it was not a
friend) and read like someone else's diary on his own activity.

## Decision
- The lines are 1–2 light suggestions for next time: a next spot, a stop worth revisiting, a
  way to ride a climb, a weather or seasonal tip, or a general tip (`tip`, always available).
  No summary of the ride, no "who came along", no route retelling.
- Content types: `next_spot`, `stop`, `climb`, `weather`, `season`, `tip`; the `route` and
  `number` types and the summary line are removed.
- No Nominatim: district names were only used to retell the route. OSM is Overpass only.
- The poem stays (flag `llm.ride_poem`).

## Consequences
- One external service less; one Overpass query pair per ride.
- `StoryFacts` loses `districts` and `route_places` (the allow-list test follows).
- Stories are versioned `ride_story_v2`; cached v1 stories are rebuilt on the next show.
