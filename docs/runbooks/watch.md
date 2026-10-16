# Ride report email (`cyp watch`)

The watch agent (`com.cy-performance.watch`, `cyp schedule install --watch`) runs
`cyp watch --all` every 10 minutes. Per profile with `report.ride` on, one tick syncs the last
days' intervals.icu activities, then for each ride of today and yesterday without a finished
`ride_reports` row it waits for the streams (up to 6 h) and for the rating (RPE / feel in
intervals.icu or the web 騎完感受, up to 2 h), then builds the PDF under
`profiles/<name>/data/reports/ride/` and emails it when `notify.email` is on. Spec:
[docs/specs/ride-report.md](../specs/ride-report.md).

Start here:

```bash
uv run cyp schedule status                         # both agents, last exit code
tail -50 ~/Library/Logs/cy-performance/watch.log
uv run cyp --profile <name> features               # report.ride and notify.email on?
uv run cyp --profile <name> watch                  # one tick now, prints each ride's action
sqlite3 profiles/<name>/data/cyp.sqlite \
  "select activity_id, status, attempts, first_seen_at, sent_at, error from ride_reports
   order by first_seen_at desc limit 10"
```

`status`: `pending_feel` waiting (streams or rating), `built` PDF only (`notify.email` off),
`sent`, `failed` (retried on the next tick), `gave_up` (3 failed attempts, or never retried).

## No email arrived

1. `cyp watch` prints `skipped: feature report.ride is off` -> switch the flags on in
   `athlete.yaml` (`report.ride: true`, `notify.email: true`).
2. The ride is not listed -> it has not reached intervals.icu yet, it is a manual entry, or it
   started before yesterday (the watcher only looks at today and yesterday). Build it by hand:
   `uv run cyp --profile <name> ride-report <activity_id> --send`.
3. `wait_streams` / `wait_feel` -> working as designed: it is sent when the rating is saved,
   2 h after the watcher first saw the ride when unrated, and without streams after 6 h.
4. Row `built` -> `notify.email` was off when it was built; resend (below).
5. Row `failed` / `gave_up` with `email not configured` -> the profile's `.env` lacks one of
   `CYP_SMTP_HOST`, `CYP_SMTP_PORT`, `CYP_SMTP_USER`, `CYP_SMTP_PASSWORD`,
   `CYP_REPORT_EMAIL_TO`. The athlete types these in; never paste a password into a chat.
6. `SMTPAuthenticationError` -> Gmail needs an App Password (2-step verification on), not the
   account password.
7. Sent but not in the inbox -> look in spam; the From address is `CYP_SMTP_USER`.

## Duplicate email

One row per ride (primary key `activity_id`), so the watcher cannot send a ride twice unless the
row was deleted or someone ran `ride-report --send` (which always resends). Two rows for the
"same" ride means two activities: the ride was uploaded twice to intervals.icu (delete the
duplicate there). A crash between the SMTP send and the row commit would resend once on the next
tick; `watch.log` shows `email.sent` without a following row update.

## Retry stuck

A `failed` row is retried every tick until its third attempt, then it becomes `gave_up` with a
macOS notice (`notify.macos`). `error` holds the last exception. A tick that finds the profile
lock held (the morning run) is skipped and leaves a `job_runs` row `watch` with
`skipped_locked`; the next tick catches up. Rows stay `pending_feel` at most 6 h; a
`pending_feel` row older than that means the watcher is not running (`cyp schedule status`).

## Resend

```bash
uv run cyp --profile <name> ride-report <activity_id>          # rebuild the PDF only, prints its path
uv run cyp --profile <name> ride-report <activity_id> --send   # rebuild and email again
```

`--send` needs `notify.email` on and records the attempt on the row (`sent` again on success).
To let the watcher retry a `gave_up` ride instead, set its row back to `failed` with
`attempts = 0`.
