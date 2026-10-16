# Weekly / monthly digest email (`cyp digest`)

The digest agent (`com.cy-performance.digest`, `cyp schedule install --digest`) runs
`cyp digest run --all` daily at 07:00, after the 05:30 autopilot. Per profile with
`report.digest_weekly` and/or `report.digest_monthly` on, it builds every **due** digest without
a finished `digest_reports` row under `profiles/<name>/data/reports/digest/` and emails it when
`notify.email` is on (to `CYP_REPORT_EMAIL_TO`, copies to `CYP_REPORT_EMAIL_CC`; the greeting
uses `CYP_REPORT_EMAIL_NAME`; copy from `reports/email_copy.py`). Spec:
[docs/specs/digest-reports.md](../specs/digest-reports.md).

Due (profile time zone): the weekly for the ISO week Monday..Sunday from the next Monday 07:00;
the monthly for a calendar month from the 1st 07:00 of the next month. A Mac asleep at 07:00
runs the agent on wake and catches up on every period due in the last 14 days. On other days
the run does nothing (and leaves a `job_runs` row `digest` with no counts).

Start here:

```bash
uv run cyp schedule status                         # all three agents, last exit code
tail -50 ~/Library/Logs/cy-performance/digest.log
uv run cyp --profile <name> features               # report.digest_* and notify.email on?
uv run cyp --profile <name> digest run             # one run now: prints each period's action
sqlite3 profiles/<name>/data/cyp.sqlite \
  "select kind, period_start, status, attempts, sent_at, error from digest_reports
   order by period_start desc limit 10"
sqlite3 profiles/<name>/data/cyp.sqlite \
  "select key, value from schema_meta where key like 'digest.%'"
```

`status`: `built` PDF only (`notify.email` off), `sent`, `failed` (retried by the next run),
`gave_up` (3 failed attempts).

## No email arrived

1. `cyp digest run` prints `skipped: features report.digest_weekly and report.digest_monthly
   are off` -> switch the flags on in `athlete.yaml`.
2. Nothing listed for the period -> it is not due yet (Monday / the 1st before 07:00), or it
   is older than the profile's `digest.<kind>.first_enabled` date in `schema_meta`: the job
   only sends periods due on or after the day it first saw the flag on, so switching a flag on
   never mails a pile of old digests. Send one by hand (below).
3. Row `built` -> `notify.email` was off when it was built; resend (below).
4. Row `failed` / `gave_up`: `error` holds the last exception. `email not configured`,
   `SMTPAuthenticationError`, spam: see [watch.md](watch.md) (same SMTP settings). A build
   error (`NotImplementedError`, a missing table) is a code problem: reproduce with
   `cyp --profile <name> digest weekly --period <monday>` (no email).
5. `skipped: another run holds the profile lock` -> the run waited 20 minutes for the
   autopilot or a watch tick and gave up; the next day's run catches up (or run it by hand).

## Duplicate email

One row per kind + period (primary key `(kind, period_start)`), so the job cannot send a period
twice unless the row was deleted or someone ran `digest ... --send` (which always resends). A
crash between the SMTP send and the row commit would resend once on the next run;
`digest.log` shows `email.sent` without a following row update.

## Send, resend or backfill one period

```bash
uv run cyp --profile <name> digest weekly --period 2026-10-05          # build only, prints the PDF path
uv run cyp --profile <name> digest weekly --period 2026-10-05 --send   # build and email (records the row)
uv run cyp --profile <name> digest monthly --period 2026-09 --send
```

`--period` is the Monday of the ISO week (`YYYY-MM-DD`) or the month (`YYYY-MM`); without it,
the newest due period. `--send` needs `notify.email` on, refuses a period that has not ended,
and records the attempt on the row (`sent` again on success), so the job never sends that
period again. To let the job retry a `gave_up` period instead, set its row back to `failed`
with `attempts = 0` (it is retried only while it is within the 14-day catch-up window).
