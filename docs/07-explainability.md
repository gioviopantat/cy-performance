# 07 · Explainability (coach voice, not just a calendar)

The athlete must be able to read **what** the system concluded, **why**, and **how it was
computed**, in plain Traditional Chinese, without opening the code. This is a first-class
layer with hooks in every other layer. Implementation is scheduled late (M5) but the seams are
built from M0 so nothing has to be retrofitted.

## 1. The `Explanation` object (core/explain.py, from M0)
Every non-trivial result (a metric, a readiness verdict, a planned workout, a plan change) can
return an `Explanation`:

```python
class Explanation(BaseModel):
    key: str  # stable id, e.g. "readiness.verdict", "plan.day.2026-10-07"
    headline_zh: str  # one sentence a non-expert understands
    because: list[Reason]  # ordered, most important first
    method: MethodRef | None  # which model/algorithm, with inputs actually used
    confidence: Literal["high", "medium", "low"]
    glossary_terms: list[str]  # ids into docs/glossary/*.md


class Reason(BaseModel):
    text_zh: str  # "昨天的心率比功率慢了 40 秒才跟上，代表自主神經還沒恢復"
    evidence: dict[str, Any]  # the numbers quoted, so the renderer never invents them
    weight: float | None  # share of the decision, when a weighted score was used


class MethodRef(BaseModel):
    model_id: str  # "banister_pmc", "cp_2p", "coggan_tss", "readiness_v1", "guardrail.hit_spacing"
    version: str
    inputs: dict[str, Any]  # exact inputs used
    doc: str  # docs/glossary/banister_pmc.md
    claim: (
        str | None
    )  # evidence claim id in core/evidence.py (§6), e.g. "banister_pmc.load_history"
```

Rules:
- Pure functions in `analysis/` and `planning/` return `(result, Explanation)` or expose
  `explain(result)`; they never format prose beyond `headline_zh`/`text_zh` templates.
- Explanations are **persisted** as JSON next to the result (`activity_metrics.explanation`,
  `readiness_daily.explanation`, `planned_workouts.explanation`, `plan_revisions.explanation`).
- Rendering to Markdown / icu NOTE / LLM narration reads only persisted explanations.

## 2. Glossary (docs/glossary/, zh-TW, one file per model)
Each file: 這是什麼 · 為什麼用它 · 怎麼算（含公式與一個用你自己數據的例子）· 限制 · 參考文獻。
Initial list: `coggan_np_if_tss`, `banister_pmc`, `ramp_rate_acwr_monotony`, `cp_wprime`,
`eftp`, `decoupling_hr_lag`, `efficiency_factor`, `time_in_zone_tid`, `readiness_v1`,
`periodization_3_1`, `ftp_target_season`, `guardrails`, `workout_library`. Glossary ids are
referenced from `Explanation.glossary_terms` and linked from every report.

## 3. Where explanations surface
| Surface | Content |
|---------|---------|
| Daily report (`data/reports/YYYY-MM-DD.md`) | 昨天騎乘解讀 → 今日狀態判斷（含每個因素權重）→ 今天課表 + 為什麼是這個 + 和原計畫差在哪 |
| icu planned-workout `description` footer | 2–3 行「為什麼排這個」+ 連到當日報告 |
| icu NOTE (daily) | 狀態判斷一句話 + 主要原因 |
| Weekly report | 本週 vs 計畫、FTP 證據、下週方向、用到的模型一覽 |
| `cyp explain <key>` CLI | 印出任一結果的完整 Explanation 與 glossary 連結 |
| Future UI (read-only API `/v1/explain/{key}`) | same JSON |

## 4. Coach narration (optional LLM)
Input = persisted `Explanation` objects only. Output = prose that must quote the same numbers
(validated by a regex check that every number in the output exists in the inputs). Falls back
to template rendering when disabled or on validation failure.

## 5. Hooks to build early
- M0: `core/explain.py`, `explanation` JSON columns on the four tables, `cyp explain` stub.
- M2: every metric function returns an Explanation; glossary stubs with formulas.
- M4: planner emits one Explanation per day and per guardrail repair.
- M5: zh-TW glossary complete, report templates, icu footer/NOTE, LLM narration.

## 6. Evidence grades (core/evidence.py)
Each scientific claim the reports or the planner rely on is graded per claim, not per model
(`docs/specs/research-review.md`): CP as the heavy/severe boundary is **A**, W′bal predicting the
failing rep is **C**.

| Grade | Meaning | Allowed use |
|-------|---------|-------------|
| **A** | Meta-analyses / several controlled studies agree, or strong physiological consensus | May drive a verdict alone |
| **B** | Group-level support; individual error is material | May drive a verdict; show the uncertainty |
| **C** | Heuristic, proprietary formula, single small study, team-sport data only, or contested | Context only, badged 參考（證據弱） |

- `REGISTRY` in `src/cyp/core/evidence.py` holds claim id → glossary id, grade, citations and the
  review date. Every glossary file has a `## 證據等級` table with the same rows
  (`tests/test_evidence.py`). Grades are data: the monthly review proposes changes, a human
  approves them, and registry and glossary change in one commit.
- `MethodRef.claim` (optional) names the claim an Explanation rests on; the renderer badges the
  headline when that claim is grade C. Unknown ids raise.
- **Verdict rule:** a verdict or warning may rest only on A/B claims
  (`evidence.verdict_allowed`). Report warnings carry their `claim`; a grade-C warning is shown
  as 參考（證據弱） context instead of ⚠️, with its numbers kept. Today ACWR, monotony, the CTL
  ramp cap and the TSB floor are all grade C, so the daily/weekly reports show them as context.
- A grade change never changes a threshold or planner rule; that needs an ADR (ADR-0004).
