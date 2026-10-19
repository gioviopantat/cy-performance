# ADR-0009 · Writes from the local web UI

**Status**: accepted · 2026-10-08

## Context
The web dashboard (`docs/specs/web-ui.md`) can now start writes that reach real accounts: the
autopilot with `write=true` (an athlete's intervals.icu calendar, then their Garmin) and a
ride's RIDE.LOG to its Strava description. Until now every such write went through a CLI flag
that a person typed (`--confirm-write`) and that the Claude Code hook guards. An HTTP endpoint
is reachable by anything that can talk to the server: other processes on the Mac, a browser tab
on another site (CSRF), a page that rebinds its DNS name to 127.0.0.1, and anyone on the
network if the server binds beyond loopback.

## Decision
1. **Same gates as the CLI, enforced in the service layer.** The endpoints call
   `services/publish.publish_plan` (via `jobs.runner.run_profile`) and
   `services/strava_write.push`; every gate of CLAUDE.md applies unchanged (feature flags,
   apply mode, write guard, needs-review refusal, Strava scope and owner check, both failing
   closed). The API adds nothing that bypasses them.
2. **Explicit confirmation per write.** `write=true` needs `confirm=true` in the same request;
   the UI asks first, naming the athlete (and the ride for Strava). Flag `api.calendar_write`
   (per profile) turns the calendar button and endpoint off.
3. **Loopback by default; a token for anything else.** `cyp serve` binds 127.0.0.1. Binding to
   another address is refused unless `CYP_API_TOKEN` is set; then every `/v1` request needs
   `Authorization: Bearer <token>`. The token and CORS list are server settings
   (`settings.SERVER_SETTINGS`): read from the process environment and the root `.env`, never
   from a profile.
4. **Host check against DNS rebinding.** A loopback server without a token answers only
   `Host: localhost`, `127.0.0.1` or `::1` (`TrustedHostMiddleware`), so a page on another
   domain that resolves to 127.0.0.1 gets 400.
5. **CSRF stance: JSON bodies only.** Write endpoints take `application/json` bodies (pydantic
   models); a cross-site HTML form can only send form encodings or `text/plain`, which fail
   validation (422), and a cross-site `fetch` with JSON needs a CORS preflight that only the
   configured dev origins pass. No cookies are used, so there is no ambient credential.
6. **One run at a time per profile.** Calendar-touching endpoints take the profile's autopilot
   lock (`jobs.runner.profile_lock`); a busy profile answers 409 or fails the job, never
   interleaves with the 05:30 run.

## Consequences
- The UI on 127.0.0.1 needs no login; remote access (ADR-0006 L4) needs the token and a UI that
  sends it (not built yet).
- Any new write endpoint must reuse a service-layer write function and come with a test that it
  refuses without `confirm` and with its flag off (`tests/api/`).
- The Claude Code hook also asks before `curl`-style POSTs to these endpoints (ADR-0007
  amendment), so an agent cannot use the API to skip the CLI's confirmation.
