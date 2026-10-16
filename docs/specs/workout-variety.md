# workout-variety
Status: draft · 2026-10-16

## Goal
Weeks that do not feel the same, without giving up the planned dose. Today variety is thin:
- Hard days rotate only among **equally scored** alternatives, by week index
  (`planning/planner.py:_rank`, flag `plan.variety`). The pick does not know what was ridden
  last week or whether the athlete hated it.
- Easy days take the endurance template **closest to the target TSS**
  (`planner.fill_endurance`), so the same Z2 ride repeats whenever availability repeats.
- Outdoor days are indoor structures plus a climb name (`plan.climb_routes`). On a road with
  lights and rollers, that structure is hard to ride as written; the per-ride report then
  judges the stimulus (`ride_intent`), but the plan does not design for the road.

The athlete gets: a memory of what was ridden, a say in what comes back, easy days that differ,
and outdoor sessions built from terrain. The planner stays deterministic (ADR-0004): the same
history, preferences and calendar give the same plan.

## Non-goals
- No change to load targets, progression, phases or guardrails. Variety may swap a session
  only for one of the same **dose** (below).
- No LLM picking workouts. No random numbers.
- No new physiology claims: variety is about adherence and enjoyment. Evidence that varying
  the stimulus itself improves adaptation is weak, so this spec does not claim it (it enters
  `core/evidence.py` as a grade-C claim `workout_library.variety`, status proposed).

## Design

### 1. Dose families (data, no new behaviour)
Each template gets a `family` and its dose is computed by the existing resolver: minutes in
zone by intent and TSS. Two templates are **interchangeable** for a slot when they share the
family and their resolved dose at the slot's progression is within ±10 % on both minutes in
the target zone and TSS. Families: `endurance_steady`, `endurance_skills` (cadence, low-cadence
force), `endurance_terrain` (hilly), `tempo`, `sweetspot`, `threshold`, `vo2_long`,
`vo2_short`, `anaerobic`, `long_finish`, `test`, `recovery`. A test checks that every template
has one, and every menu stage's alternatives share a family.

### 2. Recency (new flag `plan.variety_recency`, default on, requires `plan.variety`)
For each candidate, count how often the athlete **rode** it (paired actual, else planned and
not skipped) in the last 21 days. Score multiplier `0.85 ** n` (1 repeat -> 0.85, 2 -> 0.72).
Applied only among interchangeable candidates, so it never trades dose for novelty. Ties keep
today's week-index rotation. Key sessions are exempt: FTP tests, the block's over-distance
ride, and anything the guardrails or adaptation inserted.

### 3. Athlete preferences (new flag `plan.preferences`, default on)
- Input: in the web 騎完感受 dialog, a three-way choice per ride's template: 喜歡 / 普通 /
  不想再騎 (stored per profile, `data/template_prefs.json`, last answer wins, dated).
- Effect: 喜歡 ×1.10, 不想再騎 ×0.60 on the candidate's score, again only among
  interchangeable candidates. A disliked template can still appear when it is the only one
  that fits the dose and the day; the explanation then says so.
- Shown back: the weekly report lists the week's picks with why (recency, preference,
  limiter bias), and section 8 of the per-ride PDF says when tomorrow's pick was changed by a
  preference.

### 4. Easy-day rotation
`fill_endurance` keeps "closest TSS" as the first key but rotates among endurance templates
whose TSS is within ±10 % of the closest one, across `endurance_steady`, `endurance_skills`
and `endurance_terrain`, using the same recency and preference multipliers. Recovery days stay
recovery spins.

### 5. Outdoor terrain versions
For outdoor sweet-spot, tempo, threshold and VO2-long slots, offer a terrain version:
- **Climb**: reps defined as "one ascent of <climb> at X–Y % FTP", rep count from the
  climb's typical ascent time (`location.climbs` + the athlete's climb history), descents as
  recoveries. These exist for some intents (`climb_ss_repeats`, `climb_tempo_repeats`,
  `climb_repeats_goal_power`); fill the gaps (threshold, VO2-long).
- **Flat road**: "accumulate N minutes at X–Y % FTP in blocks of ≥ M minutes", no fixed
  rep count, so lights and turns do not break the session. The per-ride intent judge
  already scores accumulated minutes with excused stops.
Indoor days keep the structured version. Which one is used follows today's venue rule.

### Modules (layer table)
| Module | Layer | Change |
|--------|-------|--------|
| `planning/library/*.yaml`, `planning/templates.py` | 5 | `family` field; terrain templates |
| `planning/variety.py` (new) | 5 | interchangeability, recency and preference multipliers (pure) |
| `planning/planner.py` | 5 | `_rank` and `fill_endurance` call `variety.py` |
| `dataset.py` | 3 | recent rides per template for the planner context |
| `services/ride_feedback.py`, `api/`, `web/` | 7–8 | the 喜歡 / 普通 / 不想再騎 answer |
| `reports/` | 5 | weekly picks-and-why; PDF section 8 note |

### Flags
| id | default | requires | what |
|----|---------|----------|------|
| `plan.variety_recency` | on | `plan.variety` | Down-weight interchangeable templates ridden in the last 21 days. |
| `plan.preferences` | on | `plan.horizon` | The athlete's like / dislike per template weights interchangeable picks. |

With both off, the plan equals today's (`tests/planning/test_golden.py`).

## Safety
No new writes beyond the existing autopilot publish path (same write guard). Preferences are
local files under the profile's data dir. Dose and guardrails are unchanged by construction.

## Acceptance
- 26-week simulation of the reference athlete: no slot repeats the same template in two
  consecutive weeks when an interchangeable alternative exists; every swap stays within ±10 %
  of dose; weekly TSS and phase targets are identical to the flags-off run.
- A disliked template stops appearing when an alternative exists, and still appears (with the
  explanation) when none does.
- Easy days: over 4 weeks of identical availability, at least 3 different endurance templates.
- Replay on the roger profile (read-only, last 8 weeks): list how many picks change and why.
- `uv run poe check` green; flags off -> golden plan unchanged.

## Open questions (decide before code)
1. **Preference input**: the web 騎完感受 dialog only (recommended), or also a reply link in
   the PDF email?
2. **Dislike strength**: ×0.60 (strong; recommended) or ×0.80 (gentle)?
3. **New templates**: only the terrain versions (recommended), or also 6–8 new indoor
   structures (e.g. progressive threshold, criss-cross, tempo with sprints)?
4. **Flat-road "accumulate N minutes" sessions** on outdoor days: yes (recommended) or keep
   fixed reps everywhere?

## Docs to update
`docs/05-training-engine.md` (selection), `docs/glossary/workout_library.md` (families,
variety, the C-grade claim), `docs/09-features.md` (two flags), `docs/01-architecture.md`
(`planning/variety.py`), `docs/specs/web-ui.md` (the preference answer), ADR-0015 (variety
within dose families, deterministic).
