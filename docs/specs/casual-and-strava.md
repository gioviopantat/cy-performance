# casual-and-strava
Status: accepted · 2026-10-17 (Roger: "加「休閒騎」標記", "Strava 描述騎完自動更新")

Two additions after a coffee ride (大古山 with Sherry, "來回都隨便騎") that the ride report judged
against the planned Z2 180 min.

## Goal
- **A. Casual-ride tag.** The athlete marks a ride 休閒騎 (web 騎完感受 dialog, or
  `cyp ride-feedback <id> --casual`). The ride is then not graded: the ride report shows
  「休閒騎（不評分）」 instead of missed / partial, the email says so warmly, digests count it as
  casual. Its load still counts everywhere (TSB, week totals). For the planner the day's planned
  session was not done (a missed hard session is re-slotted by the existing rule) and the
  overdone rule never fires on it.
- **B. Automatic Strava RIDE.LOG.** With flag `strava.auto_description` on, the watcher writes the
  ride's RIDE.LOG into its Strava description right after the ride's report is done, through the
  one existing write path `services/strava_write.push()`.

## Non-goals
- No automatic resend: tagging a ride casual after its report was emailed only changes what
  `cyp ride-report <id>` builds next; nothing is sent again.
- No feature flag for the tag: it is an explicit per-ride answer of the athlete, and a profile
  that never tags a ride behaves exactly as before.
- The tag does not change the RIDE.LOG text, readiness or the personal scales (a casual ride's
  RPE is still a real rating).
- No Strava backfill: rides reported before the flag was turned on are not written.
- The flag is not turned on for any profile by this change.

## Design
### A. Casual tag
- **Store** (layer 7, `services/ride_feedback.py`): `data/ride_feedback/<id>.json` gains
  `"casual": true`. `RideFeedback.casual: bool`; `save(..., casual=None)` keeps the stored value
  when `None`; `set_casual(ctx, id, casual)` (CLI); `casual_ids(ctx)` and
  `casual_days(ctx, ds)` (a day is casual when every ride of that day is tagged).
- **Interfaces** (layer 8): `RideFeedbackIn/Out.casual` (`schemas.py`, OpenAPI + web types
  regenerated); web toggle 「休閒騎（不評分）」 next to RPE / feel; CLI
  `cyp ride-feedback ACTIVITY [--casual | --not-casual]` (no option: print the answers).
- **Ride report** (layer 5): `RideInputs.casual`; `build_intent` returns the same figures with
  `verdict="casual"` and a neutral headline (the athlete's tag is the first reason). `Verdict`
  gains `casual`; the headline (section 1) says 休閒騎 also for an unplanned ride. Email copy
  (`reports/email_copy.py`, `i18n/email.zh-TW.yaml`): case `casual` (subject, conclusion,
  verdict line). `services/ride_report.build` passes the tag.
- **Digests** (layer 5): `DigestInputs.casual` (ids, from `services/digest`); `SessionVerdict`
  gains `casual` (contract change: one literal); `session_verdict` returns it before the overdone
  check. Casual sessions are left out of the judged share and of the email's done/planned count
  (mentioned separately), and listed in the headline counts.
- **Planner** (layer 5): `Adaptive.casual_days` (from `services/adaptive.plan_inputs`, also in
  `cyp plan replay`); `Yesterday.casual`: a casual yesterday counts as not done (a hard plan is
  re-slotted whatever the load) and never overdone. `ds.loads` is untouched, so CTL/ATL/TSB
  and week totals keep the ride's load.
- **Compliance** (layer 7 → 5): `compliance_record(..., casual=)` adds `"casual"`; a casual slot
  has `intent_verdict="casual"` and `overdone=false`.

### B. Automatic Strava RIDE.LOG
- **Flag** `strava.auto_description` (group publish, default off, `requires=("strava.write_description",)`).
- **Watcher** (`jobs/watch.py`, layer 7): after `ride_report.send` returns ok (sent, or built
  with email off) and the flag is on, `services/strava_write.auto_push(ctx, id)` builds the text
  (`ride_log.build(...).text`: the saved RIDE.LOG if any, else the generated one) and calls
  `push(..., write=True)`. Every gate of `push` applies unchanged (flag `write_description`,
  Strava id, `activity:write` scope, token owner == ride owner, keep the athlete's text, replace
  only our block; an unchanged description is not written again).
- **Evidence**: `ride_reports.strava_status` / `strava_error` (nullable, migration):
  `written` | `unchanged` | `failed` (retried once on the next tick) | `gave_up`. Every outcome is
  logged (`watch.strava`) and appears in the tick's `RideTick` list (`strava_written`,
  `strava_failed`, …). Any exception from the Strava step is caught there: the email was already
  sent and the report row stays `sent`.
- No new caller of the Strava API: `push()` stays the only write.

## Safety
- Calendar: untouched. Strava: written only by `push()`; the flag is the per-profile
  confirmation (ADR-0020), both flags default off, and the scope + owner gates still fail
  closed. The CLI and web writes keep their explicit confirmation; the hook
  `.claude/hooks/ask_before_writes.sh` is unchanged.
- The tag writes only a local JSON file.

## Acceptance
- `uv run poe check` and `TZ=UTC uv run pytest -q` green.
- Tests: casual vs not in the intent / headline / email / digest verdicts, the planner re-slot and
  the overdone rule, compliance; the store round trip (API + CLI); flag on/off for the watcher
  push; scope and owner refusals recorded as failed then gave_up; idempotency (second push writes
  nothing, no double block); a Strava failure does not change the sent email or row status.
- Roger's 2026-10-17 rides 228 and 229 tagged with `set_casual`, their PDFs rebuilt with
  `cyp --profile roger ride-report <id>` (no `--send`).

## Docs to update
`docs/09-features.md`, `docs/01-architecture.md`, `docs/architecture.c4`, ADR-0020, CLAUDE.md
(Strava invariant, commands), README, `docs/runbooks/watch.md`, `docs/specs/web-ui.md`,
`docs/specs/ride-report.md`, `docs/02-integrations.md` (Strava write).

## C. A casual RIDE.LOG for Strava (2026-10-17)

Roger: "ridelog 也希望你優化一下 現在的幾乎沒有任何意義" (style chosen: 社群輕鬆版) and "我希望是在
strava上的內容都是休閒的 比如判斷騎行路線以及推薦下次踩點等等 希望很多樣化".

### Goal
The Strava description becomes a short, friendly post: 2–4 casual lines about the ride (where it
went, a stop, a climb, a number of the day, a spot to try next), a 4-line acrostic poem in
literary Chinese, and `#cyperformance`. Training analysis (verdicts, plan, tomorrow) stays in the
PDF and the email only.

### Non-goals
- No training talk on Strava: no 達成/未達成/騎過頭, no plan, no tomorrow, no model jargon
  (decoupling, HR lag, TSB, STATUS). The old `📋 WORKOUT` block is dropped (it was plan talk).
- No invented places: every place name comes from OpenStreetMap data of this ride.
- No other people's names (a casual ride says 跟朋友, never who).
- No Strava write here: writing stays `strava_write.push()` (manual, or B's opt-in flag).

### Design
- **Text** (`services/ride_log.build`): one block, markers unchanged so `strava_write.compose`
  still replaces only our block (old WORKOUT + RIDE.LOG + poem blocks included):
  ```
  ⚙️ RIDE.LOG
  <2–4 casual lines>
  > <poem line 1>
  > … (4 lines, only with llm.ride_poem)
  #cyperformance
  ```
  A saved text (`data/ride_logs/<id>.txt`) still wins everywhere. `RideLog.workout` is empty
  (API field kept for the web client).
- **Facts** (`reports/ride_story.py`, layer 5, pure): `StoryFacts` from the ride report the PDF
  uses (`reports/ride_facts.build_ride_report`; the stored facts JSON beside the PDF when there
  is one, so the numbers match it) plus the route places. Content types: `route`, `climb`,
  `stop`, `next_spot`, `weather`, `number`, `season`. The type is picked deterministically: a
  rotation by activity id over the types this ride has material for, skipping the previous ride's
  type (no two consecutive posts with the same type). A casual ride (tag A) prefers `stop`, then
  `route`, and adds a 跟朋友 line. Copy: `reports/i18n/ride_story.zh-TW.yaml`, variants rotated
  by activity id. The acrostic head (4 characters) is picked from a list per content type,
  skipping heads used by the last 10 stories.
- **Route** (`ingest/osm/`, layer 3; flag `strava.route_story`, default off): from the latlng
  stream. **Privacy zone**: every point within 1 km of the start or the end is dropped before any
  request; the rest is downsampled to one point per 200 m and rounded to 5 decimals; no
  timestamps, no athlete id, no activity id ever leaves. Overpass (one `around:150` query on the
  route line: `amenity=cafe`, `shop=beverages|tea|coffee|bubble_tea`, `tourism=viewpoint|attraction`,
  `natural=peak`, `leisure=park`, `amenity=place_of_worship` with a name) and Nominatim reverse
  geocoding (zh-TW; at most 3 points: the turnaround and two along the way) for district names,
  ≤ 1 request/s, `User-Agent: cy-performance/<version> (+CYP_OSM_CONTACT)`; the contact is an
  optional setting, never hard-coded. The turnaround is the kept point farthest from the start;
  the longest stop (from the report's stops) is matched to the nearest drink/café POI within
  150 m. Results are cached per ride (`data/ride_story/osm/<id>.json`); any failure means a
  RIDE.LOG without route details.
- **Next spot** (`next_spot`): one more Overpass query (viewpoints, peaks, cafés with a name) in
  the bounding box of the last 90 days' cached routes (already privacy-trimmed), minus places in
  the per-profile visited record (`data/ride_story/visited.json`, every POI a past route passed).
  One unvisited place is picked deterministically, with why it fits (its kind and the distance
  from this ride's turnaround).
- **Narration** (`llm/narrator.py`, layer 7; flag `llm.ride_poem`, default off, needs
  `ANTHROPIC_API_KEY`): one call to `claude-opus-5-5` (`settings.cyp_llm_model`), effort low,
  max_tokens 1024, timeout 30 s, SDK retries 2, server-side fallback
  (`server-side-fallback-2026-07-01`, `fallbacks="default"`), structured output
  `{lines: [...], poem: [...]}`. Input: only the facts JSON (content type, place names, numbers,
  casual, the head) and the rules (acrostic, literary Chinese, rhymed, no English, no rap,
  甜蜜點 never 甜點). `stop_reason` refusal / max_tokens, an API error, or an invalid output →
  the deterministic lines and no poem. Validation (`reports/ride_story.validate`): 2–4 lines of
  ≤ 60 characters, exactly 4 poem lines of 5–9 characters whose first characters spell the head,
  no Latin letters except `km`, `m`, `W`, `°C`, every number appears in the facts, every
  「name」 is a facts place, no POI-like word (咖啡、茶、寺、宮、廟、公園、步道、觀景台) outside a
  facts place, none of 甜點 / 三八 / 幹.
- **Cache** (`data/ride_story/<id>.json`): the chosen type, head, lines, poem and source; a
  story made with the network is reused as is, so a resend is identical.
- **When the network runs**: only `services/ride_story.prepare()` calls OSM or Claude (each
  behind its flag): the watcher before the automatic Strava write (B), and
  `cyp ride-log show --fetch`. Showing a RIDE.LOG (web, CLI) never calls out.

### Safety
No calendar or Strava write. OSM gets privacy-trimmed coordinates only (test). Claude gets the
facts JSON only, never Strava data or streams (docs/02 Strava terms). Both default off; failures
degrade to the deterministic text. ADR-0021.

### Decisions taken without asking (Roger runs bypass mode, lead 2026-10-17)
- The WORKOUT block is dropped (plan talk); its only casual fact (distance/time) moves into the
  lines.
- `cyp_llm_model` default becomes `claude-opus-5-5` (it was unused).
- The poem has no deterministic fallback: without the LLM there is no poem.
- The next-spot area is the bounding box of cached, privacy-trimmed routes (no extra stream reads).

### Acceptance
Tests stub Overpass, Nominatim and Claude (no network): privacy zone (no coordinate within 1 km
of start/end leaves), cache (second prepare makes no request), variety (no two consecutive
types), invented-name rejection, acrostic / Latin / number checks, refusal → no poem, compose
replaces an old WORKOUT + RIDE.LOG + poem block exactly once. Texts for activities 226, 228,
229 shown in the report (OSM live, LLM only if the key is set; no Strava write).
