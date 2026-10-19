# ride-report
Status: accepted · 2026-10-16

## Goal
Within ~15 minutes of a ride reaching intervals.icu (the Edge 850 uploads there directly), the
athlete gets an email with a zh-TW PDF: text plus charts that answer what intervals.icu, Strava
and Garmin do not. Nobody has to say "I rode" first.

Each section must earn its place by **combining our plan, our longitudinal models and the
athlete's own feedback**. A chart that intervals.icu already draws (power/HR timeline, power
curve, fatigued power curve after N kJ, planned-vs-actual trace overlay, zones) is not
repeated unless it carries our verdict on top of it.

| # | Section (zh-TW in the PDF) | What it answers | Why it is not in icu / Strava / Garmin |
|---|---------------------------|-----------------|------------------------------------------|
| 1 | 一句話結論 | Did this ride do its job, and what changes now | Needs our plan's purpose + verdicts; others show numbers, not a verdict |
| 2 | 目的達成度 | Planned stimulus vs achieved: minutes in the target zone, reps that counted, with outdoor interruptions excused (v1: time in target zone + load ratio; the full intent model is the follow-up spec `outdoor-intent`) | icu overlays traces; it does not judge whether the stimulus was reached on a road with lights and descents |
| 3 | 離 300W 還有多遠 | FTP evidence from this ride (`analysis/longitudinal/ftp.py`), season target line, projected date and how this ride moved it | The season goal and its trajectory live only in `athlete.yaml` |
| 4 | 外騎情境卡 | Stops (count, minutes), coasting %, temperature (the `temp` stream is already ingested), climbs; which numbers these make unreliable today | Others report the raw values but never discount the verdict for them |
| 5 | 同一條坡比一比 | Each climb vs its history (`ClimbBoard`), with the kJ already spent before it and the temperature | Strava segments compare time only, not power at the same fatigue/heat |
| 6 | 感受 vs 數據 | RPE / feel against what this ride class and length predict (`readiness.ride_feel` model); shown only if the athlete rated it, else a link to rate it | Needs our expected-RPE model |
| 7 | 弱點帳本 | Which limiter (`limiters.py`) this ride trained, and the week's running total | Our limiter model |
| 8 | 明天會怎麼排 | A **dry-run** preview of tomorrow from the deterministic planner + the adapt explanation (why it changed) | Our planner |
| 9 | 方法與證據 | Model ids, glossary links and evidence grade of every number above | Our explainability layer (docs/07) |

Numbers come only from the facts dict; an LLM may later narrate section 1, never compute
(ADR-0004).

## Non-goals
- No calendar write. The watcher never publishes; tomorrow is a dry-run preview. The only
  Strava write is the opt-in RIDE.LOG after the report (`strava.auto_description`, ADR-0020,
  [casual-and-strava.md](casual-and-strava.md)).
- A ride the athlete tagged casual (休閒騎) is not graded: verdict `casual`, a neutral
  headline and email ([casual-and-strava.md](casual-and-strava.md)).
  The 05:30 autopilot still owns the real replan.
- No webhook receiver and no public endpoint (see Design → detection).
- No Garmin Connect API (needs business approval; the Edge already syncs to intervals.icu).
- No new analysis models in this spec beyond what sections 2 and 4 need in v1.

## Design

### Detection: poll, not webhook
intervals.icu offers webhooks (ACTIVITY_UPLOADED / ACTIVITY_ANALYZED) but only to a registered
OAuth app with a public URL; we use API keys on a Mac behind NAT. Strava webhooks need a public
URL too. So: a second launch agent runs `cyp watch --all` every 10 minutes
(`StartInterval: 600`). Decision recorded in a new ADR-0010.

Per profile (one subprocess each, like `run --all`):
1. Non-blocking `profile_lock`; held (morning run) -> skip, try next tick.
2. Light sync: intervals.icu activities of the last 2 days + their streams (existing ingest).
3. New rides = synced rides with streams and no `ride_reports` row. A ride whose streams are not
   there yet is retried next tick (icu still processing), max 6 h, then reported without them.
4. Per new ride: per-ride analysis (existing pipeline) -> facts -> charts -> PDF -> email ->
   `ride_reports` row (`activity_id`, `pdf_path`, `sent_at`, `status`, `error`).
5. Every tick leaves a `job_runs` row (kind `watch`), like every other run.

### Modules (layer table)
| Module | Layer | Role |
|--------|-------|------|
| `store/models.py` + Alembic migration | 2 | `ride_reports` table |
| `reports/ride_facts.py` | 5 | facts dict for one ride (pure; reuses `facts.ride_fact`, ftp, climbs, limiters, readiness) |
| `reports/charts.py` | 5 | matplotlib -> PNG, CJK font, light theme |
| `reports/pdf.py` + `templates/ride.html.j2` | 5 | HTML -> PDF |
| `jobs/notify.py` | 7 | add `send_email()` (SMTP over TLS); stubbed in tests |
| `jobs/watch.py` | 7 | one tick for one profile; `watch --all` fan-out |
| `jobs/launchd.py` | 7 | second agent `com.cy-performance.watch` |
| `services/ride_report.py` | 7 | build / resend one report (CLI and API call this) |
| `cli/` | 8 | `cyp watch [--all]`, `cyp ride-report <activity_id> [--send]`, `cyp schedule install --watch` |

### Settings (per profile, `profiles/<name>/.env`, never in git)
`CYP_SMTP_HOST`, `CYP_SMTP_PORT`, `CYP_SMTP_USER`, `CYP_SMTP_PASSWORD` (Gmail App Password),
`CYP_REPORT_EMAIL_TO`. The athlete types these in; the agent never does.

### Flags (`core/features.py`, `docs/09-features.md`)
| id | default | requires | what |
|----|---------|----------|------|
| `report.ride` | off | `analysis.rides` | Build the per-ride PDF for new rides (`cyp watch`). |
| `notify.email` | off | `report.ride` | Email the PDF to `CYP_REPORT_EMAIL_TO`. |

### Dependencies
`matplotlib` (charts), `weasyprint` (HTML -> PDF; needs `brew install pango` on the Mac).

## Safety
- No calendar or Strava write; tomorrow's plan is computed with `write=False`.
- Email is the only new outbound call: one recipient from the profile's own `.env`, no address
  from any other source. Tests stub SMTP (the socket guard in `tests/conftest.py` already blocks
  non-loopback).
- PDFs contain personal data: written under the profile's data dir, git-ignored;
  `tests/test_privacy.py` extended to cover `*.pdf`.
- A failed email leaves `status=failed` and is retried next tick (max 3), then a macOS notice.

## Acceptance
- `uv run cyp --profile roger ride-report <a real recent activity id>` writes a PDF; I open it
  and check every section against the icu activity page by hand (numbers match the facts dict).
- `uv run cyp --profile roger watch` with flags on: a ride without a `ride_reports` row gets
  exactly one email; a second tick sends nothing.
- Tests: dedupe; lock held -> skip + `job_runs` row; streams missing -> retry then give up at 6 h;
  SMTP failure -> retry count; flag off -> nothing built; facts golden test on
  `tests/fixtures`; PDF renders zh-TW text (no tofu) in a tmp dir.
- `uv run poe check` green; `npx likec4 validate docs`.

## Open questions (decide before code)
1. ~~PDF engine~~ **Decided 2026-10-16: WeasyPrint** (over ReportLab and headless Chromium).
2. ~~Send timing~~ **Decided 2026-10-16: wait for the athlete's rating, at most 2 h.** A ride is
   sent on the first tick after its RPE/feel exists (intervals.icu field or the web 騎完感受);
   a save in the web UI triggers that profile's tick at once. After 2 h it is sent unrated and
   section 6 links to the rating page.
3. **Dad's profile**: off until asked (flags default off).

## Contract
`src/cyp/reports/ride_model.py` (`RideReport` and its sections) plus the stub signatures
`reports.ride_facts.build_ride_report`, `reports.ride_pdf.render_ride_pdf` and
`services.ride_preview.tomorrow_preview`. Builder, renderer and watcher meet only there.

## Docs to update
`docs/01-architecture.md` §5 + layer table, `tests/test_layering.py` (new modules only, no new
package), `docs/architecture.c4` (watch agent, SMTP), `docs/09-features.md`, ADR-0010
(poll vs webhook, email egress), `README.md` + `CLAUDE.md` (commands), runbook
`docs/runbooks/watch.md` (no email, duplicate email, stuck retry).
