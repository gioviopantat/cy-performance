# research-review
Status: accepted · 2026-10-16

## Goal
Every number the athlete reads says how much science stands behind it, and that judgement is
kept current:
1. **Evidence grades.** Each claim we use from `docs/glossary/` gets a grade (A/B/C) with key
   citations. A verdict sentence in a report may rest only on A/B claims; C-grade numbers are
   still shown, labelled 參考（證據弱）.
2. **Monthly review.** Once a month, per claim, new peer-reviewed literature is searched and a
   zh-TW report proposes grade changes and corrections. A human approves every change.

## Non-goals
- The review never edits code, thresholds or plans. A behaviour change goes through an ADR
  (ADR-0004: the planner is deterministic; an LLM narrates, never computes).
- No automatic re-fitting of model parameters (τ, weights) from literature.
- Not a medical review; readiness stays "not medical advice" (`readiness_v1.md` 限制).

## Grades
Graded per **claim we rely on**, not per model: CP is solid, while W′bal per-rep prediction is
not.

| Grade | Meaning | Allowed use |
|-------|---------|-------------|
| **A** | Meta-analyses / several controlled studies agree, or strong physiological consensus, in endurance athletes | May drive a verdict alone |
| **B** | Supported at group level (one meta-analysis with heterogeneity, or several small trials); individual error is material | May drive a verdict; show the uncertainty |
| **C** | Practitioner heuristic, proprietary formula, single small study, team-sport data only, or contested | Shown as context (參考), never the sole basis of a verdict or warning |

### Proposed grades (initial review, 2026-10-16)
"verified" = the citation was found and its abstract read; "full text unread" = only the abstract or
a secondary summary was seen, so check before relying on exact numbers.

| Glossary id | Claim we use | Grade | Key evidence |
|-------------|--------------|-------|--------------|
| `cp_wprime` | CP marks the heavy/severe boundary; P(t) = CP + W′/t over 2–15 min | **A** | Jones et al. 2010 MSSE review (already cited); Monod & Scherrer 1965 |
| `cp_wprime` | W′bal (Skiba 2012 single-exponential) predicts which rep fails | **C** | Chorley et al. (EJAP; year to confirm, 2021 or 2022): bi-exponential reconstitution fits better, large individual variation, slower after repeated severe efforts (verified, [PMC8854279](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8854279/)); Skiba & Clarke 2021 on model inconsistency (cited secondhand in [arXiv 2108.04510](https://arxiv.org/pdf/2108.04510), unverified) |
| `coggan_np_if_tss` | TSS is a usable dose for fitness change | **B** | Sanders et al. 2017 IJSPP 12(5):668–675, n=15: TSS r≈0.75–0.79 with submaximal fitness; individualised TRIMP equal or stronger (verified) |
| `coggan_np_if_tss` | NP's 4th-power weighting reflects metabolic cost | **C** | Empirical, no peer-reviewed validation found; glossary 限制 already says so |
| `banister_pmc` | CTL/ATL describe load history | **B** | Banister 1975; Sanders 2017 |
| `banister_pmc` | TSB predicts individual performance on a given day | **C** | Marchal et al. 2025 Sci Rep: fatigue parameters poorly identifiable, adding them did not improve prediction (abstract via [HAL](https://hal.inrae.fr/hal-04999009v1), full text unread) |
| `ramp_rate_acwr_monotony` | ACWR > 1.3 means higher injury risk | **C** | Impellizzeri et al. 2020 IJSPP (conceptual flaws, already cited); Wik et al. 2020 Sports Med: association, wide method variation; a 2025 meta-analysis ([PubMed 41029871](https://pubmed.ncbi.nlm.nih.gov/41029871/)) finds association but urges caution; all team-sport injury data, none on cycling overtraining |
| `ramp_rate_acwr_monotony` | Monotony / strain (Foster 1998) flag overtraining | **C** | Single original study; no cycling validation found |
| `ramp_rate_acwr_monotony` | CTL ramp cap per week | **C** | Practitioner heuristic |
| `eftp` | FTP ≈ MLSS (the threshold our zones hang on) | **B** | Borszcz et al. 2019 IJSPP: bias 1.4 % ± 9.2 % (limits of agreement), worse in less-trained riders (verified); Lillo-Beviá et al. 2022 JSCR: 20-min × 0.95 still ~12 W off MLSS (abstract) |
| `eftp` | intervals.icu eFTP value | **C** | Proprietary; no published validation |
| `decoupling_hr_lag` | Durability (power/HR decay with accumulated work) matters for performance | **B** | Maunder et al. 2021 Sports Med (cited); a 2025 systematic review (21 studies, [EJAP s00421-025-05885-0](https://link.springer.com/article/10.1007/s00421-025-05885-0), abstract): 10–20 % power drop after 2.5–15 kJ/kg; **kJ alone is an incomplete fatigue measure, intensity matters** |
| `decoupling_hr_lag` | Pw:Hr < 5 % = aerobically ready | **C** | Friel heuristic (blog/book) |
| `efficiency_factor` | EF trend within Z2 rides tracks aerobic base | **C** | Practitioner metric; heat/sleep confounds (glossary 限制) |
| `time_in_zone_tid` | Most volume at low intensity | **A** | Seiler 2010 IJSPP; Seiler & Kjerland 2006 |
| `time_in_zone_tid` | Polarised beats pyramidal | **C** (against) | Oliveira et al. 2024 Sports Med meta-analysis (17 studies, [PMC11329428](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11329428/), pooled numbers unread); a second 2024 meta-analysis (13 studies): no difference in VO2max or TT (secondary summary). Treat PI > 2 as a description, not a target |
| `readiness_v1` | Subjective wellness tracks training response | **B** | Saw et al. 2016 BJSM systematic review (cited) |
| `readiness_v1` | HRV-guided training beats a fixed plan | **B** (mixed) | Granero-Gallegos et al. 2020 IJERPH meta: small positive VO2max effect (verified, [PMC7663087](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7663087/)); 2021 JSAMS review (8 studies): VO2peak effect not significant (abstract) |
| `readiness_v1` | Our v1 weights and cut-offs | **C** | Ours; the glossary already calls them a starting point |
| `periodization_3_1` | Block > traditional organisation | **B** | Mølmen et al. 2019 OAJSM meta: SMD 0.40 VO2max, 0.28 Wmax, 6 studies pooled (verified) |
| `periodization_3_1` | 3:1 specifically (vs 2:1) | **C** | Convention (Friel); no comparative trial found |
| `periodization_3_1` | Recovery-week / taper cut volume 40–60 %, keep intensity | **A** | Bosquet et al. 2007 MSSE meta (cited) |
| `guardrails` | ≥ 48 h between hard sessions | **B** | Seiler et al. 2007 MSSE (small n) |
| `ftp_target_season` | 4–8 min intervals develop threshold/VO2 | **B** | Seiler et al. 2013 SJMSS; Rønnestad et al. 2020 (cited) |
| `ftp_target_season` | +20 % FTP in 26 weeks is reachable | **C** | Atypical; the glossary says trained riders usually gain 5–10 % per season |
| `workout_library` | Rønnestad 30/15 short intervals | **B** | Rønnestad et al. 2020 (elite cyclists) |
| `workout_library` | Other templates' specific structure | **C** | Practitioner design |

### Found while grading: docs and code disagree (fix in this spec's implementation)
1. `src/cyp/reports/facts.py:168` tells the athlete「受傷風險上升」when ACWR > 1.3. That claim is
   grade C, so it should be reworded as context, not removed silently.
2. `docs/05-training-engine.md:145` says the guardrails trigger on ACWR > 1.5, but
   `src/cyp/planning/` never reads ACWR (`grep -rn acwr src/cyp/planning` is empty). One of the two
   is wrong. **Decided 2026-10-16: docs/05 drops ACWR**; the code stays as is.
3. `docs/glossary/README.md` calls ramp/ACWR/monotony "守衛的主要輸入", the same drift as item 2.
4. `docs/glossary/README.md` and `workout_library.md` say "約 30 個模板"; the library has 45.

## Design

### Evidence registry
| Module | Layer | Role |
|--------|-------|------|
| `src/cyp/core/evidence.py` | 0 | `REGISTRY`: claim id → glossary id, grade, citations (DOI/PMID/URL), `reviewed` date. Same pattern as `core/features.py` |
| `docs/glossary/*.md` | — | New section `## 證據等級` per file (table: 主張 · 等級 · 依據 · 最後審查) |
| `src/cyp/core/explain.py` | 0 | `MethodRef` gets an optional `claim` id, so every `Reason` knows its grade |
| `src/cyp/reports/` | 5 | Renderers badge C-grade reasons 參考（證據弱）; ACWR/monotony warnings reworded |
| ride PDF section 9 (`docs/specs/ride-report.md`) | 5 | Table of the models used in this ride with grade + citation |

A verdict (headline or warning) whose reasons are all grade C fails a test.

### Monthly review
One procedure, kept in a skill `.claude/skills/research-review/SKILL.md`, whatever triggers it:
1. Read `core/evidence.py`; for each claim, search peer-reviewed work published after its
   `reviewed` date (PubMed / journal sites first, never blogs as evidence).
2. Classify each hit as supports / contradicts / refines. A citation counts only if its
   DOI, PMID or publisher page was opened; otherwise it is listed as 未驗證.
3. Write `docs/research/YYYY-MM.md` (zh-TW): per claim, the current grade, the proposed grade and
   why, new citations, and suggested glossary wording. Nothing else is edited.
4. Roger approves item by item. Approved items update `core/evidence.py` + the glossary in a
   normal commit; anything that would change a threshold or planner rule becomes a new ADR.

Trigger, to choose (open question 1):

| Option | How | Pros | Cons |
|--------|-----|------|------|
| **(a) Cloud routine** | Claude scheduled agent on `gioviopantat/cy-performance`, 1st of each month; pushes a branch `research/YYYY-MM` with the report | Runs when the Mac sleeps; needs no personal data (glossary is public); review is a diff | Cloud usage cost; touches the remote (a branch, no PR unless you say so) |
| (b) Local launchd + `claude -p` | Monthly launch agent runs the skill headless; report emailed via the ride-report SMTP | Same mailbox as ride reports | Needs ride-report email first; headless permission prompts; Mac must be awake |
| (c) Reminder only | Monthly email/notification: "run /research-review" | Cheapest, fully human-driven | Depends on you remembering to act |

Recommendation: **(a)**, branch only, no automatic PR (your rule: confirm before PRs).

## Safety
- No calendar, Strava or email writes from the review itself (option (b) reuses the existing
  email path).
- No personal data leaves the machine: the review reads only `docs/glossary/` and
  `core/evidence.py`.
- The review never modifies code or thresholds; `tests/test_evidence.py` checks the registry
  and glossary agree, so a hand edit to one without the other fails CI.

## Acceptance
- `tests/test_evidence.py`: every glossary file has a `## 證據等級` section matching
  `REGISTRY`; every `MethodRef.claim` used in code exists; no verdict rests only on grade-C
  reasons; ACWR warning text no longer claims injury risk.
- A daily report rendered from fixtures shows 參考（證據弱） next to ACWR and monotony.
- Running the skill once by hand produces `docs/research/2026-10.md` with every claim listed and
  every citation either linked or marked 未驗證.
- `uv run poe check` green.

## Open questions (decide before code)
1. ~~Trigger~~ **Decided 2026-10-16: (a) cloud routine, branch only.** Uses the plan's normal usage, no separate charge (code.claude.com/docs/en/routines.md).
2. ~~Drift item 2~~ **Decided 2026-10-16: drop ACWR from docs/05** (ramp rate + TSB floor cover it).
3. ~~Initial grades~~ **Accepted 2026-10-16** as a starting point; the first review (2026-10) re-checks them against literature up to October 2026.

## Docs to update
`docs/glossary/*.md` (證據等級 sections), `docs/07-explainability.md` (grades in explanations),
`docs/01-architecture.md` (new `core/evidence.py`), `docs/05-training-engine.md` (drift item 2),
`CLAUDE.md` + `README.md` (the skill), ADR-0010 or ADR-0011 for "verdicts need A/B evidence"
(numbering shared with `ride-report`).
