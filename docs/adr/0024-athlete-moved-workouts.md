# ADR-0024 · A workout the athlete drags to another day stays there

**Status**: accepted · 2026-10-19 (Roger dragged Tuesday's sweet spot to Monday in
intervals.icu). Extends ADR-0005 (idempotent publishing).

## Context
Our calendar events carry the planned day in their `external_id`
(`cyp:<season>:<YYYY-MM-DD>:<slot>`). The diff read an event's day from that id, so an event
the athlete had moved would be moved back, rewritten or deleted by the next morning's
publish; reports and compliance looked the plan up by its planned day, so the ride was judged
against Monday's rest and Tuesday counted as skipped.

## Decision
- An event of ours whose `start_date_local` differs from the day in its `external_id` was
  moved by the athlete. The diff never updates, moves or deletes it (counted as `declined`).
- Reports, the weekly digest and compliance judge that planned workout on the day the event
  sits on (`PlannedWorkoutRepo.moved_day` / `moved_onto`).
- The ride watcher's light sync also reads calendar events, so the move is known before the
  ride's report is built.
- The planner counts this week's past hard rides (not tagged casual) against the week's hard
  sessions (`PlanContext.done_hard`), so the moved workout is not scheduled a second time.

## Consequences
- The planner still plans the emptied day from availability; to keep it free the athlete (or
  Claude) sets `availability.dates: {<day>: 0}`.
