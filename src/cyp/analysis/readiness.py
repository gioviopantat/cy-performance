"""Daily readiness v1: hard rules, then a weighted z-score (docs/04 §4, glossary readiness_v1).

This module is the authority; the glossary page mirrors it.

**Rules** (first match decides, then the score may only lower the verdict further):

1. Sick / injured: an icu calendar event of category ``SICK`` / ``INJURED`` covering the day,
   or wellness ``injury`` ≥ :data:`INJURY_REST_MIN` → ``REST`` (status ``SICK``).
2. HRV z < −1.5 **and** resting-HR z > +1.5 → ``REST`` (status ``OVERREACHED``).
3. Yesterday's ride status ``BLUNTED`` → at most ``EASY``.
4. Yesterday's load > 130 % of its plan → at most ``EASY``.

**Score**: each available component is a direction-normalised z (positive = good), clipped to
±2; ``S = Σ wᵢ·zᵢ / Σ wᵢ`` over *present* components (missing → dropped, weights
renormalised); ``score = clamp(50 + 25·S, 0, 100)``.

| component | z | weight |
|-----------|---|--------|
| ``hrv`` | z of ln(rMSSD) vs trailing baseline | 0.30 |
| ``rhr`` | −z of resting HR | 0.15 |
| ``sleep`` | mean of z(sleep hours), z(sleep score) | 0.20 |
| ``tsb`` | piecewise: −30 → −1, 0 → 0, +10 → +1 | 0.15 |
| ``ride`` | mean of yesterday's decoupling / load-vs-plan / HR-lag signals | 0.20 |
| ``subjective`` | −mean z of soreness, fatigue, stress, mood (icu 1–4, 1 = best) | 0.20 |

Baselines: the :data:`BASELINE_DAYS` days before the day (the day itself excluded); a component
needs ≥ :data:`MIN_BASELINE_N` baseline values and a non-zero sd.

**Verdict**: ≥ 65 → ``UPGRADE`` if TSB > −10 and yesterday was not a hard day, else
``AS_PLANNED``; 45–64 → ``AS_PLANNED``; 30–44 → ``EASY``; < 30 → ``REST``. Then rule caps.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.core.load import Readiness, ReadinessStatus, Recommendation

ALGO_VERSION = "readiness_v1"
#: Same algorithm, with post-ride RPE / feel among the inputs (flag ``readiness.ride_feel``).
ALGO_VERSION_FEEL = "readiness_v1.1"
BASELINE_DAYS = 60
MIN_BASELINE_N = 14
Z_CLIP = 2.0
INJURY_REST_MIN = 3
WEIGHTS: dict[str, float] = {
    "hrv": 0.30,
    "rhr": 0.15,
    "sleep": 0.20,
    "tsb": 0.15,
    "ride": 0.20,
    "subjective": 0.20,
}
COMPONENT_ZH: dict[str, str] = {
    "hrv": "HRV",
    "rhr": "安靜心率",
    "sleep": "睡眠",
    "tsb": "訓練狀態 TSB",
    "ride": "昨天的騎乘反應",
    "subjective": "主觀感受",
}
SUBJECTIVE_FIELDS: tuple[str, ...] = ("soreness", "fatigue", "stress", "mood")
#: icu's 1–4 scales list the best answer first (1 = low soreness / great mood).
SUBJECTIVE_HIGHER_IS_WORSE = True
HARD_CLASSES = frozenset({"vo2", "threshold", "race", "sweetspot"})
_ORDER: tuple[Recommendation, ...] = ("REST", "EASY", "AS_PLANNED", "UPGRADE")
#: Wellness fields readiness can use, for the coverage report.
WELLNESS_FIELDS: tuple[str, ...] = (
    "hrv",
    "hrv_sdnn",
    "resting_hr",
    "sleep_s",
    "sleep_score",
    "sleep_quality",
    "soreness",
    "fatigue",
    "stress",
    "mood",
    "motivation",
    "injury",
    "readiness_icu",
    "weight_kg",
)


@dataclass
class WellnessPoint:
    """The wellness fields readiness reads for one day (``None`` = not recorded)."""

    date: dt.date
    hrv: float | None = None
    resting_hr: float | None = None
    sleep_s: float | None = None
    sleep_score: float | None = None
    soreness: float | None = None
    fatigue: float | None = None
    stress: float | None = None
    mood: float | None = None
    injury: float | None = None


@dataclass
class YesterdayRide:
    """Yesterday's training response (aggregated over the day's rides)."""

    status: str | None = None
    classification: str | None = None
    decoupling_pct: float | None = None
    decoupling_reliable: bool = False
    hr_lag_s: float | None = None
    load: float | None = None
    planned_load: float | None = None
    #: Post-ride RPE 1–10 and feel 1 (strong) … 5 (weak), from intervals.icu or the web UI.
    rpe: float | None = None
    feel: int | None = None
    #: Class and moving minutes of the ride the RPE / feel belong to (the longest rated ride).
    rated_classification: str | None = None
    rated_minutes: float | None = None


#: The RPE a session of each class is meant to feel like (Borg CR-10 style, 1–10).
EXPECTED_RPE: dict[str, float] = {
    "recovery": 2.0,
    "endurance": 3.0,
    "tempo": 5.0,
    "sweetspot": 6.0,
    "threshold": 7.0,
    "vo2": 8.0,
    "race": 9.0,
}
#: RPE this far above the session's intent caps today at EASY.
RPE_SURPRISE = 3.0
FEEL_WEAK = 5
#: Rides longer than this feel harder at the same intent: +1 expected RPE per extra hour, ≤ +2.
LONG_RIDE_RPE_MIN = 180.0
LONG_RIDE_RPE_MAX_EXTRA = 2.0


def expected_rpe(y: YesterdayRide) -> float:
    """The RPE the rated ride was meant to feel like (its class, plus a long-ride allowance)."""
    base = EXPECTED_RPE.get(y.rated_classification or y.classification or "", 4.0)
    extra = max((y.rated_minutes or 0.0) - LONG_RIDE_RPE_MIN, 0.0) / 60.0
    return base + min(extra, LONG_RIDE_RPE_MAX_EXTRA)


@dataclass
class ReadinessInputs:
    """Everything :func:`compute_readiness` needs for one day."""

    date: dt.date
    today: WellnessPoint | None
    history: Sequence[WellnessPoint] = field(default_factory=list)
    tsb: float | None = None
    yesterday: YesterdayRide | None = None
    sick_or_injured: str | None = None  # icu event category, if any


# ---------------------------------------------------------------------------- components


def zscore(value: float | None, baseline: Iterable[float | None]) -> float | None:
    """Z of ``value`` against ``baseline``; ``None`` if too few points or no spread."""
    vals = [float(v) for v in baseline if v is not None and math.isfinite(float(v))]
    if value is None or len(vals) < MIN_BASELINE_N:
        return None
    sd = statistics.pstdev(vals)
    if sd <= 1e-9:
        return None
    return (float(value) - statistics.fmean(vals)) / sd


def clip(z: float) -> float:
    """Clip to ±:data:`Z_CLIP`."""
    return max(-Z_CLIP, min(Z_CLIP, z))


def tsb_component(tsb: float | None) -> float | None:
    """Piecewise-linear TSB map: −30 → −1, 0 → 0, +10 → +1 (clipped ±2)."""
    if tsb is None:
        return None
    return clip(tsb / 30.0 if tsb < 0 else tsb / 10.0)


def ride_component(y: YesterdayRide | None) -> tuple[float | None, dict[str, float]]:
    """Mean of yesterday's response signals (positive = good) and the parts used."""
    if y is None:
        return None, {}
    parts: dict[str, float] = {}
    if y.decoupling_pct is not None and y.decoupling_reliable:
        # ≤ 5 % is normal aerobic drift; every 3 % beyond costs one "z".
        parts["decoupling"] = clip(-max(y.decoupling_pct - 5.0, 0.0) / 3.0)
    if y.load is not None and y.planned_load:
        ratio = y.load / y.planned_load
        parts["load_vs_plan"] = clip(-max(ratio - 1.1, 0.0) / 0.2)
    if y.hr_lag_s is not None:
        # Ride analysis calls ≤ 45 s fresh and ≥ 90 s BLUNTED; −1 at the BLUNTED line.
        parts["hr_lag"] = clip(-max(y.hr_lag_s - 45.0, 0.0) / 45.0)
    if y.status == "BLUNTED":
        parts["blunted"] = -1.0
    if y.feel is not None:
        # icu feel: 1 strong … 3 normal … 5 weak -> +1 … 0 … −1.
        parts["feel"] = clip((3 - y.feel) / 2.0)
    if y.rpe is not None:
        expected = expected_rpe(y)
        # Every 2 RPE points harder than the session intended cost one "z"; easier helps.
        parts["rpe"] = clip(-(y.rpe - expected) / 2.0)
    if not parts:
        return None, {}
    return sum(parts.values()) / len(parts), parts


@dataclass
class Component:
    """One scored input."""

    name: str
    z: float
    weight: float
    raw: dict[str, Any] = field(default_factory=dict)


def components(inp: ReadinessInputs) -> tuple[list[Component], dict[str, float | None]]:
    """Every available component, plus the raw z-scores used by the rules."""
    t = inp.today or WellnessPoint(inp.date)
    hist = list(inp.history)
    out: list[Component] = []
    zs: dict[str, float | None] = {}

    def ln(v: float | None) -> float | None:
        return math.log(v) if v is not None and v > 0 else None

    z_hrv = zscore(ln(t.hrv), [ln(h.hrv) for h in hist])
    zs["hrv"] = z_hrv
    if z_hrv is not None:
        out.append(Component("hrv", clip(z_hrv), WEIGHTS["hrv"], {"hrv": t.hrv, "raw_z": z_hrv}))
    z_rhr = zscore(t.resting_hr, [h.resting_hr for h in hist])
    zs["rhr"] = z_rhr
    if z_rhr is not None:
        out.append(
            Component(
                "rhr", clip(-z_rhr), WEIGHTS["rhr"], {"resting_hr": t.resting_hr, "raw_z": z_rhr}
            )
        )
    sleep_parts = [
        z
        for z in (
            zscore(t.sleep_s, [h.sleep_s for h in hist]),
            zscore(t.sleep_score, [h.sleep_score for h in hist]),
        )
        if z is not None
    ]
    if sleep_parts:
        z = sum(sleep_parts) / len(sleep_parts)
        out.append(
            Component(
                "sleep",
                clip(z),
                WEIGHTS["sleep"],
                {
                    "sleep_h": round(t.sleep_s / 3600, 2) if t.sleep_s else None,
                    "sleep_score": t.sleep_score,
                    "raw_z": z,
                },
            )
        )
    tz = tsb_component(inp.tsb)
    if tz is not None:
        out.append(Component("tsb", tz, WEIGHTS["tsb"], {"tsb": inp.tsb}))
    rz, rparts = ride_component(inp.yesterday)
    if rz is not None:
        out.append(Component("ride", rz, WEIGHTS["ride"], {"parts": rparts}))
    subj = [
        zf
        for f in SUBJECTIVE_FIELDS
        if (zf := zscore(getattr(t, f), [getattr(h, f) for h in hist])) is not None
    ]
    if subj:
        sign = -1.0 if SUBJECTIVE_HIGHER_IS_WORSE else 1.0
        z = sign * sum(subj) / len(subj)
        out.append(
            Component(
                "subjective",
                clip(z),
                WEIGHTS["subjective"],
                {f: getattr(t, f) for f in SUBJECTIVE_FIELDS},
            )
        )
    return out, zs


# ------------------------------------------------------------------------------- verdict


def _cap(rec: Recommendation, cap: Recommendation) -> Recommendation:
    return rec if _ORDER.index(rec) <= _ORDER.index(cap) else cap


def aggregate(comps: Sequence[Component]) -> tuple[float, float]:
    """``(S, score)``: renormalised weighted mean of the present components and its 0–100 map.

    No components -> ``(0, 50)`` (neutral, confidence low).
    """
    total_w = sum(c.weight for c in comps)
    if total_w <= 0:
        return 0.0, 50.0
    s = sum(c.weight * c.z for c in comps) / total_w
    return s, max(0.0, min(100.0, 50.0 + 25.0 * s))


def compute_readiness(inp: ReadinessInputs) -> Readiness:
    """Rules + weighted score -> :class:`Readiness` with a full Explanation."""
    comps, zs = components(inp)
    total_w = sum(c.weight for c in comps)
    s, score = aggregate(comps)
    y = inp.yesterday
    hard_yesterday = bool(y and y.classification in HARD_CLASSES)

    if score >= 65:
        rec: Recommendation = (
            "UPGRADE" if (inp.tsb is None or inp.tsb > -10) and not hard_yesterday else "AS_PLANNED"
        )
    elif score >= 45:
        rec = "AS_PLANNED"
    elif score >= 30:
        rec = "EASY"
    else:
        rec = "REST"
    status: ReadinessStatus = {
        "UPGRADE": "FRESH",
        "AS_PLANNED": "NORMAL",
        "EASY": "NORMAL",
        "REST": "OVERREACHED",
    }[rec]  # type: ignore[assignment]
    rules: list[Reason] = []
    t = inp.today
    injury = t.injury if t else None
    if inp.sick_or_injured or (injury is not None and injury >= INJURY_REST_MIN):
        rec, status = "REST", "SICK"
        rules.append(
            Reason(
                text_zh=(
                    "icu 日曆標記為" + (inp.sick_or_injured or "")
                    if inp.sick_or_injured
                    else f"傷病分數 {injury:.0f}（≥ {INJURY_REST_MIN}）"
                )
                + " → 今天休息",
                evidence={"event": inp.sick_or_injured, "injury": injury},
                weight=None,
            )
        )
    elif (
        (hz := zs.get("hrv")) is not None
        and (rz := zs.get("rhr")) is not None
        and (hz < -1.5 and rz > 1.5)
    ):
        rec, status = "REST", "OVERREACHED"
        rules.append(
            Reason(
                text_zh=(
                    f"HRV 低於基準 {abs(hz):.1f} 個標準差且安靜心率高 {rz:.1f} 個標準差 → 休息"
                ),
                evidence={"hrv_z": round(hz, 2), "rhr_z": round(rz, 2)},
            )
        )
    else:
        if y and y.status == "BLUNTED":
            if _ORDER.index(rec) > _ORDER.index("EASY"):
                rec = "EASY"
            status = "BLUNTED"
            rules.append(
                Reason(
                    text_zh="昨天騎乘判定心率鈍化（BLUNTED）→ 今天最多輕鬆騎",
                    evidence={"yesterday_status": "BLUNTED"},
                )
            )
        surprise = y.rpe - expected_rpe(y) if y and y.rpe is not None else None
        rpe_hit = surprise is not None and surprise >= RPE_SURPRISE
        if y and ((y.feel is not None and y.feel >= FEEL_WEAK) or rpe_hit):
            rec = _cap(rec, "EASY")
            rules.append(
                Reason(
                    text_zh=(
                        "昨天騎完的感受很差"
                        + (
                            f"（RPE {y.rpe:.0f}，比這類課預期高 {surprise:.0f}）"
                            if rpe_hit and y.rpe is not None and surprise is not None
                            else ""
                        )
                        + (f"（感覺 {y.feel}/5）" if y.feel is not None else "")
                        + " → 今天最多輕鬆騎"
                    ),
                    evidence={"rpe": y.rpe, "feel": y.feel, "classification": y.classification},
                )
            )
        if y and y.load is not None and y.planned_load and y.load > 1.3 * y.planned_load:
            rec = _cap(rec, "EASY")
            rules.append(
                Reason(
                    text_zh=(
                        f"昨天負荷 {y.load:.0f} 超過計畫 {y.planned_load:.0f} 的 130 % → 最多輕鬆騎"
                    ),
                    evidence={"load": y.load, "planned_load": y.planned_load},
                )
            )

    return Readiness(
        date_local=inp.date,
        score_0_100=round(score, 1),
        status=status,
        recommendation=rec,
        inputs={
            "components": {
                c.name: {"z": round(c.z, 3), "weight": c.weight, **_json(c.raw)} for c in comps
            },
            "missing": [k for k in WEIGHTS if k not in {c.name for c in comps}],
            "weighted_sum": round(s, 4),
            "tsb": inp.tsb,
            "rule_hits": [r.text_zh for r in rules],
        },
        explanation=_explain(inp, comps, total_w, s, score, rec, status, rules),
        algo_version=ALGO_VERSION_FEEL if _rated(inp.yesterday) else ALGO_VERSION,
    )


def _rated(y: YesterdayRide | None) -> bool:
    return y is not None and (y.rpe is not None or y.feel is not None)


def _json(d: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = round(v, 3) if isinstance(v, float) else v
    return out


REC_ZH: dict[str, str] = {
    "REST": "休息",
    "EASY": "輕鬆騎（只騎 Z2，TSS 不超過原計畫 60 %）",
    "AS_PLANNED": "照課表",
    "UPGRADE": "狀態好，可進一步",
}


def _component_text(c: Component) -> str:
    """One readable line per component; ``c.z`` is already direction-normalised (+ = good)."""
    effect = "有利" if c.z > 0.05 else "不利" if c.z < -0.05 else "中性"
    raw_z = c.raw.get("raw_z")
    if c.name in {"hrv", "rhr", "sleep"} and isinstance(raw_z, float | int):
        side = "高" if raw_z >= 0 else "低"
        return f"{COMPONENT_ZH[c.name]}比你的基準{side} {abs(raw_z):.1f} 個標準差（{effect}）"
    if c.name == "tsb":
        return f"進入今天的 TSB {c.raw.get('tsb'):+.1f}，換算 {c.z:+.2f}（{effect}）"
    if c.name == "ride":
        parts = c.raw.get("parts") or {}
        names = {
            "decoupling": "解耦",
            "load_vs_plan": "負荷對計畫",
            "hr_lag": "心率延遲",
            "blunted": "鈍化",
        }
        detail = "、".join(f"{names.get(k, k)} {v:+.1f}" for k, v in parts.items())
        return f"昨天騎乘反應 {c.z:+.2f}（{detail}；{effect}）"
    return f"{COMPONENT_ZH[c.name]} {c.z:+.2f}（主觀分數相對基準；{effect}）"


def _explain(
    inp: ReadinessInputs,
    comps: list[Component],
    total_w: float,
    s: float,
    score: float,
    rec: Recommendation,
    status: str,
    rules: list[Reason],
) -> Explanation:
    because: list[Reason] = list(rules)
    for c in sorted(comps, key=lambda c: -abs(c.weight * c.z)):
        share = c.weight / total_w if total_w else 0.0
        because.append(
            Reason(
                text_zh=_component_text(c),
                evidence={"component": c.name, "z": round(c.z, 3), **_json(c.raw)},
                weight=round(share, 3),
            )
        )
    missing = [COMPONENT_ZH[k] for k in WEIGHTS if k not in {c.name for c in comps}]
    if missing:
        because.append(
            Reason(
                text_zh="缺少資料、權重已重新分配：" + "、".join(missing),
                evidence={"missing": missing},
            )
        )
    n_obj = sum(1 for c in comps if c.name in {"hrv", "rhr", "sleep"})
    confidence: Literal["high", "medium", "low"] = (
        "high" if n_obj >= 3 else "medium" if n_obj >= 1 or len(comps) >= 2 else "low"
    )
    return Explanation(
        key=f"readiness.{inp.date.isoformat()}",
        headline_zh=f"準備度 {score:.0f} 分 → {REC_ZH[rec]}（{status}）",
        because=because,
        method=MethodRef(
            model_id="readiness_v1",
            version=ALGO_VERSION,
            inputs={
                "weights": WEIGHTS,
                "baseline_days": BASELINE_DAYS,
                "min_baseline_n": MIN_BASELINE_N,
                "weighted_sum": round(s, 4),
            },
            doc="docs/glossary/readiness_v1.md",
        ),
        confidence=confidence,
        glossary_terms=["readiness_v1", "banister_pmc", "decoupling_hr_lag"],
    )


def wellness_coverage(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many days carry each readiness-relevant wellness field (non-null)."""
    counts = dict.fromkeys(WELLNESS_FIELDS, 0)
    for r in rows:
        for f in WELLNESS_FIELDS:
            if r.get(f) is not None:
                counts[f] += 1
    return counts
