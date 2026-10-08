---
name: diagnose-run
description: Diagnose a failed or missing cy-performance autopilot run (schedule, stage failure, write guard, lock, config) and propose or apply the fix. Use when a run failed, a plan did not reach the calendar, or the user says the workouts are missing.
---

# Diagnose a run

1. Evidence, in this order (read-only):
   - `uv run cyp schedule status`; `tail -80 ~/Library/Logs/cy-performance/autopilot.log`
   - `uv run cyp profile list`
   - `uv run cyp --profile <name> doctor`
   - `uv run cyp --profile <name> run --no-write --json` (safe reproduction)
   - `profiles/<name>/data/logs/cyp.jsonl` (structlog JSON; grep the stage or `error`)
2. Match the message to `docs/runbooks/README.md` and follow that page.
3. A code bug: write a failing test first, fix, `uv run poe check`, update docs per
   `CLAUDE.md` ("Always update docs with code"), then re-run step 1's dry run.
4. Report: root cause, what you changed, what the user must do (e.g. a new API key), and
   whether today's plan is now on the calendar.
