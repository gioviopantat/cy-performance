# Runbook: no network at 05:30 (`nodename nor servname provided`)

**Symptom.** Every stage that talks to intervals.icu fails with
`IngestError: intervals.icu GET /athlete/0: [Errno 8] nodename nor servname provided, or not known`
(sync and publish), for every profile, and the report's start time is later than 05:30.

**Cause.** The Mac was asleep. launchd starts the missed job as soon as the Mac wakes (often a
short "dark wake"), before Wi-Fi and DNS are back.

**What the code does.** `cyp run --all` first calls `jobs.runner.wait_for_network()`: it resolves
`intervals.icu` with back-off (5 s → 60 s) for up to 15 minutes, then runs anyway so a failure
still leaves a report and a `job_runs` row.

**Fix it now.**
1. `uv run cyp --profile <name> run --no-write --json` to confirm the network is back.
2. Ask the owner, then run the real thing: `uv run cyp run --all`.

**Prevent it.** Keep the Mac awake around 05:30: System Settings → Energy → "Prevent automatic
sleeping when the display is off", or schedule a wake:
`sudo pmset repeat wakeorpoweron MTWRFSU 05:25:00` (needs the owner's password; run it yourself).
