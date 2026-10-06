"""Limiter detection: what most likely holds FTP back, as a planner input (docs/04 §1, §3).

Each rule compares one observable with a reference and, when it fires, emits a
:class:`Limiter` carrying a ``severity`` in 0–1 and a ``template_bias`` (workout ``intent`` ->
multiplicative weight) that the planner may use when choosing among eligible templates.
Rules (all thresholds named constants, all evidence quoted in the Explanation):

- ``vo2_ceiling`` — MMP 5 min / FTP < 1.12: the aerobic ceiling sits close to threshold, so
  raising FTP further needs VO2 work (typical ratio 1.15–1.25).
- ``sustained_power`` — MMP 20 min / FTP < 1.02: recent best 20 min does not support the FTP
  setting (no recent hard 20 min, or FTP set high) -> threshold / sweet-spot extension.
- Best-effort rules (``vo2_ceiling``, ``sustained_power``) need evidence that the window
  holds *near-maximal* attempts (:class:`MaxEffortEvidence`). Without it the rule emits
  ``status="insufficient_data"`` (severity 0, no planner bias): not riding hard is not the
  same as being unable to (travel, a bike in service, legs tired from other sports).
- ``durability`` — late/fresh EF ratio < 0.95 on 1 000+ kJ rides -> long rides with late
  sweet-spot / tempo.
- ``grey_zone`` — mean mid-zone share > 30 % over the last 4 weeks -> keep Z2 easy.
- ``aerobic_volume`` — CTL below ``ctl_floor`` (default 55) for a 300 W goal -> volume first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cyp.core.explain import Explanation, MethodRef, Reason

LIMITERS_VERSION = "limiters_v3"  # v3: "maximal" = threshold-level, not sweet spot
VO2_RATIO_MIN = 1.12
SUSTAINED_RATIO_MIN = 1.02
DURABILITY_RATIO_MIN = 0.95
GREY_ZONE_MAX = 0.30
CTL_FLOOR = 55.0


#: Evidence thresholds for "a near-maximal attempt happened in the window".
SUSTAINED_MIN_S = 900  # ≥ 15 min ...
SUSTAINED_FTP_FRAC = 1.00  # ... at ≥ 100 % FTP (power) or
SUSTAINED_LTHR_FRAC = 0.98  # ... at ≥ 98 % LTHR (heart rate)
# Sweet spot (88–94 % FTP, ~92–95 % LTHR) is hard but sub-maximal: riding it says nothing about
# whether the 20-min ceiling is above or below FTP, so it must not count as evidence.
SHORT_MIN_S = 180  # ≥ 3 min ...
SHORT_FTP_FRAC = 1.05  # ... at ≥ 105 % FTP or
SHORT_LTHR_FRAC = 1.00  # ... at ≥ 100 % LTHR
EVIDENCE_WINDOW_DAYS = 42

LimiterStatus = Literal["limiter", "insufficient_data"]


@dataclass
class MaxEffortEvidence:
    """Near-maximal attempts found in the last ``window_days`` (each item quotes its numbers).

    ``sustained`` backs ``sustained_power`` and downward FTP proposals (≥ 15 min at ≥ 100 %
    FTP or ≥ 98 % LTHR, or a test); ``short`` backs ``vo2_ceiling`` (≥ 3 min at ≥ 105 % FTP
    or ≥ 100 % LTHR, or a test).
    """

    window_days: int = EVIDENCE_WINDOW_DAYS
    sustained: list[dict[str, Any]] = field(default_factory=list)
    short: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """JSON-safe dict."""
        return {
            "window_days": self.window_days,
            "sustained": self.sustained[:5],
            "n_sustained": len(self.sustained),
            "short": self.short[:5],
            "n_short": len(self.short),
        }


@dataclass
class Limiter:
    """A detected limiter with its planner bias (or an ``insufficient_data`` check)."""

    id: str
    severity: float
    title_zh: str
    template_bias: dict[str, float] = field(default_factory=dict)
    explanation: Explanation | None = None
    status: LimiterStatus = "limiter"


def insufficient_reason_zh(kind: Literal["sustained", "short"], window_days: int) -> str:
    """zh-TW reason for a best-effort rule that cannot be judged."""
    if kind == "sustained":
        return (
            f"近 {window_days} 天沒有接近極限的長時間努力（≥ 15 分鐘、≥ 100 % FTP 或 ≥ 98 % "
            "LTHR，或測驗；甜蜜點不算），無法判斷 20 分鐘功率是否撐得起 FTP；沒騎硬 ≠ 騎不動"
        )
    return (
        f"近 {window_days} 天沒有接近極限的 3–8 分鐘努力（≥ 105 % FTP 或 ≥ 100 % LTHR，"
        "或測驗），無法判斷有氧天花板；沒騎硬 ≠ 騎不動"
    )


def _insufficient(
    lid: str, kind: Literal["sustained", "short"], window_days: int, evidence: dict[str, Any]
) -> Limiter:
    reason = insufficient_reason_zh(kind, window_days)
    expl = Explanation(
        key=f"limiter.{lid}",
        headline_zh=f"資料不足：{reason.split('，')[0]}，暫不判斷此限制因子",
        because=[Reason(text_zh=reason, evidence=evidence)],
        method=MethodRef(
            model_id="limiters",
            version=LIMITERS_VERSION,
            inputs={
                "window_days": window_days,
                "sustained_min_s": SUSTAINED_MIN_S,
                "sustained_ftp_frac": SUSTAINED_FTP_FRAC,
                "sustained_lthr_frac": SUSTAINED_LTHR_FRAC,
                "short_min_s": SHORT_MIN_S,
                "short_ftp_frac": SHORT_FTP_FRAC,
                "short_lthr_frac": SHORT_LTHR_FRAC,
            },
            doc="docs/04-analysis-engine.md",
        ),
        confidence="low",
        glossary_terms=["cp_wprime", "eftp"],
    )
    return Limiter(lid, 0.0, f"資料不足：{reason}", {}, expl, "insufficient_data")


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


def _expl(lid: str, headline: str, reasons: list[Reason], inputs: dict[str, object]) -> Explanation:
    return Explanation(
        key=f"limiter.{lid}",
        headline_zh=headline,
        because=reasons,
        method=MethodRef(
            model_id="limiters",
            version=LIMITERS_VERSION,
            inputs=inputs,
            doc="docs/04-analysis-engine.md",
        ),
        confidence="medium",
        glossary_terms=["cp_wprime", "time_in_zone_tid", "efficiency_factor"],
    )


def detect_limiters(
    *,
    ftp: float,
    mmp: dict[int, float],
    durability_ratio: float | None,
    mid_share_4w: float | None,
    ctl: float | None,
    ctl_floor: float = CTL_FLOOR,
    max_efforts: MaxEffortEvidence | None = None,
) -> list[Limiter]:
    """Evaluate all rules; result sorted by severity (highest first).

    With ``max_efforts`` given, the best-effort rules only fire when the window holds
    near-maximal attempts; otherwise they return an ``insufficient_data`` entry instead
    (``max_efforts=None`` keeps the v1 behaviour: best efforts are taken at face value).
    """
    out: list[Limiter] = []
    p5, p20 = mmp.get(300), mmp.get(1200)
    if p5 and ftp > 0 and p5 / ftp < VO2_RATIO_MIN and max_efforts and not max_efforts.short:
        out.append(
            _insufficient(
                "vo2_ceiling",
                "short",
                max_efforts.window_days,
                {"mmp_300": p5, "ftp": ftp, "ratio": round(p5 / ftp, 3), "n_short_efforts": 0},
            )
        )
    elif p5 and ftp > 0 and p5 / ftp < VO2_RATIO_MIN:
        r = p5 / ftp
        sev = _clip((VO2_RATIO_MIN - r) / 0.10 + 0.3)
        out.append(
            Limiter(
                "vo2_ceiling",
                sev,
                "有氧天花板偏低（5 分鐘功率離 FTP 太近）",
                {"vo2": 1.5, "anaerobic": 1.1},
                _expl(
                    "vo2_ceiling",
                    f"5 分鐘最佳 {p5:.0f} W 只有 FTP 的 {r * 100:.0f} %",
                    [
                        Reason(
                            text_zh=f"一般在 115–125 %，門檻 {VO2_RATIO_MIN * 100:.0f} %",
                            evidence={"mmp_300": p5, "ftp": ftp, "ratio": round(r, 3)},
                        ),
                        *_evidence_reason(max_efforts, "short"),
                    ],
                    {"vo2_ratio_min": VO2_RATIO_MIN},
                ),
            )
        )
    p20_low = (not p20 or p20 / ftp < SUSTAINED_RATIO_MIN) if ftp > 0 else False
    if p20_low and max_efforts is not None and not max_efforts.sustained:
        out.append(
            _insufficient(
                "sustained_power",
                "sustained",
                max_efforts.window_days,
                {
                    "mmp_1200": p20,
                    "ftp": ftp,
                    "ratio": round(p20 / ftp, 3) if p20 else None,
                    "n_sustained_efforts": 0,
                },
            )
        )
    elif p20 and ftp > 0 and p20 / ftp < SUSTAINED_RATIO_MIN:
        r = p20 / ftp
        sev = _clip((SUSTAINED_RATIO_MIN - r) / 0.08 + 0.3)
        out.append(
            Limiter(
                "sustained_power",
                sev,
                "近期 20 分鐘功率撐不起目前 FTP",
                {"threshold": 1.4, "sweetspot": 1.3},
                _expl(
                    "sustained_power",
                    f"20 分鐘最佳 {p20:.0f} W 是 FTP 的 {r * 100:.0f} %",
                    [
                        Reason(
                            text_zh=(
                                f"FTP ≈ 95 % 的 20 分鐘功率，所以 20 分鐘至少應是 FTP 的 "
                                f"{SUSTAINED_RATIO_MIN * 100:.0f} %"
                            ),
                            evidence={"mmp_1200": p20, "ftp": ftp, "ratio": round(r, 3)},
                        ),
                        *_evidence_reason(max_efforts, "sustained"),
                    ],
                    {"sustained_ratio_min": SUSTAINED_RATIO_MIN},
                ),
            )
        )
    if durability_ratio is not None and durability_ratio < DURABILITY_RATIO_MIN:
        sev = _clip((DURABILITY_RATIO_MIN - durability_ratio) / 0.05 + 0.3)
        out.append(
            Limiter(
                "durability",
                sev,
                "長時間後效率下滑（耐久力）",
                {"endurance": 1.2, "tempo": 1.2, "sweetspot": 1.1},
                _expl(
                    "durability",
                    f"1000 kJ 後效率只剩 {durability_ratio * 100:.1f} %",
                    [
                        Reason(
                            text_zh=f"門檻 {DURABILITY_RATIO_MIN * 100:.0f} %",
                            evidence={"ratio": durability_ratio},
                        )
                    ],
                    {"durability_ratio_min": DURABILITY_RATIO_MIN},
                ),
            )
        )
    if mid_share_4w is not None and mid_share_4w > GREY_ZONE_MAX:
        sev = _clip((mid_share_4w - GREY_ZONE_MAX) / 0.15 + 0.3)
        out.append(
            Limiter(
                "grey_zone",
                sev,
                "中強度（灰色地帶）比例太高",
                {"endurance": 1.3, "recovery": 1.1, "tempo": 0.7},
                _expl(
                    "grey_zone",
                    f"近 4 週中區佔 {mid_share_4w * 100:.0f} %",
                    [
                        Reason(
                            text_zh=f"上限 {GREY_ZONE_MAX * 100:.0f} %；Z2 要騎得更慢",
                            evidence={"mid_share_4w": round(mid_share_4w, 3)},
                        )
                    ],
                    {"grey_zone_max": GREY_ZONE_MAX},
                ),
            )
        )
    if ctl is not None and ctl < ctl_floor:
        sev = _clip((ctl_floor - ctl) / 20.0 + 0.2)
        out.append(
            Limiter(
                "aerobic_volume",
                sev,
                "有氧基礎量（CTL）不足",
                {"endurance": 1.3},
                _expl(
                    "aerobic_volume",
                    f"CTL {ctl:.0f}，低於 300 W 目標建議的 {ctl_floor:.0f}",
                    [
                        Reason(
                            text_zh="FTP 的長期上限受訓練量支撐",
                            evidence={"ctl": round(ctl, 1), "ctl_floor": ctl_floor},
                        )
                    ],
                    {"ctl_floor": ctl_floor},
                ),
            )
        )
    out.sort(key=lambda lim: -lim.severity)
    return out


def _evidence_reason(
    ev: MaxEffortEvidence | None, kind: Literal["sustained", "short"]
) -> list[Reason]:
    if ev is None:
        return []
    items = ev.sustained if kind == "sustained" else ev.short
    if not items:
        return []
    top = items[0]
    return [
        Reason(
            text_zh=(
                f"近 {ev.window_days} 天有 {len(items)} 次接近極限的努力"
                f"（例：{top.get('date')} {top.get('label_zh', '')}），最佳值可信"
            ),
            evidence={"n": len(items), "example": top},
        )
    ]


def combined_bias(limiters: list[Limiter]) -> dict[str, float]:
    """Severity-weighted product of every limiter's bias: ``1 + sev*(b-1)`` per intent.

    ``insufficient_data`` entries carry no bias and are skipped.
    """
    bias: dict[str, float] = {}
    for lim in limiters:
        if lim.status != "limiter":
            continue
        for intent, b in lim.template_bias.items():
            bias[intent] = bias.get(intent, 1.0) * (1.0 + lim.severity * (b - 1.0))
    return {k: round(v, 3) for k, v in sorted(bias.items())}
