"""Adaptive inputs per profile (services/adaptive.py): flags on/off, learned_params, golden.

With ``plan.personal_scales`` and ``plan.progression_gate`` off the planner and readiness are
exactly the pre-adaptive ones (spec adaptive-planning: golden plan unchanged with flags off).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from cyp.analysis.personal_scales import Learned
from cyp.planning import gate as gate_mod
from cyp.planning.gate import GateDecision, gate_chain
from cyp.planning.job import Adaptive, PlanOverrides, plan_horizon
from cyp.planning.season import build_skeleton
from cyp.services import adaptive
from cyp.services import readiness as readiness_service
from cyp.services.context import AppContext
from cyp.store.models import LearnedParam
from tests.api.conftest import NOW

TODAY = NOW.date()
OFF = "notify.macos=off,plan.personal_scales=off,plan.progression_gate=off"


def _flags(ctx: AppContext, value: str) -> None:
    ctx.settings = ctx.settings.model_copy(update={"cyp_features": value})


def _days(run: Any) -> list[tuple[Any, ...]]:
    return [(d.date, d.template_id, dict(d.params), round(d.tss, 3)) for d in run.days]


def test_flags_off_give_the_plain_planner(ctx: AppContext) -> None:
    _flags(ctx, OFF)
    assert adaptive.scales_for(ctx, TODAY) is None
    assert adaptive.plan_inputs(ctx, TODAY) == Adaptive()
    assert adaptive.record(ctx, TODAY) == 0
    ds, cfg = ctx.dataset(), ctx.athlete_config()
    plain = plan_horizon(ds, cfg, today=TODAY, now_local=NOW)
    off = plan_horizon(
        ds, cfg, today=TODAY, now_local=NOW, adaptive=adaptive.plan_inputs(ctx, TODAY, ds)
    )
    assert _days(off) == _days(plain)
    assert [e.key for e in off.adaptations] == [e.key for e in plain.adaptations]


def test_scales_on_record_learned_params_and_yaml_wins(ctx: AppContext) -> None:
    _flags(ctx, "notify.macos=off")  # registry default: personal scales on
    scales = adaptive.scales_for(ctx, TODAY)
    assert scales is not None and scales.easy_day_tss is not None
    assert adaptive.plan_inputs(ctx, TODAY).easy_day == scales.easy_day_tss
    n = adaptive.record(ctx, TODAY)
    with ctx.factory() as s:
        rows = {r.param: r for r in s.scalars(select(LearnedParam))}
    assert n == len(rows) and {"feel_baseline", "easy_day_tss", "rpe_offset.all"} <= set(rows)
    assert all(r.as_of == TODAY and r.algo_version == "personal_scales_v1" for r in rows.values())
    # Written values win over learned ones; the next morning updates the same rows.
    path = Path(ctx.athlete_config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw.setdefault("planner", {})["scales"] = {"easy_day_tss": 70, "feel_baseline": 2}
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    assert adaptive.record(ctx, TODAY + dt.timedelta(days=1)) == n
    with ctx.factory() as s:
        easy = s.get(LearnedParam, (ctx.dataset().athlete_id, "easy_day_tss"))
        assert easy is not None and (easy.value, easy.source) == (70.0, "yaml")
        assert len(s.scalars(select(LearnedParam)).all()) == n


def test_readiness_uses_scales_only_when_on(ctx: AppContext) -> None:
    _flags(ctx, OFF)
    assert readiness_service._scales(ctx, TODAY, ctx.dataset()) == {}
    _flags(ctx, "notify.macos=off")
    got = readiness_service._scales(ctx, TODAY, ctx.dataset())
    assert set(got) == {"rpe_offset", "feel_baseline"} and "all" in got["rpe_offset"]


def test_horizon_changes_only_widened_or_repaired_days(ctx: AppContext) -> None:
    ds, cfg = ctx.dataset(), ctx.athlete_config()
    habit = Adaptive(easy_day=Learned(66.0, 10, "learned", 80.0))
    plain = {d.date: d for d in plan_horizon(ds, cfg, today=TODAY, now_local=NOW).days}
    run = plan_horizon(ds, cfg, today=TODAY, now_local=NOW, adaptive=habit)
    explained = {e.key.rsplit(".", 1)[-1] for e in run.adaptations} | {
        r.date.isoformat() for r in run.repairs
    }
    for d in run.days:
        if d.template_id != plain[d.date].template_id:
            assert d.date.isoformat() in explained
    easy_today = plan_horizon(
        ds, cfg, today=TODAY, now_local=NOW, adaptive=habit,
        overrides=PlanOverrides(readiness="EASY"),
    )  # fmt: skip
    assert not any(
        e.key == f"plan.learn.easy_day_tss.{TODAY.isoformat()}" for e in easy_today.adaptations
    )


# ------------------------------------------------------------------- progression gate


def _early_season(ctx: AppContext, start: dt.date = dt.date(2026, 9, 7)) -> None:
    path = Path(ctx.athlete_config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["season"]["start"] = start
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_gate_flag_off_by_default_and_on_by_choice(ctx: AppContext) -> None:
    _early_season(ctx)
    _flags(ctx, "notify.macos=off")
    assert not adaptive.plan_inputs(ctx, TODAY).gate
    assert adaptive.gate_decision(ctx, dt.date(2026, 10, 5)) is None
    _flags(ctx, "notify.macos=off,plan.progression_gate=on")
    inputs = adaptive.plan_inputs(ctx, TODAY)
    assert inputs.gate and inputs.rpe_offset  # reads the personal offsets
    g = adaptive.gate_decision(ctx, dt.date(2026, 10, 5))
    assert g is not None and g.explanation is not None
    assert g.explanation.key == "plan.gate.2026-10-05"
    # Deterministic, and never above the season's index plus the extra steps taken.
    ds, cfg = ctx.dataset(), ctx.athlete_config()
    sk = build_skeleton(cfg)
    a = gate_chain(ds, sk, cfg.availability.weekly_max_minutes, until=TODAY)
    b = gate_chain(ds, sk, cfg.availability.weekly_max_minutes, until=TODAY)
    assert [(k, v.decision, v.progression_index) for k, v in a.items()] == [
        (k, v.decision, v.progression_index) for k, v in b.items()
    ]
    assert list(a) == [w.start for w in sk.weeks[1:] if w.start <= TODAY]
    assert all(1 <= g.progression_index <= g.season_index + g.extras_used for g in a.values())


def test_horizon_applies_a_down_decision(ctx: AppContext, monkeypatch: Any) -> None:
    _early_season(ctx)
    ds, cfg = ctx.dataset(), ctx.athlete_config()
    plain = plan_horizon(ds, cfg, today=TODAY, now_local=NOW)
    week = TODAY - dt.timedelta(days=TODAY.weekday())
    season_week = next(w for w in build_skeleton(cfg).weeks if w.start == week)

    def down(*_a: Any, **_k: Any) -> dict[dt.date, Any]:
        return {
            week: gate_mod._apply(
                season_week,
                "down",
                [],
                gate_mod.WeekEvidence(start=week - dt.timedelta(days=7)),
                GateDecision(week, "up", season_week.progression_index, 1),
                0,
            )
        }

    monkeypatch.setattr(gate_mod, "gate_chain", down)
    run = plan_horizon(ds, cfg, today=TODAY, now_local=NOW, adaptive=Adaptive(gate=True))
    assert run.week_targets[week].target_tss == pytest.approx(
        plain.week_targets[week].target_tss * gate_mod.DOWN_TSS_FACTOR, abs=0.2
    )
    assert any(e.key == f"plan.gate.{week.isoformat()}" for e in run.adaptations)
    assert run.gate[week].decision == "down"
