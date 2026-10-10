"""Durability: efficiency at matched intensity as work accumulates (docs/04 §3).

Per ride (:func:`ef_by_kj`): cumulative mechanical work (kJ) is integrated from 1 Hz power; the
moving, pedalling seconds with HR whose 30 s power sits inside the *endurance band*
(``ENDURANCE_BAND`` × FTP) are grouped into kJ buckets (``KJ_BUCKETS``). Per bucket
``EF = mean(P) / mean(HR)`` when it holds at least ``MIN_BUCKET_S`` seconds. The first
``WARMUP_S`` seconds are skipped (HR still rising).

Durability ratio = EF in the latest bucket at or beyond 1 000 kJ / EF in the first bucket.
1.00 = no fade; 0.95 = HR 5 % higher for the same power after a big day. This is the
"GC-rider" number: how much of your fresh self is left after 1 000+ kJ.

Trend (:func:`durability_trend`): per 28-day block, median ratio and EF-at-1000 kJ.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field

import numpy as np

from cyp.analysis.ride.frames import RideFrame, rolling_mean
from cyp.core.explain import Explanation, MethodRef, Reason

DURABILITY_VERSION = "durability_v1"
KJ_BUCKETS: tuple[int, ...] = (0, 500, 1000, 1500, 2000, 2500, 3000)
ENDURANCE_BAND = (0.55, 0.80)
MIN_BUCKET_S = 600
WARMUP_S = 600
LATE_KJ = 1000


def bucket_label(lo: int) -> str:
    """``"1000-1500"`` style label; the last bucket is open-ended (``"3000+"``)."""
    idx = KJ_BUCKETS.index(lo)
    return f"{lo}+" if idx == len(KJ_BUCKETS) - 1 else f"{lo}-{KJ_BUCKETS[idx + 1]}"


@dataclass
class RideDurability:
    """EF per kJ bucket for one ride."""

    activity_id: int
    date: dt.date
    total_kj: float
    ef_by_bucket: dict[str, float] = field(default_factory=dict)
    seconds_by_bucket: dict[str, int] = field(default_factory=dict)

    @property
    def ef_fresh(self) -> float | None:
        """EF in the first bucket with data."""
        for lo in KJ_BUCKETS:
            v = self.ef_by_bucket.get(bucket_label(lo))
            if v is not None:
                return v
        return None

    @property
    def ef_late(self) -> float | None:
        """EF in the deepest bucket at or beyond :data:`LATE_KJ`."""
        late = [
            self.ef_by_bucket[bucket_label(lo)]
            for lo in KJ_BUCKETS
            if lo >= LATE_KJ and bucket_label(lo) in self.ef_by_bucket
        ]
        return late[-1] if late else None

    @property
    def ratio(self) -> float | None:
        """``ef_late / ef_fresh``; ``None`` unless the ride reached :data:`LATE_KJ` with data."""
        fresh, late = self.ef_fresh, self.ef_late
        if fresh is None or late is None or fresh <= 0:
            return None
        return late / fresh


def ef_by_kj(
    frame: RideFrame, ftp: float, *, activity_id: int, date: dt.date
) -> RideDurability | None:
    """Per-bucket EF for a ride with measured power and HR; ``None`` if either is missing."""
    watts, hr = frame.watts, frame.hr
    if watts is None or hr is None or ftp <= 0:
        return None
    p = np.where(np.isfinite(watts), watts, 0.0)
    kj = np.cumsum(np.where(frame.recorded, p, 0.0)) / 1000.0
    p30 = rolling_mean(p, 30)
    lo, hi = ENDURANCE_BAND[0] * ftp, ENDURANCE_BAND[1] * ftp
    t = frame.t_s - frame.t_s[0]
    ok = (
        frame.moving
        & (p > 0)
        & np.isfinite(hr)
        & (hr > 0)
        & (p30 >= lo)
        & (p30 <= hi)
        & (t >= WARMUP_S)
    )
    out = RideDurability(
        activity_id=activity_id, date=date, total_kj=float(kj[-1]) if len(kj) else 0.0
    )
    edges = [*KJ_BUCKETS, float("inf")]
    for i, b_lo in enumerate(KJ_BUCKETS):
        mask = ok & (kj >= b_lo) & (kj < edges[i + 1])
        n = int(mask.sum())
        if n < MIN_BUCKET_S:
            continue
        label = bucket_label(b_lo)
        out.ef_by_bucket[label] = round(float(p[mask].mean() / hr[mask].mean()), 4)
        out.seconds_by_bucket[label] = n
    return out


def ride_durability_json(frame: RideFrame, ftp: float) -> dict[str, object] | None:
    """Per-ride durability blob for ``activity_metrics.durability`` (computed at analysis time)."""
    r = ef_by_kj(frame, ftp, activity_id=0, date=dt.date.min)
    if r is None:
        return None
    return {
        "version": DURABILITY_VERSION,
        "ftp": round(ftp, 1),
        "total_kj": round(r.total_kj, 1),
        "ef_by_bucket": r.ef_by_bucket,
        "seconds_by_bucket": r.seconds_by_bucket,
    }


def from_json(activity_id: int, date: dt.date, blob: object) -> RideDurability | None:
    """Inverse of :func:`ride_durability_json` (``None`` for missing / other versions)."""
    if not isinstance(blob, dict) or blob.get("version") != DURABILITY_VERSION:
        return None
    return RideDurability(
        activity_id=activity_id,
        date=date,
        total_kj=float(blob.get("total_kj") or 0.0),
        ef_by_bucket=dict(blob.get("ef_by_bucket") or {}),
        seconds_by_bucket=dict(blob.get("seconds_by_bucket") or {}),
    )


@dataclass
class DurabilityBlock:
    """Durability summary for one 28-day block."""

    start: dt.date
    end: dt.date
    n_rides: int
    n_long: int
    median_ratio: float | None
    median_ef_late: float | None


def durability_trend(
    rides: Iterable[RideDurability], *, end: dt.date, n_blocks: int = 6, block_days: int = 28
) -> list[DurabilityBlock]:
    """Median durability ratio per block, oldest first, ending on ``end``."""
    rides = list(rides)
    out: list[DurabilityBlock] = []
    for i in range(n_blocks - 1, -1, -1):
        b_end = end - dt.timedelta(days=i * block_days)
        b_start = b_end - dt.timedelta(days=block_days - 1)
        sel = [r for r in rides if b_start <= r.date <= b_end]
        ratios = [r.ratio for r in sel if r.ratio is not None]
        lates = [r.ef_late for r in sel if r.ef_late is not None]
        out.append(
            DurabilityBlock(
                start=b_start,
                end=b_end,
                n_rides=len(sel),
                n_long=len(ratios),
                median_ratio=round(statistics.median(ratios), 4) if ratios else None,
                median_ef_late=round(statistics.median(lates), 4) if lates else None,
            )
        )
    return out


def explain_durability(blocks: list[DurabilityBlock], *, as_of: dt.date) -> Explanation:
    """Explanation of the latest block vs the previous ones."""
    with_data = [b for b in blocks if b.median_ratio is not None]
    because: list[Reason] = []
    if not with_data:
        headline = "最近沒有超過 1000 kJ、且含心率與功率的長騎，無法評估耐久力"
        conf = "low"
    else:
        last = with_data[-1]
        fade = (1 - (last.median_ratio or 1)) * 100
        headline = (
            f"長騎超過 1000 kJ 後效率掉 {fade:.1f} %"
            f"（{last.end.isoformat()} 止 28 天，{last.n_long} 趟）"
        )
        because.append(
            Reason(
                text_zh=(
                    f"同樣耐力區功率下，騎完 1000 kJ 後的 EF 是剛開始的 "
                    f"{(last.median_ratio or 0) * 100:.1f} %"
                ),
                evidence={"median_ratio": last.median_ratio, "n_long": last.n_long},
            )
        )
        if len(with_data) >= 2:
            prev = with_data[-2]
            delta = ((last.median_ratio or 0) - (prev.median_ratio or 0)) * 100
            because.append(
                Reason(
                    text_zh=(
                        f"比前一個區塊 {'進步' if delta >= 0 else '退步'} {abs(delta):.1f} 個百分點"
                    ),
                    evidence={"prev_ratio": prev.median_ratio, "delta_pp": round(delta, 2)},
                )
            )
        conf = "high" if last.n_long >= 3 else "medium" if last.n_long >= 1 else "low"
    return Explanation(
        key="trends.durability",
        headline_zh=headline,
        because=because,
        method=MethodRef(
            model_id="durability_kj_buckets",
            version=DURABILITY_VERSION,
            inputs={
                "kj_buckets": list(KJ_BUCKETS),
                "endurance_band_ftp": list(ENDURANCE_BAND),
                "min_bucket_s": MIN_BUCKET_S,
                "warmup_s": WARMUP_S,
            },
            doc="docs/glossary/decoupling_hr_lag.md",
        ),
        confidence=conf,  # type: ignore[arg-type]
        glossary_terms=["efficiency_factor", "decoupling_hr_lag"],
    )
