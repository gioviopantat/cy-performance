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
