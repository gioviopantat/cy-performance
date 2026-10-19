"""Outdoor context of one ride: stops, coasting, heat, and which numbers they make unreliable.

Section 4 of the per-ride report (docs/specs/ride-report.md). Pure: a :class:`RideFrame` in,
an :class:`OutdoorFacts` out. Each :class:`Caveat` names a number today's conditions discount
and carries the measured value next to its threshold, so the text never invents a figure
(docs/07).

- Stop: a run of not-moving seconds (``RideFrame.moving``: stationary *or* device paused) of at
  least :data:`STOP_MIN_S` between the first and the last moving second.
- Coasting: moving seconds with a power reading of :data:`COAST_MAX_W` (0 W), as a share of
  moving seconds that have a power reading (dropouts are excluded from both).
- Temperature: the head unit's ``temp_c`` stream over recorded seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cyp.analysis.ride.frames import RideFrame, runs

CONTEXT_VERSION = "outdoor_context_v1"

#: A stop is at least this long; shorter pauses are shifting, a slow corner or GPS jitter.
STOP_MIN_S = 15
#: Coasting = a moving second at or below this power (a reading of exactly 0 W).
COAST_MAX_W = 0.0
#: Absolute floor: below this average temperature heat is not what drives HR (thermoregulatory
#: load at the same power becomes material above roughly 25 °C).
HEAT_AVG_C = 25.0
#: Heat is named only when it is also this much above the athlete's usual (recent median ride
#: temperature): in a hot climate every ride is warm, and only *hotter than usual* makes today's
#: HR-based numbers (decoupling, EF, HR lag) incomparable with the athlete's own history.
HEAT_MARGIN_C = 3.0
#: Stops per moving hour from which interval structure and VI are dominated by traffic lights.
MANY_STOPS_PER_H = 4.0
#: Stopped minutes from which the same is true however few the stops were.
MANY_STOPPED_MIN = 15.0
#: Coasting share above which average power understates the work done while pedalling (NP and
#: average power diverge; compare pedalling minutes, not averages).
HIGH_COAST_PCT = 25.0


@dataclass
class Stop:
    """One stop, in seconds from the start of the ride."""

    start_s: int
    end_s: int

    @property
    def seconds(self) -> int:
        """Length of the stop."""
        return self.end_s - self.start_s


@dataclass
class OutdoorFacts:
    """What :func:`outdoor_context` measured."""

    stops: list[Stop] = field(default_factory=list)
    stopped_s: int = 0
    moving_s: int = 0
    coasting_pct: float | None = None
    temp_avg_c: float | None = None
    temp_max_c: float | None = None
    caveats: list[Caveat] = field(default_factory=list)

    @property
    def stops_per_h(self) -> float:
        """Stops per moving hour."""
        return len(self.stops) / (self.moving_s / 3600) if self.moving_s else 0.0


def find_stops(frame: RideFrame, *, min_s: int = STOP_MIN_S) -> list[Stop]:
    """Not-moving runs of at least ``min_s`` strictly inside the ride (start/end excluded)."""
    moving = frame.moving
    idx = np.flatnonzero(moving)
    if idx.size == 0:
        return []
    first, last = int(idx[0]), int(idx[-1])
    t = frame.t_s
    out = []
    for s, e in runs(~moving[first : last + 1]):
        lo, hi = first + s, first + e
        if hi - lo >= min_s:
            out.append(Stop(int(t[lo]), int(t[hi - 1]) + 1))
    return out


def coasting_pct(frame: RideFrame) -> float | None:
    """Share (%) of moving seconds with a power reading at :data:`COAST_MAX_W`."""
    w = frame.watts
    if w is None:
        return None
    ok = frame.moving & np.isfinite(w)
    n = int(ok.sum())
    if n == 0:
        return None
    return float(np.count_nonzero(w[ok] <= COAST_MAX_W) / n * 100.0)


def temperature(frame: RideFrame) -> tuple[float | None, float | None]:
    """(average, max) of the ``temp_c`` stream over recorded seconds."""
    temp = frame.col("temp_c")
    if temp is None:
        return None, None
    vals = temp[frame.recorded & np.isfinite(temp)]
    if vals.size == 0:
        return None, None
    return float(vals.mean()), float(vals.max())


@dataclass
class Caveat:
    """A number today's conditions make unreliable: a stable code plus the values quoted.

    The zh-TW sentence is rendered by ``reports`` from the code (one template per code), with
    exactly these numbers.
    """

    code: str
    evidence: dict[str, float | int]


def caveats(f: OutdoorFacts, baseline_temp_c: float | None = None) -> list[Caveat]:
    """Caveats that fire, each with the measured value and its threshold.

    ``baseline_temp_c`` is the athlete's usual ride temperature; without one only the absolute
    floor applies.
    """
    out: list[Caveat] = []
    t = f.temp_avg_c
    if t is not None and t >= HEAT_AVG_C:
        if baseline_temp_c is None:
            out.append(Caveat("heat", {"temp_avg_c": round(t, 1), "limit_c": HEAT_AVG_C}))
        elif t >= baseline_temp_c + HEAT_MARGIN_C:
            out.append(
                Caveat(
                    "heat_vs_usual",
                    {
                        "temp_avg_c": round(t, 1),
                        "usual_c": round(baseline_temp_c, 1),
                        "diff_c": round(t - baseline_temp_c, 1),
                        "margin_c": HEAT_MARGIN_C,
                    },
                )
            )
    stopped_min = f.stopped_s / 60
    if f.stops_per_h >= MANY_STOPS_PER_H or stopped_min >= MANY_STOPPED_MIN:
        out.append(
            Caveat(
                "stops",
                {
                    "stops_n": len(f.stops),
                    "stopped_min": round(stopped_min, 1),
                    "stops_per_h": round(f.stops_per_h, 1),
                    "limit_per_h": MANY_STOPS_PER_H,
                    "limit_min": MANY_STOPPED_MIN,
                },
            )
        )
    if f.coasting_pct is not None and f.coasting_pct >= HIGH_COAST_PCT:
        out.append(
            Caveat(
                "coasting", {"coasting_pct": round(f.coasting_pct, 1), "limit_pct": HIGH_COAST_PCT}
            )
        )
    return out


def outdoor_context(frame: RideFrame, baseline_temp_c: float | None = None) -> OutdoorFacts:
    """Stops, coasting, temperature and caveats of one outdoor ride."""
    stops = find_stops(frame)
    t_avg, t_max = temperature(frame)
    f = OutdoorFacts(
        stops=stops,
        stopped_s=sum(s.seconds for s in stops),
        moving_s=frame.moving_s,
        coasting_pct=coasting_pct(frame),
        temp_avg_c=t_avg,
        temp_max_c=t_max,
    )
    f.caveats = caveats(f, baseline_temp_c)
    return f
