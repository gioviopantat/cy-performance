"""Build the per-ride :class:`Explanation` (docs/07 §1) from a :class:`RideMetrics`.

Every number that appears in ``headline_zh`` / ``text_zh`` is also present (with the same
rounding) in the ``evidence`` dict of its reason, so renderers never invent figures. Model ids
match ``docs/glossary/*.md`` file names.
"""

from __future__ import annotations

from typing import Any

from cyp.analysis.ride.classify import CLASS_ZH
from cyp.analysis.ride.result import RideInputs, RideMetrics
from cyp.core.explain import Explanation, MethodRef, Reason

GLOSSARY = {
    "coggan_np_if_tss": "docs/glossary/coggan_np_if_tss.md",
    "decoupling_hr_lag": "docs/glossary/decoupling_hr_lag.md",
    "efficiency_factor": "docs/glossary/efficiency_factor.md",
    "cp_wprime": "docs/glossary/cp_wprime.md",
    "time_in_zone_tid": "docs/glossary/time_in_zone_tid.md",
}

STATUS_ZH = {
    "FRESH": "狀態新鮮",
    "NORMAL": "狀態正常",
    "BLUNTED": "心率反應鈍化",
    "OVERREACHED": "負荷過頭",
    "SICK": "生病",
}
NEXT_ZH = {
    "REST": "休息",
    "EASY": "輕鬆騎",
    "AS_PLANNED": "照表操課",
    "UPGRADE": "可以加碼",
}


def explain_ride(metrics: RideMetrics, inputs: RideInputs) -> Explanation:
    """Explanation keyed ``ride:{activity_id}`` summarising what, why and how."""
    because: list[Reason] = []
    terms: list[str] = []
    th = inputs.thresholds

    class_zh = CLASS_ZH.get(metrics.classification, metrics.classification)
    status_zh = STATUS_ZH.get(metrics.status, metrics.status)
    next_zh = NEXT_ZH.get(metrics.next_recommendation, metrics.next_recommendation)

    # --- load (NP / IF / TSS) -------------------------------------------------------------
    minutes = round(metrics.moving_s / 60)
    if metrics.tss_source == "power" and metrics.np_w is not None and metrics.tss is not None:
        ev: dict[str, Any] = {
            "np_w": round(metrics.np_w),
            "ftp_w": round(inputs.ftp) if inputs.ftp else None,
            "if": round(metrics.if_, 2) if metrics.if_ is not None else None,
            "moving_min": minutes,
            "tss": round(metrics.tss),
            "vi": round(metrics.vi, 2) if metrics.vi is not None else None,
        }
        because.append(
            Reason(
                text_zh=(
                    f"NP {ev['np_w']} W ÷ FTP {ev['ftp_w']} W = IF {ev['if']}；"
                    f"移動 {ev['moving_min']} 分鐘 → TSS {ev['tss']}（VI {ev['vi']}）"
                ),
                evidence=ev,
            )
        )
        terms.append("coggan_np_if_tss")
    elif metrics.tss_source == "hr" and metrics.tss is not None:
        ev = {"hr_tss": round(metrics.tss), "moving_min": minutes}
        because.append(
            Reason(
                text_zh=(
                    f"無功率計：以心率 TRIMP 估算 hrTSS {ev['hr_tss']}"
                    f"（移動 {ev['moving_min']} 分鐘）"
                ),
                evidence=ev,
            )
        )
        terms.append("coggan_np_if_tss")
    elif metrics.tss_source == "estimated" and metrics.tss is not None:
        ev = {
            "np_est_w": round(metrics.np_w) if metrics.np_w is not None else None,
            "tss_est": round(metrics.tss),
            "moving_min": minutes,
        }
        because.append(
            Reason(
                text_zh=(
                    f"無功率計與心率：以物理模型估算 NP {ev['np_est_w']} W、"
                    f"TSS {ev['tss_est']}（移動 {ev['moving_min']} 分鐘，低信心）"
                ),
                evidence=ev,
            )
        )
        terms.append("coggan_np_if_tss")

    # --- time in zone ---------------------------------------------------------------------
    tiz = metrics.time_in_zone_power
    if tiz:
        low = round((tiz.get("Z1", 0) + tiz.get("Z2", 0)) / 60)
        mid = round((tiz.get("Z3", 0) + tiz.get("Z4", 0)) / 60)
        high = round(sum(v for k, v in tiz.items() if k not in {"Z1", "Z2", "Z3", "Z4"}) / 60)
        ev = {"low_min": low, "mid_min": mid, "high_min": high}
        because.append(
            Reason(
                text_zh=f"低/中/高強度時間 {low}/{mid}/{high} 分鐘 → 判定為{class_zh}",
                evidence=ev,
            )
        )
        terms.append("time_in_zone_tid")

    # --- decoupling -----------------------------------------------------------------------
    detail = metrics.hr_drift_detail
    if detail and metrics.decoupling_pct is not None:
        ev = {
            "ef_first": round(detail["ef_first"], 2),
            "ef_second": round(detail["ef_second"], 2),
            "decoupling_pct": round(metrics.decoupling_pct, 1),
            "reliable": bool(detail.get("reliable")),
            "threshold_pct": th.easy_min_decoupling,
        }
        note = "" if ev["reliable"] else "；此趟太短或不夠穩定，僅供參考"
        because.append(
            Reason(
                text_zh=(
                    f"前半 EF {ev['ef_first']} → 後半 EF {ev['ef_second']}，"
                    f"有氧解耦 {ev['decoupling_pct']}%（警戒線 {ev['threshold_pct']}%）{note}"
                ),
                evidence=ev,
            )
        )
        terms.append("decoupling_hr_lag")

    # --- HR lag ---------------------------------------------------------------------------
    if metrics.hr_lag_s is not None:
        ev = {
            "hr_lag_s": round(metrics.hr_lag_s),
            "corr": round(metrics.hr_lag_corr, 2) if metrics.hr_lag_corr is not None else None,
            "blunted_s": round(th.blunted_min_lag_s),
        }
        because.append(
            Reason(
                text_zh=(
                    f"心率在功率變化後約 {ev['hr_lag_s']} 秒跟上（相關 {ev['corr']}；"
                    f"≥ {ev['blunted_s']} 秒視為鈍化）"
                ),
                evidence=ev,
            )
        )
        if "decoupling_hr_lag" not in terms:
            terms.append("decoupling_hr_lag")

    # --- EF -------------------------------------------------------------------------------
    if metrics.ef is not None and metrics.np_w is not None and metrics.avg_hr is not None:
        ev = {
            "np_w": round(metrics.np_w),
            "avg_hr": round(metrics.avg_hr),
            "ef": round(metrics.ef, 2),
        }
        because.append(
            Reason(
                text_zh=f"效率因子 EF = NP {ev['np_w']} ÷ 平均心率 {ev['avg_hr']} = {ev['ef']}",
                evidence=ev,
            )
        )
        terms.append("efficiency_factor")

    # --- W'bal ----------------------------------------------------------------------------
    if metrics.wbal_min_j is not None and inputs.cp and inputs.w_prime:
        ev = {
            "wbal_min_kj": round(metrics.wbal_min_j / 1000, 1),
            "cp_w": round(inputs.cp),
            "w_prime_kj": round(inputs.w_prime / 1000, 1),
        }
        because.append(
            Reason(
                text_zh=(
                    f"W' 電池最低剩 {ev['wbal_min_kj']} kJ"
                    f"（CP {ev['cp_w']} W、W' {ev['w_prime_kj']} kJ）"
                ),
                evidence=ev,
            )
        )
        terms.append("cp_wprime")

    # --- climbs ---------------------------------------------------------------------------
    if metrics.climbs:
        best = max(metrics.climbs, key=lambda c: c.get("vam_m_h") or 0)
        ev = {
            "climbs": len(metrics.climbs),
            "best_vam": round(best["vam_m_h"]) if best.get("vam_m_h") else None,
            "best_gain_m": round(best["gain_m"]),
        }
        because.append(
            Reason(
                text_zh=(
                    f"偵測到 {ev['climbs']} 段爬坡，最佳 VAM {ev['best_vam']} m/h"
                    f"（爬升 {ev['best_gain_m']} m）"
                ),
                evidence=ev,
            )
        )

    # --- headline / method / confidence ---------------------------------------------------
    head_ev: dict[str, Any] = {}
    if metrics.if_ is not None and metrics.tss is not None:
        head_ev = {"if": round(metrics.if_, 2), "tss": round(metrics.tss)}
        headline = (
            f"這趟是{class_zh}（IF {head_ev['if']}、TSS {head_ev['tss']}），"
            f"{status_zh}，明天建議{next_zh}。"
        )
    elif metrics.tss is not None:
        head_ev = {"tss": round(metrics.tss)}
        headline = (
            f"這趟是{class_zh}（估算 TSS {head_ev['tss']}），{status_zh}，明天建議{next_zh}。"
        )
    else:
        headline = f"這趟是{class_zh}，{status_zh}，明天建議{next_zh}。"
    if head_ev:
        because.insert(
            0,
            Reason(
                text_zh=f"狀態判定：{status_zh} → {next_zh}",
                evidence={**head_ev, "status": metrics.status, "next": metrics.next_recommendation},
            ),
        )

    primary = "coggan_np_if_tss"
    method = MethodRef(
        model_id=primary,
        version=metrics.algo_version,
        inputs={
            "tss_source": metrics.tss_source,
            "ftp_w": inputs.ftp,
            "ftp_source": inputs.ftp_source,
            "weight_kg": inputs.weight_kg,
            "moving_s": metrics.moving_s,
            "zones_source": inputs.zones_source,
            "thresholds": th.as_dict(),
        },
        doc=GLOSSARY[primary],
    )

    if metrics.tss_source == "power" and inputs.has_hr and metrics.moving_s >= 3600:
        confidence = "high"
    elif metrics.tss_source == "power":
        confidence = "medium"
    else:
        confidence = "low"

    return Explanation(
        key=f"ride:{metrics.activity_id}",
        headline_zh=headline,
        because=because,
        method=method,
        confidence=confidence,
        glossary_terms=terms,
    )
