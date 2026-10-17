# Personalized planning: goal-aware menus, limiter bias that bites, athlete preferences
Status: done 2026-10-07, revised after review 2026-10-07 (accepted by the owner: fully automatic; an intensity preference for every profile; preferred hard days; a wider library of well-known sessions)

## Problem
Two athletes with different goals, limiters and preferences get nearly identical weeks. On
2026-10-07, an FTP-target athlete without a limiter and an older athlete with a distance goal
and a durability limiter both got: Tuesday sweet spot 3×N, Saturday "long ride + late tempo",
and Z2 fill. Causes, all in `planning/planner.py`:
1. `HIT_MENU` / `LONG_MENU` depend on the phase only. In base, the HIT menu has one option
   (`ss_3x_n`), so the limiter bias (`planner_bias`, e.g. endurance 1.2 / tempo 1.2 /
   sweetspot 1.1 for a durability limiter) has nothing to choose between.
2. The bias only reorders alternatives; it never changes which *kind* of session a slot gets.
3. HIT days come from a fixed `HIT_DAY_ORDER` (Tuesday first) for everyone.
4. The goal kind (`ftp_target` vs `distance`) and the onboarding answer "how do you like to
   ride" (question 6) do not reach template choice.
5. The library is small (31 templates), so the same names recur.

## Goal
- Different goals and limiters give visibly different, physiologically sensible weeks.
- Load is nearly unchanged: same weekly TSS targets, same guardrails, same safety.
- Every choice explains itself in zh-TW ("依你的目標（長距離耐力）與強度偏好（適中）選課").

## Non-goals
- No change to season skeletons, TSS targets, ramp caps or guardrails.
- No ML / LLM choice: still deterministic rules (ADR-0004).
- No new sports.

## Design
1. **Emphasis per athlete** (`planning/menus.py` `emphasis()`, pure). Derived from the goals:
   - `ftp`: no distance goal (or only past ones). New menus in the same sweet spot → threshold
     → VO2 progression as v1, with alternatives in every stage.
   - `endurance`: a `distance` goal whose date has not passed. Base: tempo and sweet-spot work on climbs, cadence Z2.
     Build: sweet spot + tempo-on-climb + over-unders. Threshold: threshold 2×15–2×20, at most
     one VO2 every other week. Long ride: steady Z2 / distance, a finish ride every other
     week (finish rides are tried first in those weeks; rotation only within the group).
   - (later) `climb`, `general` / maintain: the same mechanism, more menus.
2. **Goal-aware menus** (`planning/menus.py`). `HIT_MENU` and `LONG_MENU` become
   `MENUS[emphasis][phase]`, and **every stage gets ≥ 2 alternatives** where physiology allows,
   so the limiter bias has something to choose from. The bias is also applied to the long-ride
   pick (not to the Z2 fill: that stays the v1 closest-TSS choice).
3. **Preferences** in `athlete.yaml` `planner:` (optional, defaults = today's behaviour; both
   are part of `plan.goal_menus` and ignored while it is off):
   - `hit_days: [tue, thu]`: preferred weekdays for hard sessions;
   - `intensity: easy | moderate | hard` (question 6). Every profile gets one. `easy` shifts the alternatives towards tempo and
     sweet spot, `hard` towards threshold and VO2, within the same number of hard sessions;
     `hit_per_week` still caps that number.
   - Masters (age ≥ 60 on the planning day, the same date the age rules use; needs
     `plan.athlete_rules` and `plan.goal_menus`, docs/05 §2.5): when two hard days are only 48 h apart (Tue + Thu), the
     second one gets the lighter alternative (tempo / climb tempo). Research suggests a
     hard-easy-easy rhythm for riders in their 60s; this keeps the athlete's chosen days and
     respects that rhythm in load.
4. **Variety**: when a stage has several equally ranked alternatives, rotate deterministically
   by week index, so consecutive weeks are not copies. The same inputs still give the same plan.
5. **Library: well-known, research-backed sessions** (YAML with the existing renderer, HR
   fallback and zh-TW explanation that names the source). Only the structures are used; nothing
   is copied from commercial apps.
   - VO2: `vo2_30_15_ronnestad` = 3 × (13 × 30 s / 15 s), 3′ between sets (Rønnestad 2015:
     more time > 90 % VO2max, larger gains than 4 × 5′ at a lower RPE);
     `microbursts_15_15` = 3 × 10 × 15 s / 15 s.
   - Threshold: `threshold_4x8_seiler` = 4 × 8′ @ ~102–106 % FTP / 2′ (Seiler 2013: the best
     return of 4×4 / 4×8 / 4×16).
   - Sweet spot / tempo: `tempo_3x15`, `tempo_2x25`, `tempo_with_surges` (10 s surges every
     2′), `ss_pyramid` (5-10-15-10-5′), `over_unders_light` (95/105 %).
   - Strength-endurance: `low_cadence_sfr` = 4 × 6′ @ 85–90 % at 55–65 rpm on a climb (Italian
     SFR).
   - Climbs (sized to `location.climbs`): `climb_tempo_repeats`, `climb_ss_repeats`.
   - Endurance: `z2_hilly_120` (a Z2 fill option with goal menus; not a long-ride option,
     so the long ride never shrinks to two hours), `z2_hilly_180`, `endurance_fast_finish_150`.
   - Distance progression (`plan.long_ride_progression`): the long ride lasts exactly the
     week's progression minutes: a menu ride of that length if its normal progression gives
     one, else `long_ride_distance` sized to it (Z2). The over-distance ride is never rotated
     away, and its note names the minutes actually planned.
6. **Explanations**: each day's `because` names the emphasis, the limiter and the preference
   that picked the template.
8. **Automatic feedback loop: post-ride RPE / feel** (fully automatic, no new step for the
   athlete beyond answering in intervals.icu):
   - intervals.icu's per-activity `icu_rpe` (1–10) and `feel` (1 strong … 5 weak) are already
     synced. They become a readiness input `ride_feel`:
     - RPE far above what the session intended (e.g. RPE ≥ 7 on a Z2 or recovery ride), or
       feel 4–5, lowers readiness;
     - an easy RPE on a hard session raises it.
   - The daily wellness answers (fatigue, soreness, stress, mood) already feed readiness.
   - The 05:30 run already re-plans from readiness (REST / EASY / AS_PLANNED / UPGRADE). With
     the new input, "I felt terrible yesterday" turns today's intervals into Z2 without anyone
     touching the plan.
   - The web 紀錄 page gets a one-tap "騎完感受" (RPE + feel) per ride. It is stored locally
     and used at once; writing it back to intervals.icu is a later, flag-gated option.
   - Per field, the web answer wins over intervals.icu's (the page and readiness agree). RPE
     and feel come from one ride (the day's longest rated ride) and are judged against that
     ride's class, plus +1 expected RPE per hour beyond 3 h (≤ +2): a five-hour Z2 ride may
     feel like a 5. Verdicts with these inputs carry `algo_version` `readiness_v1.1`.
7. **Flags** (ADR-0008): `plan.goal_menus` (default on), `plan.variety` (default on),
   `readiness.ride_feel` (default on). With these and the age / progression flags
   off, plans are byte-identical to the v1 planner (golden snapshot,
   `tests/planning/test_golden.py`), even with `hit_days` / `intensity` / `birth_year` written.

Layers: `planning` only (+ `settings` for the new optional fields). Docs: docs/05 §2.3 and §6,
docs/09, the template README.

## Safety
Only the template choice and the weekday of hard sessions change. Weekly and daily TSS targets,
guardrails, recovery weeks and the write path are untouched. A changed choice re-publishes
through the normal diff (our events only).

## Sources
- Rønnestad et al. 2015, short (30/15) vs long intervals in trained cyclists;
  summaries: biketips.com/ronnestad-intervals-for-cyclists, knowledgeiswatt.substack.com/p/32.
- Seiler et al. 2013, 4×4 / 4×8 / 4×16 effort-matched intervals (Scand J Med Sci Sports 23(1)).
- Masters cyclists: fewer hard sessions rather than lower intensity, hard-easy-easy, slower
  perceived recovery after 48 h (Borges et al., Waikato/Griffith; trainright.com).
- Low cadence / big gear: Fast Talk Labs (Neal Henderson); Billat 30-30, flying 40s, tempo
  (coaching literature).

## Acceptance
- With the flags off: the season plan equals the v1 golden snapshot.
- Simulated seasons (tests, invented athletes):
  - distance + durability: climb or tempo sessions appear; steady long rides every other week;
  - ftp: the sweet spot → threshold progression keeps its kind;
  - at least half of the hard and long sessions differ in template between the two;
  - an older distance athlete's long ride grows by ≤ 60 min between consecutive loading
    weeks and reaches `long_ride_max_minutes`.
- `hit_days: [thu]` puts the single base-week HIT on Thursday.
- The full-season simulation is guardrail-clean for both emphases.
- The weekly TSS of every simulated loading week stays within ±10 % of the flags-off plan for
  the same inputs (athlete < 60). ±5 % is not reachable: the endurance menus put more of the
  week into tempo and Z2, and the residual Z2 fill is capped by each day's minutes. For
  athletes ≥ 60 the lighter second hard day lowers the week further on purpose.
- Every new template passes the library tests (render, HR fallback, zh-TW text).
- A ride with RPE 8 on a planned Z2 day lowers the next day's readiness and turns a planned
  hard session into Z2 (test). The UI "騎完感受" round-trips.

## Docs to update
docs/05 §2.3, §6; docs/09; `planning/library/README.md`; docs/onboarding-questions.md (Q6 now
maps to `planner.intensity`); `CLAUDE.md` map.
