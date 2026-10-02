# ADR-0005 · Idempotent calendar publishing via `external_id` and diff-first writes

**Status**: accepted · 2026-10-02

## Context
intervals.icu `POST /athlete/{id}/events/bulk` supports `upsert` on `external_id` (and `uid`),
plus `PUT /events/bulk-delete`. The athlete also creates their own events (races, notes, workouts
from other sources) on the same calendar.

## Decision
- Every event we create carries `external_id = cyp:{season_id}:{YYYY-MM-DD}:{slot}` and tag `cyp`.
- Publishing computes desired state for the horizon, reads back current `cyp:` events, and emits
  the minimal create/update/delete set. Events without our prefix are read-only to us.
- After writing we read back `icu_training_load` and verify it is within ±10 % of our target;
  otherwise the run is marked `needs_review` and no further automatic publishes happen until a
  human runs `cyp plan --apply --force` or fixes the template.
- Mutability window: past days never; today only before a configurable cut-off (10:00 local).
- Max 20 events per run; dry-run is the default.

## Consequences
- Re-running the daily job is safe; crashes mid-publish converge on the next run.
- The athlete can edit or delete any of our events; the next run treats a deleted `cyp:` event
  as "athlete declined" for that day (recorded as `status=skipped_by_athlete`) rather than recreating it.
- M3 spike must confirm `upsert=true` semantics under API-key auth (the spec ties `upsert` to the
  OAuth app; fallback is `upsertOnUid=true` with `uid = external_id`).
