# Strava problems

Strava is optional (`sync.strava`). For a profile without Strava, set `sync.strava: false` in its
`athlete.yaml`.

- 429: the run stops Strava early and keeps the cursor; the next run continues. Budget:
  `STRAVA_MAX_DETAIL_FETCHES` in the profile `.env`.
- Token invalid: `uv run cyp --profile <name> auth strava` (browser OAuth on port 8721).
- RIDE.LOG write refused (`ride-log push`, the 寫入 Strava button):
  - "lacks activity:write": add `activity:write` to `STRAVA_SCOPE` in the profile `.env`, then
    `uv run cyp --profile <name> auth strava`;
  - "token has no athlete id": authorise again (same command);
  - "belongs to athlete X, the ride to Y" or "cannot tell who owns this Strava ride": the token
    is someone else's, or Strava returned no owner. Never work around it: authorise as the
    ride's owner (an incognito window when another Strava account is signed in).
