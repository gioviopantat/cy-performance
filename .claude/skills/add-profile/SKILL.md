---
name: add-profile
description: Onboard a new athlete profile in cy-performance from an intervals.icu API key (goal, weekly availability, first dry run, promotion). Use when the user wants to add a family member or friend, or says "幫 X 建 profile".
---

# Add an athlete profile

1. Get from the user: a short name (lowercase), the athlete's intervals.icu API key, and the
   answers to `docs/onboarding-questions.md` (birth year, health screen, goal, minutes per
   weekday, ...). Never echo or commit the key; it lives only in
   `profiles/<name>/.env` (mode 600, git-ignored).
2. `uv run cyp profile add <name> --key <key> --goal-ftp <W> --availability "tue=60,...,sun=120" --birth-year <YYYY> --health none --distance-km <km> --yes`
   (the questions to ask first: `docs/onboarding-questions.md`)
   (downloads 365 days; reads FTP, weight, time zone and athlete id from intervals.icu).
3. Check the athlete's intervals.icu Garmin settings allow uploading planned workouts
   (`icu_garmin_upload_workouts`); if not, tell the user how to enable it.
4. `uv run cyp --profile <name> run --no-write` and show the user the plan
   (`uv run cyp --profile <name> plan --dry-run`).
5. Only after the user approves: `uv run cyp profile promote <name> --yes`.
6. Make sure the schedule is installed: `uv run cyp schedule status`.
