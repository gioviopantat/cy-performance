"""Limiter detection: what most likely holds FTP back, as a planner input (docs/04 §1, §3).

Each rule compares one observable with a reference and, when it fires, emits a
:class:`Limiter` carrying a ``severity`` in 0–1 and a ``template_bias`` (workout ``intent`` ->
multiplicative weight) that the planner may use when choosing among eligible templates.
Rules (all thresholds named constants, all evidence quoted in the Explanation):

- ``vo2_ceiling`` — MMP 5 min / FTP < 1.12: the aerobic ceiling sits close to threshold, so
  raising FTP further needs VO2 work (typical ratio 1.15–1.25).
- ``sustained_power`` — MMP 20 min / FTP < 1.02: recent best 20 min does not support the FTP
  setting (no recent hard 20 min, or FTP set high) -> threshold / sweet-spot extension.
- ``durability`` — late/fresh EF ratio < 0.95 on 1 000+ kJ rides -> long rides with late
  sweet-spot / tempo.
- ``grey_zone`` — mean mid-zone share > 30 % over the last 4 weeks -> keep Z2 easy.
- ``aerobic_volume`` — CTL below ``ctl_floor`` (default 55) for a 300 W goal -> volume first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cyp.core.explain import Explanation, MethodRef, Reason

LIMITERS_VERSION = "limiters_v1"
VO2_RATIO_MIN = 1.12
SUSTAINED_RATIO_MIN = 1.02
DURABILITY_RATIO_MIN = 0.95
GREY_ZONE_MAX = 0.30
CTL_FLOOR = 55.0


@dataclass
class Limiter:
    """A detected limiter with its planner bias."""

    id: str
    severity: float
    title_zh: str
    template_bias: dict[str, float] = field(default_factory=dict)
    explanation: Explanation | None = None


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
) -> list[Limiter]:
    """Evaluate all rules; result sorted by severity (highest first)."""
    out: list[Limiter] = []
    p5, p20 = mmp.get(300), mmp.get(1200)
    if p5 and ftp > 0 and p5 / ftp < VO2_RATIO_MIN:
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
                        )
                    ],
                    {"vo2_ratio_min": VO2_RATIO_MIN},
                ),
            )
        )
    if p20 and ftp > 0 and p20 / ftp < SUSTAINED_RATIO_MIN:
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
                        )
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


def combined_bias(limiters: list[Limiter]) -> dict[str, float]:
    """Severity-weighted product of every limiter's bias: ``1 + sev*(b-1)`` per intent."""
    bias: dict[str, float] = {}
    for lim in limiters:
        for intent, b in lim.template_bias.items():
            bias[intent] = bias.get(intent, 1.0) * (1.0 + lim.severity * (b - 1.0))
    return {k: round(v, 3) for k, v in sorted(bias.items())}
