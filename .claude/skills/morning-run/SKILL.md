---
name: morning-run
description: Run or verify today's cy-performance autopilot for every profile and summarise each athlete's plan in zh-TW. Use when the user asks to "run today's plan", "check this morning's run", "幫我跑今天的課表" or similar.
---

# Morning run

1. `uv run cyp schedule status` and `uv run cyp profile list`. If today's run already happened
   (last run = today) and is `ok`, do not run again; go to step 3.
2. Otherwise run `uv run cyp run --all --json` (writes only for profiles in apply mode; that is
   the user's standing choice, do not change modes). Each output line is one profile's report.
3. For each profile summarise in zh-TW: stages that failed or were skipped and why, the plan
   change summary, whether the calendar was written. For details:
   `uv run cyp --profile <name> plan --dry-run` (today + next days) and
   `profiles/<name>/data/reports/daily/<date>.md`.
4. Any `failed` stage → follow the `diagnose-run` skill before reporting.
5. Never run `profile promote`, `plan --apply`, or edit an athlete's `athlete.yaml` unless asked.
