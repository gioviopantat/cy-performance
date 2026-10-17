# ADR-0020 · The ride watcher may write the RIDE.LOG to Strava when the profile opts in

**Status**: accepted · 2026-10-17 (Roger's decision: "Strava 描述騎完自動更新")

## Context
The RIDE.LOG reaches a ride's Strava description only through `services/strava_write.push()`,
and until now every write needed an explicit per-ride confirmation (`cyp ride-log push
--confirm-write`, or the web button with `confirm=true`, ADR-0009). Roger wants the description
updated by itself after each ride, as the email report already is (ADR-0010).

## Decision
1. New flag `strava.auto_description` (default off, requires `strava.write_description`). Turning
   it on for a profile **is** the confirmation for that profile's rides; it is a human step in
   `athlete.yaml`, never done by the autopilot or an agent without the athlete's go-ahead.
2. With it on, the ride watcher (`jobs/watch.py`), right after a ride's report is done (sent, or
   built with email off), calls `strava_write.auto_push()`, which calls `push(..., write=True)`.
   `push()` stays the only Strava write; every other gate is unchanged: flag
   `strava.write_description`, a Strava id, the `activity:write` scope, token owner == ride owner
   (fails closed), the athlete's own text kept and only our block replaced, no write when the
   description already holds the text.
3. The text is what the UI shows: the saved RIDE.LOG (e.g. with the poem) if any, else the
   generated one.
4. Failures never block or change the email: the outcome goes to new nullable columns
   `ride_reports.strava_status` / `strava_error` (`written` | `unchanged` | `failed` |
   `gave_up`), is logged (`watch.strava`), retried once on the next tick, then given up with a
   macOS notice. No backfill of rides reported before the flag was on.
5. The interactive commands keep their confirmation, and `.claude/hooks/ask_before_writes.sh`
   is unchanged: an agent still asks before `ride-log push --confirm-write`.

## Consequences
- CLAUDE.md's Strava invariant names this one exception.
- A profile with the flag on gets one Strava API read and at most one write per new ride.
- A rejected gate (missing scope, another athlete's token) shows as `strava_status = gave_up`
  after two ticks; the fix is in `docs/runbooks/watch.md`.
