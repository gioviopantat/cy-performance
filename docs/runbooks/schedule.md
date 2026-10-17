# Scheduled run missing or late

- `uv run cyp schedule status` → "not loaded": `uv run cyp schedule install [--at 05:30]` from the
  repo directory (the agent stores this working directory and the venv's python).
- Moved the repo or recreated `.venv` in another place: re-run `cyp schedule install`.
- Mac asleep at 05:30: launchd runs the missed job once on wake (late, not skipped).
- Output: `~/Library/Logs/cy-performance/autopilot.log`. Exit code 1 = some stage failed (see
  `cyp profile list`), 2 = no profiles.
