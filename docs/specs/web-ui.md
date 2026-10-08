# Web UI (local dashboard)
Status: done · 2026-10-07 (v1 plus the RIDE.LOG, feedback and Strava additions below)

## Goal
The owner can do everyday things with buttons instead of commands, for any profile:
- see today (readiness, today's workout, fitness trend);
- see the plan (next 14 days, the season, why each workout);
- look at rides (list, per-ride analysis, the RIDE.LOG metrics block), edit and save the
  RIDE.LOG text (e.g. add the acrostic poem), record the post-ride feeling, and write the text
  to the ride's Strava description after a preview and a confirmation;
- run the autopilot (dry run, or write to the calendar after a confirmation);
- see what the scheduled runs did.

## Non-goals
- No public hosting and no accounts: it runs on the owner's Mac (`cyp serve`, loopback). Remote
  access is ADR-0006 L4.
- No editing of athlete.yaml from the UI (v1). Profiles are added with `cyp profile add`.
- No LLM poem for RIDE.LOG (ADR-0004: the deterministic metrics block is generated; the athlete
  or the owner adds the poem by editing the text; a narrator comes later behind a flag).

## Design
- **Backend** (`api/`, `services/`):
  - **Profile selection.** Every `/v1` request may carry `X-CYP-Profile: <slug>` (fallback:
    `profiles/.default`). `api/deps.ctx` returns that profile's cached `AppContext`, so one
    server serves every athlete with the same isolation as `cyp --profile`.
  - New endpoints:
    - `GET /v1/profiles` (profile status rows);
    - `POST /v1/autopilot` `{write, confirm}` as a background job (`jobs.runner.run_profile`;
      `write=true` needs `confirm=true`, `planner.mode: apply` and flag `api.calendar_write`);
    - `GET /v1/autopilot/runs` (recent `autopilot` job rows with per-stage results);
    - `GET /v1/activities/{id}/ride-log` (WORKOUT + RIDE.LOG text, `services/ride_log.py`;
      the saved edited version when there is one, plus the generated one);
    - `POST /v1/activities/{id}/ride-log` `{text}` saves the edited text
      (`data/ride_logs/<id>.txt`, the previous version kept as `.prev.txt`); an empty text
      deletes it and goes back to the generated one;
    - `GET` / `POST /v1/activities/{id}/feedback` `{rpe, feel}`: post-ride RPE 1–10 and feel
      1–5 (intervals.icu convention, 1 = strong), `data/ride_feedback/<id>.json`; tomorrow's
      readiness uses it under flag `readiness.ride_feel`;
    - `POST /v1/activities/{id}/strava-description` `{text, confirm}`: without `confirm` a
      preview (`before` / `after`); with `confirm` the write (`services/strava_write.push`).
  - `cyp serve` serves the built UI from `web/dist` at `/` when it exists.
- **Frontend** (`web/`):
  - Vite + React + TypeScript (strict), TanStack Query for server state. Types come from
    `docs/api/openapi.json` via `openapi-typescript`, so the UI cannot drift from the API
    silently.
  - Plain CSS with light/dark tokens; zh-TW labels. No chart library: small SVG charts.
  - Screens: 今天 · 課表 · 紀錄 · 執行; a profile switcher and the planner-mode badge in the
    header.
- **Flags** (ADR-0008): `api.calendar_write` (default on) lets the UI's write button work for
  that profile; off = the button is hidden and the endpoint refuses. `strava.write_description`
  (default off) does the same for the Strava write; `readiness.ride_feel` decides whether the
  post-ride feeling changes readiness.
- **Client rules**: every API call takes the profile explicitly (the same value as in its query
  key); the RIDE.LOG editor never replaces text that has unsaved edits and does not refetch on
  window focus; deleting a saved RIDE.LOG asks first.
- **Layers**:
  - `services/ride_log.py` (L7);
  - `api/` (L8) gains a profile registry;
  - `web/` is outside the Python layering. It talks only to `/v1`.

## Safety
- The only calendar write is `POST /v1/autopilot` with `write=true`. The server requires
  `confirm=true`, apply mode, the flag and the usual write guard (`publish_plan`). The UI asks
  for an explicit confirmation naming the athlete.
- The only Strava write is `POST /v1/activities/{id}/strava-description` with `confirm=true`,
  through `services/strava_write.push` (flag, `activity:write` scope, token owner == ride owner;
  keeps the athlete's own text and replaces only our block). The UI enables 寫入 Strava only after
  a preview of the current text, and the confirmation names the athlete and the ride and shows
  the resulting description; it sends exactly the previewed text.
- Loopback by default; a non-loopback bind needs `CYP_API_TOKEN` (see ADR on web writes).

## Acceptance
- `uv run cyp serve` → http://127.0.0.1:8765 shows the UI. Switching profile changes every
  panel. A dry run from the UI produces the same stage report as `cyp --profile X run --no-write`.
- API tests:
  - profile header routing, including an unknown profile → 404;
  - the autopilot endpoint refuses `write` without `confirm`, in propose mode and with the flag
    off;
  - ride-log text for a seeded ride; save / restore of an edited text; stale ETags never
    served for `/ride-log`;
  - feedback round trip; the Strava write refuses without the flag, the scope or `confirm`.
- `npm --prefix web run check` (tsc + build) is part of `uv run poe check`. A test fails when
  `docs/api/openapi.json` is stale.

## Docs to update
docs/08-api.md, docs/01 §5.9 + layer notes, docs/architecture.c4 (web UI container), docs/09,
README (Quickstart), CLAUDE.md (map + gate).
