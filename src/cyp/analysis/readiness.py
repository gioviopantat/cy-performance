"""Daily readiness v2: hard rules, then a weighted z-score (docs/04 §4, glossary readiness_v1).

This module is the authority; the glossary page mirrors it.

**Rules** (first match decides, then the score may only lower the verdict further):

1. Sick / injured: an icu calendar event of category ``SICK`` / ``INJURED`` covering the day,
   or wellness ``injury`` ≥ :data:`INJURY_REST_MIN` → ``REST`` (status ``SICK``).
2. HRV component ≤ :data:`HRV_REST_COMPONENT` (the 7-day mean ≥ 1.25 SD below baseline)
   **and** resting-HR z > +1.5 → ``REST`` (status ``OVERREACHED``).
3. Yesterday's post-ride feel 5/5 or RPE ≥ :data:`RPE_SURPRISE` above the session's intent
   (flag ``readiness.ride_feel``) → at most ``EASY``.
4. Yesterday's load > 130 % of its plan → at most ``EASY``.

**HRV (v2, ADR-0013)**: the mean of ln(rMSSD) over the :data:`HRV_WINDOW_DAYS` days ending
today (today included, missing nights skipped, ≥ :data:`MIN_HRV_WINDOW_N` nights required,
Plews 2014) is compared with the daily ln(rMSSD) values of the baseline window:
``z7 = (mean7 − mean_base) / sd_base``. Inside ±:data:`HRV_SWC_SD` SD (the smallest worthwhile
change band of the HRV-guided trials) the component is 0; outside it each further 0.5 SD is
one unit: ``hrv = sign(z7)·(|z7| − 0.5)/0.5``, clipped ±2. v1 used the single-day z.

**Score**: each available component is a direction-normalised z (positive = good), clipped to
±2; ``S = Σ wᵢ·zᵢ / max(Σ wᵢ, MIN_TOTAL_WEIGHT)`` over *present* components (missing →
dropped, weights renormalised, but when the present weights sum to less than
:data:`MIN_TOTAL_WEIGHT` the shortfall counts as a neutral 0, ADR-0016);
``score = clamp(50 + 25·S, 0, 100)``.

| component | z | weight |
|-----------|---|--------|
| ``hrv`` | 7-day ln(rMSSD) mean vs baseline, ±0.5 SD dead band (above) | 0.30 |
| ``rhr`` | −z of resting HR | 0.15 |
| ``sleep`` | mean of z(sleep hours), z(sleep score) | 0.20 |
| ``tsb`` | piecewise: −30 → −1, 0 → 0, +10 → +1 | 0.15 |
| ``ride`` | mean of yesterday's feel / RPE signals (body only, ADR-0017) | 0.20 |
| ``subjective`` | −mean z of soreness, fatigue, stress, mood (icu 1–4, 1 = best) | 0.20 |

Baselines: the :data:`BASELINE_DAYS` days before the day (the day itself excluded); a component
needs ≥ :data:`MIN_BASELINE_N` baseline values and a non-zero sd. Yesterday's decoupling and HR
lag are not inputs (v2, ADR-0012): they describe durability, not tomorrow's readiness.

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

ALGO_VERSION = "readiness_v2.2"
#: Same algorithm, with post-ride RPE / feel among the inputs (flag ``readiness.ride_feel``).
ALGO_VERSION_FEEL = "readiness_v2.3"
BASELINE_DAYS = 60
MIN_BASELINE_N = 14
#: HRV is read as the rolling mean of ln(rMSSD) over this many days (Plews 2013).
HRV_WINDOW_DAYS = 7
#: Valid nights needed inside the window (Plews 2014: three per week suffice).
MIN_HRV_WINDOW_N = 3
#: Half-width of the normal band, in baseline SDs (Javaloyes 2019, Vesterinen 2016).
HRV_SWC_SD = 0.5
#: Rule 2 fires at or below this HRV component (= the 7-day mean 1.25 SD under baseline).
HRV_REST_COMPONENT = -1.5
#: Below this total weight of present components the rest counts as neutral (ADR-0016), so a
#: day with only TSB (0.15) is not scored as if TSB were the whole picture.
MIN_TOTAL_WEIGHT = 0.5
RHR_REST_Z = 1.5
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

    classification: str | None = None
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
    # Load vs plan is not a body signal: TSB already carries the load, and the planner reacts
    # to an overdone day itself (planning/adapt.py, ADR-0017). Only how the ride felt counts.
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
class HrvWindow:
    """The 7-day ln(rMSSD) mean against the baseline (see the module docstring)."""

    z7: float
    mean7_ln: float
    n_days: int
    baseline_ln: float
    baseline_sd: float

    @property
    def component(self) -> float:
        """0 inside ±:data:`HRV_SWC_SD`; one unit per further 0.5 SD outside, clipped ±2."""
        excess = max(abs(self.z7) - HRV_SWC_SD, 0.0) / 0.5
        return clip(math.copysign(excess, self.z7))

    @property
    def band(self) -> Literal["below", "within", "above"]:
        """Where the 7-day mean sits relative to the ±0.5 SD band."""
        if self.z7 < -HRV_SWC_SD:
            return "below"
        return "above" if self.z7 > HRV_SWC_SD else "within"


def _ln(v: float | None) -> float | None:
    return math.log(v) if v is not None and v > 0 else None


def hrv_window(
    day: dt.date, today: float | None, history: Sequence[WellnessPoint]
) -> HrvWindow | None:
    """7-day ln(rMSSD) mean vs the history's daily values; ``None`` when either is too thin."""
    lo = day - dt.timedelta(days=HRV_WINDOW_DAYS - 1)
    window = [_ln(h.hrv) for h in history if lo <= h.date < day] + [_ln(today)]
    vals = [v for v in window if v is not None]
    base = [v for v in (_ln(h.hrv) for h in history) if v is not None]
    if len(vals) < MIN_HRV_WINDOW_N or len(base) < MIN_BASELINE_N:
        return None
    sd = statistics.pstdev(base)
    if sd <= 1e-9:
        return None
    mean7, mean_base = statistics.fmean(vals), statistics.fmean(base)
    return HrvWindow((mean7 - mean_base) / sd, mean7, len(vals), mean_base, sd)


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

    hw = hrv_window(inp.date, t.hrv, hist)
    zs["hrv"] = hw.component if hw is not None else None
    if hw is not None:
        out.append(
            Component(
                "hrv",
                hw.component,
                WEIGHTS["hrv"],
                {
                    "hrv": t.hrv,
                    "raw_z": hw.z7,
                    "mean7_ln": hw.mean7_ln,
                    "n_days": hw.n_days,
                    "baseline_ln": hw.baseline_ln,
                    "baseline_sd": hw.baseline_sd,
                    "band": hw.band,
                },
            )
        )
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
    """``(S, score)``: weighted mean of the present components and its 0–100 map.

    The divisor is at least :data:`MIN_TOTAL_WEIGHT`: missing weight beyond what the present
    components cover is shrunk to a neutral 0. No components -> ``(0, 50)``.
    """
    total_w = sum(c.weight for c in comps)
    if total_w <= 0:
        return 0.0, 50.0
    s = sum(c.weight * c.z for c in comps) / max(total_w, MIN_TOTAL_WEIGHT)
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
        and (hz <= HRV_REST_COMPONENT and rz > RHR_REST_Z)
    ):
        rec, status = "REST", "OVERREACHED"
        z7 = next(c.raw["raw_z"] for c in comps if c.name == "hrv")
        rules.append(
            Reason(
                text_zh=(
                    f"HRV 7 日平均低於基準 {abs(z7):.1f} 個標準差，"
                    f"且安靜心率高 {rz:.1f} 個標準差 → 休息"
                ),
                evidence={"hrv_z7": round(z7, 2), "rhr_z": round(rz, 2)},
            )
        )
    else:
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
    if c.name == "hrv" and isinstance(raw_z, float | int):
        band = {"below": "低於", "within": "在", "above": "高於"}[c.raw.get("band", "within")]
        side = "高" if raw_z >= 0 else "低"
        return (
            f"HRV 7 日平均（{c.raw.get('n_days')} 晚）比你的基準{side} {abs(raw_z):.1f} 個標準差，"
            f"{band}正常範圍（±{HRV_SWC_SD}）（{effect}）"
        )
    if c.name in {"rhr", "sleep"} and isinstance(raw_z, float | int):
        side = "高" if raw_z >= 0 else "低"
        return f"{COMPONENT_ZH[c.name]}比你的基準{side} {abs(raw_z):.1f} 個標準差（{effect}）"
    if c.name == "tsb":
        return f"進入今天的 TSB {c.raw.get('tsb'):+.1f}，換算 {c.z:+.2f}（{effect}）"
    if c.name == "ride":
        parts = c.raw.get("parts") or {}
        names = {"feel": "騎完感覺", "rpe": "RPE"}
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
    divisor = max(total_w, MIN_TOTAL_WEIGHT)
    for c in sorted(comps, key=lambda c: -abs(c.weight * c.z)):
        share = c.weight / divisor if total_w else 0.0
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
    if 0 < total_w < MIN_TOTAL_WEIGHT:
        neutral = 1.0 - total_w / MIN_TOTAL_WEIGHT
        because.append(
            Reason(
                text_zh=(
                    f"有資料的輸入權重只有 {total_w:.2f}（< {MIN_TOTAL_WEIGHT}），"
                    f"不足的 {neutral * 100:.0f} % 當成中性 0，避免單一輸入決定今天"
                ),
                evidence={
                    "present_weight": round(total_w, 2),
                    "min_total_weight": MIN_TOTAL_WEIGHT,
                    "neutral_share_pct": round(neutral * 100),
                },
                weight=round(neutral, 3),
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
                "min_total_weight": MIN_TOTAL_WEIGHT,
                "hrv_window_days": HRV_WINDOW_DAYS,
                "min_hrv_window_n": MIN_HRV_WINDOW_N,
                "hrv_swc_sd": HRV_SWC_SD,
                "weighted_sum": round(s, 4),
            },
            doc="docs/glossary/readiness_v1.md",
            claim="readiness_v1.weights",
        ),
        confidence=confidence,
        glossary_terms=["readiness_v1", "banister_pmc"],
    )


def wellness_coverage(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many days carry each readiness-relevant wellness field (non-null)."""
    counts = dict.fromkeys(WELLNESS_FIELDS, 0)
    for r in rows:
        for f in WELLNESS_FIELDS:
            if r.get(f) is not None:
                counts[f] += 1
    return counts
