"""Goal projection: robust trend, minimum-data rules, never a fake date."""

from __future__ import annotations

import datetime as dt

from cyp.analysis.longitudinal import goal

AS_OF = dt.date(2026, 10, 14)


def series(f: object, days: int = 56) -> dict[dt.date, float]:
    return {AS_OF - dt.timedelta(days=days - 1 - i): f(i) for i in range(days)}  # type: ignore[operator]


def test_steady_rise_projects_a_date() -> None:
    p = goal.project_goal(series(lambda i: 240 + 0.3 * i), as_of=AS_OF, target_w=285)
    assert p.slope_w_per_week == 2.1 and p.kendall_tau == 1.0
    assert p.level_w is not None and abs(p.level_w - 256.0) < 1.5
    assert p.projected_date is not None and p.reason is None
    expected = AS_OF + dt.timedelta(days=round((285 - p.level_w) / 2.1 * 7))
    assert abs((p.projected_date - expected).days) <= 1


def test_too_few_weeks_gives_no_date() -> None:
    p = goal.project_goal(series(lambda i: 240 + i, days=20), as_of=AS_OF, target_w=285)
    assert p.projected_date is None and p.reason == "too_few_weeks"


def test_single_step_is_not_a_trend() -> None:
    # Real-data shape: flat for six weeks, then one step up.
    p = goal.project_goal(series(lambda i: 199.0 if i < 42 else 216.0), as_of=AS_OF, target_w=300)
    assert p.projected_date is None
    assert p.reason in {"inconsistent", "flat_or_falling"}


def test_flat_or_falling() -> None:
    p = goal.project_goal(series(lambda i: 260 - 0.1 * i), as_of=AS_OF, target_w=285)
    assert p.projected_date is None and p.reason == "flat_or_falling"


def test_estimates_far_below_the_setting_give_no_date() -> None:
    p = goal.project_goal(series(lambda i: 200 + 0.3 * i), as_of=AS_OF, target_w=300, setting_w=265)
    assert p.projected_date is None and p.reason == "below_setting"


def test_too_far_away() -> None:
    p = goal.project_goal(series(lambda i: 200 + 0.05 * i), as_of=AS_OF, target_w=300)
    assert p.projected_date is None and p.reason == "too_far"


def test_target_already_reached() -> None:
    p = goal.project_goal(series(lambda i: 290 + 0.1 * i), as_of=AS_OF, target_w=285)
    assert p.projected_date == AS_OF


def test_kendall_tau_and_theil_sen() -> None:
    assert goal.kendall_tau([1, 2, 3]) == 1.0
    assert goal.kendall_tau([3, 2, 1]) == -1.0
    assert goal.kendall_tau([1]) is None
    fit = goal.theil_sen([(x, float(x)) for x in range(7)] + [(7, 50.0)])
    assert fit is not None and fit[0] == 1.0  # one outlier does not move the slope
