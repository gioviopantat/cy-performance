# ADR-0021 · First LLM use (RIDE.LOG narration) and OpenStreetMap route lookups

**Status**: accepted · 2026-10-17 (Roger: a casual, varied RIDE.LOG with route and next-spot
ideas; OSM approved; spec casual-and-strava §C)

## Context
The Strava RIDE.LOG printed model jargon nobody read. Roger wants a casual post that knows where
the ride went and suggests places, with an acrostic poem. That needs place names (no local data
has them) and free text (templates alone repeat).

## Decision
1. **Narration only** (ADR-0004 holds): Claude phrases lines and writes the poem from a facts JSON
   the deterministic code built (content type, OSM place names, numbers) and rephrases the
   deterministic draft lines: its lines may use only the places and numbers of the draft. It never computes a
   number or chooses a place; the output is validated (numbers and 「names」 ⊆ facts, acrostic,
   no Latin letters, length) and dropped when invalid. Flag `llm.ride_poem`, default off; key
   `ANTHROPIC_API_KEY` per profile. Model `claude-opus-5-5`, effort low, ≤ 1024 output tokens,
   timeout 30 s, 2 SDK retries, server-side fallback on refusal.
2. **Cost**: one call per ride, a second one only when the first broke a rule (it is told which);
   measured 2026-10-17: ~800 input + ~150 output tokens per call, so ≈ US$0.006 per call on
   `claude-opus-5-5` ($4 / $20 per MTok) and at most ≈ US$0.012 per ride (under US$1 a month for
   two athletes; less on a cheaper `CYP_LLM_MODEL`). Cached per ride: a resend costs nothing.
3. **OSM** (flag `strava.route_story`, default off): Overpass and Nominatim with
   privacy-trimmed coordinates only (1 km around start and end removed, 200 m sampling, no
   timestamps or ids), ≤ 1 request/s, an identifying User-Agent, results cached per ride. ODbL:
   place names are facts from OSM; the post carries no map data.
4. **Failure mode**: any OSM or LLM failure, refusal or invalid output gives the deterministic
   RIDE.LOG (no route details, no poem); it never blocks the report, the email or the Strava
   write, and is logged (`ride_story.*`).
5. Only `services/ride_story.prepare()` calls out (the watcher before B's write, and
   `cyp ride-log show --fetch`); displaying a RIDE.LOG never does.

## Consequences
- A new runtime dependency (`anthropic`) and two external services, both optional.
- Tests never touch the network (stubbed clients; the socket guard stays).
- The poem rules (acrostic, literary Chinese, 甜蜜點) are code now, not only a habit.
