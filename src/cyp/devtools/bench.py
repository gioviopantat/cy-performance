"""Wall-clock benchmarks of the recompute paths the UI will trigger (``cyp dev bench``)."""

from __future__ import annotations

import datetime as dt
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cyp.settings import AthleteConfig
from cyp.store.streams import StreamStore


def _time(fn: Callable[[], Any], repeat: int) -> dict[str, float]:
    runs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - t0) * 1000)
    return {
        "first_ms": round(runs[0], 1),
        "median_ms": round(statistics.median(runs), 1),
        "min_ms": round(min(runs), 1),
    }


def run_bench(
    factory: sessionmaker[Session],
    store: StreamStore,
    cfg: AthleteConfig,
    *,
    today: dt.date,
    reports_dir: Path,
    repeat: int = 5,
) -> dict[str, dict[str, float]]:
    """Time trends, FTP, readiness and planning on the current store."""
    from cyp.analysis.longitudinal.run import build_trends
    from cyp.analysis.readiness_job import run_readiness
    from cyp.dataset import CACHE
    from cyp.planning.job import build_plan, plan_horizon
    from cyp.services.ftp import recompute as recompute_ftp

    now = dt.datetime.combine(today, dt.time(7, 0))
    out: dict[str, dict[str, float]] = {}
    out["trends"] = _time(
        lambda: build_trends(factory, store, as_of=today, reports_dir=reports_dir), repeat
    )
    out["readiness_7d"] = _time(
        lambda: run_readiness(factory, [today - dt.timedelta(days=i) for i in range(7)]), repeat
    )
    out["plan_14d"] = _time(
        lambda: build_plan(factory, cfg, today=today, now_local=now, persist=False), repeat
    )
    out["ftp"] = _time(lambda: recompute_ftp(factory, as_of=today), repeat)
    out["plan_preview_warm"] = _time(
        lambda: plan_horizon(CACHE.get(factory), cfg, today=today, now_local=now),  # type: ignore[arg-type]
        repeat,
    )
    return out
