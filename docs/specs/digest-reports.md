# digest-reports
Status: draft · 2026-10-17

## Goal
Every athlete (Roger and his dad, same level of detail) gets by email:
- a **weekly** zh-TW PDF every Monday at 07:00 (Asia/Taipei) for the ISO week that just ended,
- a **monthly** zh-TW PDF on the 1st at 07:00 for the calendar month that just ended,

built like the per-ride report ([ride-report.md](ride-report.md)): our plan + our longitudinal
models + the athlete's feedback, each number with its evidence grade, nothing that
intervals.icu / Strava / Garmin already draw unless it carries our verdict. 07:00 is after the
05:30 autopilot, so the next week's plan is already in the calendar and can be shown.

## Non-goals
- No calendar or Strava writes; next week's plan is read from what the autopilot stored.
- No new analysis models; digests aggregate what exists (ride intents, TID by sessions,
  readiness v2, goal projection, durability, limiters, climbs, evidence).
- The Markdown weekly report (`report.weekly`) stays; the PDF is a separate artifact.

## Content
### Weekly (`digest_weekly_v1`)
| # | Section | From |
|---|---------|------|
| 1 | 一句話結論 (the week's verdict: done / partly / missed vs plan, and the one thing to change) | sections below, deterministic |
| 2 | 計畫 vs 實際: a 7-day grid, each planned session with its ride's intent verdict (達成 / 部分 / 未達成 / 騎過頭 / 沒騎), outdoor interruptions excused | `ride_reports` facts / `ride_intent` per ride |
| 3 | 負荷: planned vs actual TSS per day; CTL / ATL / TSB start → end of week; ramp vs cap | `fitness_daily`, plan |
| 4 | 強度分布 by sessions vs the phase target (time-in-zone as reference) | `tid_v2` (ADR-0014) |
| 5 | 離目標還有多遠: FTP evidence of the week, goal projection now vs a week ago | `goal_projection` |
| 6 | 恢復: 7-day HRV vs baseline band, resting HR, sleep, ride feel vs expected, readiness verdicts of the week | `readiness_v2` |
| 7 | 本週亮點: best climbs vs history (same fatigue / heat), durability (late-ride power) vs the 4-week median | climbs, durability |
| 8 | 下週課表: the stored plan for the coming week with each session's purpose | `planned_workouts` |
| 9 | 方法與證據 | `core/evidence.py` |

### Monthly (`digest_monthly_v1`)
| # | Section | From |
|---|---------|------|
| 1 | 一句話結論 for the month | deterministic |
| 2 | 一致性: sessions planned / done / achieved, longest gap, by week | ride intents, plan |
| 3 | 體能曲線: CTL over the month vs the season plan's CTL path | `fitness_daily`, `season.py` |
| 4 | 功率–時間曲線: this month's best 5 s … 60 min vs last month and the season best | power curves |
| 5 | FTP / CP / W′: estimates over the month, which efforts back them, goal projection trend | ftp, cp fits, goal |
| 6 | 強度分布 by sessions per week vs phase targets | `tid_v2` |
| 7 | 限制因子: limiter ranking now vs a month ago, and what the plan does about it | limiters |
| 8 | 恢復與健康: HRV / RHR / sleep monthly trend, rest days, readiness REST/EASY counts | readiness |
| 9 | 下個月: phase and block of the season, key sessions, tests | season, plan |
| 10 | 文獻月報摘要 if a `docs/research/<YYYY-MM>.md` landed this month (grade changes approved) | research review |
| 11 | 方法與證據 | evidence |

### Empty periods
A week (or month) without rides still gets a short digest: what was planned, that it was a
break (and why when known, e.g. availability `0` dates such as dad's trip 10/15–10/26),
fitness drift, and the plan for coming back. Sections without data are omitted.

## Design
| Module | Layer | Role |
|--------|-------|------|
| `reports/digest_model.py` | 5 | contract: `WeeklyDigest`, `MonthlyDigest` (pydantic, like `ride_model.py`) |
| `reports/digest_facts.py` (+ section modules) | 5 | fill the contract from stored results; reuse `facts.weekly_facts` and the ride-report builders |
| `reports/digest_pdf.py`, `charts.py`, `templates/digest_*.html.j2` | 5 | render (WeasyPrint, matplotlib; same look as the ride PDF) |
| `store/models.py` + migration | 2 | `digest_reports(kind, period_start, status, attempts, pdf_path, sent_at, error)`, unique per kind + period |
| `services/digest.py` | 7 | build / send one digest |
| `jobs/digest.py`, `jobs/launchd.py` | 7 | `cyp digest --all`: per profile, build and send every due, unsent digest (so a Mac asleep at 07:00 catches up on wake); third launch agent `com.cy-performance.digest` at 07:00 daily (does nothing on days with nothing due) |
| `cli/` | 8 | `cyp digest weekly|monthly [--period YYYY-MM-DD|YYYY-MM] [--send]`, `cyp schedule install --digest` |

Flags: `report.digest_weekly` (default off, requires `analysis.rides`),
`report.digest_monthly` (default off, requires `analysis.rides`); emailing reuses
`notify.email` and the profile's SMTP settings.

## Safety
Same as the ride report: no calendar or Strava write; email is the only outbound write, to the
profile's own `CYP_REPORT_EMAIL_TO`; PDFs under the profile's data dir (git-ignored); one row
per kind + period, so a digest is sent once; tests stub SMTP.

## Acceptance
- Fixture athlete: a full week, a week with stops/heat outdoor rides, an empty (trip) week, a
  month across a recovery week; golden JSON per kind; PDFs render zh-TW without tofu.
- `cyp --profile roger digest weekly --period 2026-10-05` and `monthly --period 2026-09` build
  PDFs; every number checked by hand against the facts JSON and intervals.icu.
- The launch agent sends nothing twice; a missed Monday is sent on wake.
- `uv run poe check` green; architecture docs, LikeC4, flags doc, README, CLAUDE.md, runbook.

## Open questions
1. Backfill on launch: send the weekly for 10/5–10/11 and the monthly for September to both
   athletes once it works? (Recommended: yes, as the first real check.)
2. Dad's trip week digests (10/19, 10/26): send the short "break" version (recommended) or
   skip while he is away?
