"""Goal menus, preferences, variety and the post-ride feeling (spec personalized-planning)."""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

from cyp.analysis.longitudinal.pmc import PMCState, simulate
from cyp.analysis.readiness import (
    ReadinessInputs,
    YesterdayRide,
    compute_readiness,
    expected_rpe,
)
from cyp.planning import menus
from cyp.planning.athlete_rules import effective, is_senior
from cyp.planning.guardrails import check
from cyp.planning.planner import DayPlan, PlanContext, plan_week
from cyp.planning.season import SeasonWeek, build_skeleton, week_targets
from cyp.planning.templates import Template
from cyp.settings import AthleteConfig
from tests.planning.test_athlete_rules import TODAY, _cfg, _distance_cfg
from tests.planning.test_season_planner import _gi


def _on(cfg: AthleteConfig, **planner: Any) -> AthleteConfig:
    """Flags on. (Helpers rebuild configs via model_dump, which marks every field as written,
    so the switches are forced here; real athlete.yaml files only mark what they contain.)"""
    out, _ = effective(cfg, TODAY, goal_menus=True, variety=True)
    update = {"menus": "goal", "variety": True, **planner}
    return out.model_copy(update={"planner": out.planner.model_copy(update=update)})


def _weeks(
    cfg: AthleteConfig, library: dict[str, Template], weeks: int = 26, *, masters: bool = False
) -> list[tuple[SeasonWeek, list[DayPlan]]]:
    sk = build_skeleton(cfg)
    ctx = PlanContext(cfg=cfg, library=library, goal_date=sk.goal_date, masters=masters)
    ctl = atl = 55.0
    prev = None
    out: list[tuple[SeasonWeek, list[DayPlan]]] = []
    for w in sk.weeks[:weeks]:
        t = week_targets(w, ctl, weekly_max_minutes=900, prev_loading_tss=prev, atl_start=atl)
        wk = plan_week(w, t, ctx)
        out.append((w, wk))
        end = simulate(PMCState(w.start - dt.timedelta(days=1), ctl, atl), [d.tss for d in wk])[-1]
        ctl, atl = end.ctl, end.atl
        if not w.recovery and w.phase != "test":
            prev = t.target_tss
    return out


def _season(cfg: AthleteConfig, library: dict[str, Template], weeks: int = 26) -> list[DayPlan]:
    return [d for _, wk in _weeks(cfg, library, weeks) for d in wk]


def _long(wk: list[DayPlan]) -> DayPlan | None:
    return next((d for d in wk if d.role == "long_ride"), None)


def test_every_menu_template_exists_and_fits_its_phase(library: dict[str, Template]) -> None:
    for emph, phases in menus.HIT_MENUS.items():
        for phase, slots in phases.items():
            for stages in slots:
                for stage in stages:
                    usable = [t for t in stage if phase in library[t].phases or phase == "test"]
                    assert usable, f"{emph}/{phase}: no usable template in {stage}"
    for tid in (*menus.LONG_STEADY, *menus.LONG_FINISH, *menus.LIGHT_HIT):
        assert tid in library


def test_endurance_emphasis_changes_the_kind_of_work(library: dict[str, Template]) -> None:
    ftp = [d for d in _season(_on(_cfg()), library, 8) if d.role in ("hit", "long_ride")]
    endu = [d for d in _season(_on(_distance_cfg()), library, 8) if d.role in ("hit", "long_ride")]
    endu_ids = {d.template_id for d in endu}
    assert endu_ids & {"tempo_3x15", "climb_tempo_repeats", "tempo_with_surges", "low_cadence_sfr"}
    assert endu_ids & set(menus.LONG_STEADY)
    pairs = list(zip(ftp, endu, strict=False))
    assert sum(a.template_id != b.template_id for a, b in pairs) >= len(pairs) / 2


def test_variety_rotates_between_weeks(library: dict[str, Template]) -> None:
    hits = [d.template_id for d in _season(_on(_distance_cfg()), library, 8) if d.role == "hit"]
    assert len(set(hits)) >= 3


def test_hit_days_preference(library: dict[str, Template]) -> None:
    cfg = _on(_cfg(), hit_days=["thu"])
    first = [d for d in _season(cfg, library, 1) if d.role == "hit"]
    assert first and first[0].date.strftime("%a") == "Thu"
    # Part of the goal menus: with plan.goal_menus off the default order (Tue first) stays.
    off = cfg.model_copy(update={"planner": cfg.planner.model_copy(update={"menus": "phase"})})
    first_off = [d for d in _season(off, library, 1) if d.role == "hit"]
    assert first_off and first_off[0].date.strftime("%a") == "Tue"


def test_intensity_preference_shifts_alternatives(library: dict[str, Template]) -> None:
    easy = _season(_on(_cfg(), intensity="easy"), library, 16)
    hard = _season(_on(_cfg(), intensity="hard"), library, 16)
    intents = lambda days: [library[d.template_id].intent for d in days if d.role == "hit"]  # noqa: E731
    assert intents(easy).count("vo2") <= intents(hard).count("vo2")


def test_masters_get_a_lighter_second_hard_day(library: dict[str, Template]) -> None:
    cfg = _on(_cfg(athlete__birth_year=1950), hit_days=["tue", "thu"])
    sk = build_skeleton(cfg)
    build = next(w for w in sk.weeks if w.phase == "build" and not w.recovery)
    targets = week_targets(build, 60.0, weekly_max_minutes=900, prev_loading_tss=650.0)
    for masters in (True, False):
        ctx = PlanContext(cfg=cfg, library=library, goal_date=sk.goal_date, masters=masters)
        hits = [d for d in plan_week(build, targets, ctx) if d.role == "hit"]
        assert len(hits) == 2
        assert (hits[1].template_id in menus.LIGHT_HIT) is masters
        assert any("48 小時" in n for n in hits[1].notes) is masters


def test_age_answers_reach_the_planner_only_with_athlete_rules() -> None:
    raw = _cfg(athlete__birth_year=1950)
    assert is_senior(effective(raw, TODAY)[0], TODAY)
    assert not is_senior(effective(raw, TODAY, athlete_rules=False)[0], TODAY)


def test_long_ride_grows_gently_and_reaches_the_ceiling(library: dict[str, Template]) -> None:
    cfg = _on(_distance_cfg(athlete__birth_year=1950, season__load_pattern=None))
    loading = [(w, _long(wk)) for w, wk in _weeks(cfg, library, masters=True) if w.phase != "test"]
    planned = [(w, d.minutes) for w, d in loading if d is not None and not w.recovery]
    ceiling = cfg.availability.long_ride_max_minutes
    assert ceiling is not None
    assert all(m == w.long_ride_minutes for w, m in planned)  # the progression is really planned
    assert max(m for w, m in planned if not w.over_distance) == ceiling
    for (_, a), (_, b) in itertools.pairwise(planned):
        assert b - a <= 60, (a, b)


def test_over_distance_ride_survives_rotation(library: dict[str, Template]) -> None:
    cfg = _on(_distance_cfg())
    for w, wk in _weeks(cfg, library):
        d = _long(wk)
        if not w.over_distance or d is None:
            continue
        assert d.template_id == "long_ride_distance" and d.minutes == w.long_ride_minutes
        assert any(f"約 {d.minutes} 分" in n for n in d.notes)


def test_finish_weeks_get_a_finish_ride(library: dict[str, Template]) -> None:
    # Distance goal without a progression ceiling: menu long rides, finish every other week.
    cfg = _on(_distance_cfg(availability__long_ride_max_minutes=None))
    for w, wk in _weeks(cfg, library, 22):
        d = _long(wk)
        if d is None or w.recovery or w.phase == "test":
            continue
        group = menus.LONG_STEADY if w.index % 2 else menus.LONG_FINISH
        assert d.template_id in group, (w.index, d.template_id)


def test_weekly_load_close_to_flags_off(library: dict[str, Template]) -> None:
    raw = _distance_cfg(season__load_pattern=None)
    on, off = _on(raw), effective(raw, TODAY)[0]
    for (w, a), (_, b) in zip(_weeks(on, library), _weeks(off, library), strict=True):
        if w.recovery or w.phase == "test":
            continue
        ta, tb = sum(d.tss for d in a), sum(d.tss for d in b)
        assert abs(ta - tb) / tb <= 0.10, (w.index, round(ta), round(tb))


def test_past_distance_goal_no_longer_drives_emphasis() -> None:
    cfg = _distance_cfg()
    goal = next(g for g in cfg.goals if g.kind == "distance")
    assert menus.emphasis(cfg, goal.date) == "endurance"
    assert menus.emphasis(cfg, goal.date + dt.timedelta(days=1)) == "ftp"


def test_personalized_season_is_guardrail_clean(library: dict[str, Template]) -> None:
    for cfg in (_on(_cfg()), _on(_distance_cfg(athlete__birth_year=1950))):
        days = _season(cfg, library)
        sk = build_skeleton(cfg)
        assert check(days, _gi(cfg, PMCState(sk.start - dt.timedelta(days=1), 55.0, 55.0))) == []
        hard = [d.date for d in days if d.is_hard]
        assert all((b - a).days >= 2 for a, b in itertools.pairwise(hard))


def _ready(y: YesterdayRide) -> Any:
    return compute_readiness(ReadinessInputs(date=TODAY, today=None, tsb=0.0, yesterday=y))


def test_hard_feeling_easy_ride_caps_today() -> None:
    for y in (YesterdayRide(classification="endurance", rpe=8), YesterdayRide(feel=5)):
        r = _ready(y)
        assert r.recommendation in ("EASY", "REST")  # never a hard session the day after
        assert any("感受很差" in b.text_zh for b in r.explanation.because)
    calm = _ready(YesterdayRide(classification="vo2", rpe=6, feel=1))
    assert calm.recommendation in ("AS_PLANNED", "UPGRADE")


def test_reason_names_rpe_only_when_it_triggered() -> None:
    r = _ready(YesterdayRide(classification="vo2", rpe=7, feel=5))
    text = next(b.text_zh for b in r.explanation.because if "感受很差" in b.text_zh)
    assert "RPE" not in text and "5/5" in text


def test_long_rides_may_feel_harder() -> None:
    long_z2 = YesterdayRide(classification="endurance", rpe=6, rated_minutes=300)
    assert expected_rpe(long_z2) == 5.0  # 3 + 2 h beyond 3 h
    assert not any("感受很差" in b.text_zh for b in _ready(long_z2).explanation.because)
    short = YesterdayRide(classification="endurance", rpe=6, rated_minutes=60)
    assert any("感受很差" in b.text_zh for b in _ready(short).explanation.because)


def test_rpe_is_judged_against_the_rated_ride() -> None:
    # Hardest ride of the day was VO2, but the RPE belongs to the long Z2 ride.
    y = YesterdayRide(classification="vo2", rated_classification="endurance", rpe=7)
    assert expected_rpe(y) == 3.0
    assert _ready(y).recommendation in ("EASY", "REST")
