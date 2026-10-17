# ADR-0010 · Per-ride report: poll intervals.icu, deliver by email

**Status**: accepted · 2026-10-16

## Context
The athlete wants a report after every ride without asking for it ([specs/ride-report.md](../specs/ride-report.md)).
Rides reach intervals.icu directly from the Edge. intervals.icu and Strava both offer webhooks,
but only to a registered OAuth app with a public HTTPS endpoint; this project uses per-athlete
API keys on a Mac behind NAT, and opening a public endpoint would add an attack surface for one
convenience. The report also needs a delivery channel that works on a phone.

## Decision
1. **Poll, not webhooks.** A second launch agent (`com.cy-performance.watch`) runs
   `cyp watch --all` every 10 minutes. Each tick takes the profile lock (skips if the morning run
   holds it), syncs only recent activities, and checks the API key owner first, like every sync.
2. **Send once, after the rating.** One `ride_reports` row per ride. A ride is sent on the first
   tick after its RPE/feel exists, or 2 h after it was first seen; rides without streams wait up
   to 6 h. Failed builds or emails retry up to 3 times, then a macOS notice.
3. **Email is the only new outbound write.** SMTP over TLS with credentials and one recipient
   from the profile's own `.env` (never inherited, never from any other source); flags
   `report.ride` and `notify.email` default off.
4. **The watcher writes no calendar and no Strava.** Tomorrow's plan in the PDF is a dry run;
   the 05:30 autopilot still owns the real replan.

## Consequences
- Up to 10 minutes of latency (a web rating triggers the tick at once).
- No public endpoint; nothing to secure beyond SMTP credentials in a git-ignored file.
- A Mac that sleeps delays reports until it wakes (launchd runs the missed tick).
- Revisit if a hosted deployment (M6) gets a public URL: webhooks would then be cheap.
