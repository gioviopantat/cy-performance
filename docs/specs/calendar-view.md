# Calendar view (課表 + 紀錄 in one page)
Status: done · 2026-10-10

## Goal
One 行事曆 page replaces the 課表 and 紀錄 tabs. A month/week grid where each day cell answers,
at a glance, "what was planned, what happened, how did it go"; a click opens a modal with the
detail (today's workout profile and explanation, or the ride analysis, RIDE.LOG and feedback).
Inspired by the intervals.icu calendar, keeping our look and fixing what is not intuitive there.

## Non-goals
- No drag-and-drop to move workouts (a calendar write; moves stay with the planner or
  `availability.dates`).
- No editing of planned workouts from the cell.
- No new analysis: every number already exists in the API.

## Design
- **Cell (shown outside, filtered)**:
  - day number; today ringed;
  - one status stripe colour, the main signal:
    - done as planned: green;
    - over 130 % of plan: orange;
    - under 50 % / partial: yellow;
    - skipped (planned, nothing ridden, day over): red outline;
    - rest day: muted;
    - future planned: neutral, dashed;
  - one line: workout type icon + short name (`Z2 120′`, `甜蜜點 3×12`);
  - one number: TSS done / planned (`102/88`), or planned TSS for the future;
  - a readiness dot when we have one (go / easy / stop colours).
- **Not in the cell (modal only)**: steps profile, explanation, zones, decoupling, power curve,
  RIDE.LOG, feedback, Strava write.
- **Week row summary** (right column on desktop, header on phone): TSS done/target, hours,
  CTL at the end of the week, a small bar of done vs target.
- **Intuitive fixes vs icu**:
  - planned vs done never share a style (outline vs filled);
  - the legend is always visible;
  - "today" is always in view on open;
  - one tap = modal; ← 較早 / 今天 / 較晚 → buttons (and a swipe on a phone) move two weeks;
  - the modal has prev/next day buttons (and ← → keys), Esc / the back gesture closes it.
- **Phone**: the same weeks in 7 narrow columns (stripe + icon + done or planned TSS; the name
  and "/planned" are dropped), the week summary as a row above each week; swipe left / right
  moves two weeks; a tap opens a bottom sheet instead of a centred modal.
- **API**: `GET /v1/calendar?start=&end=` (whole weeks, ≤ 63 days) returns per day: the calendar's
  workout (else our proposal), the rides (id, name, minutes, TSS, status), readiness score /
  recommendation, CTL/TSB, the athlete's own events (sick, race), the computed compliance status, plus per-week summaries.
  Built in `services/calendar.py` (L7) from `Dataset`; read-only.
- **Web**: `views/Calendar.tsx` replaces the `Plan.tsx` and `Rides.tsx` tabs; the ride detail
  (`views/RideDetail.tsx`: numbers, feeling, zones, climbs, RIDE.LOG editor) is reused inside
  the modal, rendered through a portal. The season overview stays as a panel under the grid.
- No feature flag (presentation, no server behaviour change).

## Safety
Read-only endpoint. The modal reuses the existing RIDE.LOG / Strava write UI with its
confirmation (ADR-0009); nothing new writes.

## Acceptance
- API test: compliance status for done / over / partial / skipped / rest / future days; range
  limit; profile header routing.
- Playwright screenshots at 1280 and 390 px, default and pixel skins: today visible, legend
  visible, modal opens and closes with Esc.
- `uv run poe check` green.

## Docs to update
docs/specs/web-ui.md (screens), docs/08-api.md, docs/architecture.c4 (web description),
docs/01 layer table (`services/calendar.py`), README screenshot notes.
