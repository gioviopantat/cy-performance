---
name: research-review
description: Monthly literature review of the evidence grades behind cy-performance's glossary claims; writes a zh-TW report docs/research/YYYY-MM.md on a branch research/YYYY-MM for Roger to approve. Use when the scheduled research routine fires, or the user says "run the research review", "文獻月報", "重新審查證據等級".
---

# Research review (monthly)

Spec: `docs/specs/research-review.md`. Runs unattended (cloud routine, 1st of each month) or by
hand. It **only** writes one report; a human approves every change afterwards.

## Hard limits
- Edit nothing but `docs/research/<YYYY-MM>.md`. Never touch code, thresholds, the glossary,
  `core/evidence.py`, specs or ADRs; put suggested edits in the report.
- Work on a branch `research/YYYY-MM` cut from the repo's default branch; one commit, push the
  branch. **Never open a PR**, never merge, no Co-Authored-By line.
- Never run `cyp` write commands (no calendar, Strava or email). No personal data: read only
  `docs/` and `src/`; `profiles/` is not in the repo and must not be needed.

## 1. Inputs
- `core/evidence.py` `REGISTRY`: claim id, glossary id, grade, citations, `reviewed`
  date. If the file does not exist yet, use the "Proposed grades" table in the spec.
- `docs/glossary/<id>.md` for each claim (its 限制 and 證據等級 sections).
- The previous report `docs/research/<last month>.md`: carry its 未驗證 items forward.
- Code thresholds the claims back (grep `src/cyp/analysis`, `src/cyp/planning`,
  `src/cyp/reports/facts.py` for module constants, e.g. readiness `WEIGHTS`, durability
  `Thresholds`, `KJ_BUCKETS`, `RECOVERY_FACTOR`, guardrail caps). A threshold no claim covers
  is a candidate new claim.

## 2. Search (per claim)
- Window: published after the claim's `reviewed` date (first run: the last 3 years).
- Queries: the claim's key terms + `systematic review`, `meta-analysis`, `randomized`,
  `cyclists` / `endurance athletes`; also "cited by" of the claim's key citations.
- Sources, in order: PubMed, PMC, journal sites (Sports Med, MSSE, IJSPP, EJAP, JSAMS,
  SJMSS, J Appl Physiol, Sci Rep). Use web search in extended mode for recent or niche items,
  then open each paper's page.
- Many claims: split them across parallel subagents by glossary group (CP/FTP · load/PMC/ACWR
  · durability/HR · TID/periodization/intervals · readiness/HRV/recovery); each returns a
  table, then you check every row yourself.

## 3. Evidence rules
- A citation counts only if its DOI, PubMed, PMC or publisher page was **opened** and showed
  the authors, year and journal. Otherwise list it as 未驗證 with the reason.
- PubMed web pages block automated fetches (cookie wall). Fetch the same PubMed record by
  PMID instead: `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=EXT_ID:<PMID>&format=json&resultType=core`
  or NCBI E-utilities `efetch.fcgi?db=pubmed&id=<PMID>&rettype=abstract`. Label those rows
  "摘要（API）" and PMC pages "全文", and say so in the report: whether API records count
  is Roger's call (open question in `docs/research/2026-10.md`).
- Before writing, re-check a sample of rows yourself (at least every grade-changing citation):
  PMID, authors, year, journal and DOI must match the record.
- Strength: systematic review / meta-analysis > RCT > cohort > cross-sectional. Team-sport or
  non-endurance data count only as indirect.
- Blogs, podcasts, forums, vendor docs and preprints are never evidence (they may lead you to
  a paper). Say "abstract only" when the full text was not read.
- Summarise in your own words; quote at most a short phrase.
- Classify each new paper: 支持 / 反駁 / 修正 (supports / contradicts / refines).

## 4. Report: `docs/research/<YYYY-MM>.md` (zh-TW, Roger reads it)
1. 摘要: grades to change, the most important corrections, what remains 未驗證.
2. A table of every claim: id · current grade · proposed grade · one-line reason.
3. Per claim: 目前等級 → 建議等級 and why; 關鍵文獻 (authors, year, journal, DOI/PMID link
   that was opened); 新證據 since the last review with its label; 建議的 glossary 措辭.
4. 建議新增的主張: thresholds the code relies on that no claim covers.
5. 需更正: glossary or spec sentences the evidence no longer supports, quoted with file path.
6. 未驗證清單.
7. A note that grade changes need Roger's approval, then a normal commit updating
   `core/evidence.py` + the glossary together; anything that changes a threshold or planner
   rule needs a new ADR (ADR-0004).

Then `git checkout -b research/YYYY-MM`, add the report, commit
`docs(research): YYYY-MM literature review`, push the branch, and stop.
