# Write guard refused to publish

`services/publish.check_write_guard()` writes only when the API key's owner (`GET /athlete/0`)
equals the profile's `icu_athlete_id` (or, without a profile, the athlete id the DB synced).
The same check runs before every intervals.icu sync of a profile (`jobs/sync._check_key_owner`,
message "…; not syncing"), so a wrong key never pulls someone else's rides into the profile.

- "belongs to iX but this profile is for iY": a key was pasted into the wrong profile. Fix the
  `.env`; **do not** edit `icu_athlete_id` to match unless the athlete really changed accounts.
- "unknown target athlete" (publish) or "has no icu_athlete_id … not syncing" (sync):
  `profile.yaml` has no `icu_athlete_id` (profiles never fall back to the sync cursor; both
  checks fail closed). Set it to the athlete's id (intervals.icu URL `/athlete/i…`), which
  `cyp profile add` prints.
- "INTERVALS_ATHLETE_ID=… but this profile is for …" (publish) or "…, but
  INTERVALS_ATHLETE_ID=…; not syncing" (sync): remove or fix that line in the `.env`.
- `cyp publish spike --confirm-write` with a profile runs the same guard (and needs
  `publish.calendar` on).
