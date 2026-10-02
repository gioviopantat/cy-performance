"""Workout template library: schema validation, expression grammar, resolve(), closed-form TSS."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cyp.planning.templates import (
    LIBRARY_DIR,
    ParamSpec,
    Repeat,
    ResolvedWorkout,
    Step,
    Template,
    TemplateError,
    closed_form_tss,
    evaluate,
    load_library,
    load_template,
    nominal_tss,
    param_grid,
    resolve,
)

README = LIBRARY_DIR / "README.md"


@pytest.fixture(scope="module")
def lib() -> dict[str, Template]:
    return load_library()


def _leaves(w: ResolvedWorkout) -> list[Step]:
    out: list[Step] = []
    for it in w.items:
        out.extend(it.steps if isinstance(it, Repeat) else (it,))
    return out


def _readme_table() -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    in_table = False
    for line in README.read_text(encoding="utf-8").splitlines():
        if line.startswith("## 9."):
            in_table = True
            continue
        if not in_table or not line.startswith("| ") or line.startswith("| id "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows[cells[0]] = (cells[5], cells[6])
    return rows


# ------------------------------------------------------------------------------------------
# Library-wide


def test_library_has_30_templates_keyed_by_file_stem(lib: dict[str, Template]) -> None:
    stems = {p.stem for p in LIBRARY_DIR.glob("*.yaml")}
    assert len(lib) == 30
    assert set(lib) == stems
    assert all(t.id == tid for tid, t in lib.items())


def test_readme_table_lists_every_template(lib: dict[str, Template]) -> None:
    assert set(_readme_table()) == set(lib)


@pytest.mark.parametrize("tid", sorted(p.stem for p in LIBRARY_DIR.glob("*.yaml")))
def test_template_resolves_in_every_allowed_mode(lib: dict[str, Template], tid: str) -> None:
    t = lib[tid]
    assert t.indoor_ok or t.outdoor_ok
    assert t.tss_model == "closed_form"
    assert set(t.progression) <= set(t.params)
    if t.outdoor_ok:
        assert t.outdoor_rendering is not None
    if t.hr_fallback is None:
        assert t.hr_fallback_reason_zh

    resolved: list[ResolvedWorkout] = []
    if t.indoor_ok:
        resolved.append(resolve(t))
    if t.outdoor_ok:
        resolved.append(resolve(t, outdoor=True))
    if t.hr_fallback is not None:
        resolved.append(resolve(t, outdoor=not t.indoor_ok, target="HR"))
    else:
        with pytest.raises(TemplateError):
            resolve(t, outdoor=not t.indoor_ok, target="HR")

    durations = {w.duration_s for w in resolved}
    assert len(durations) == 1, "HR / outdoor durations must equal the power version"
    for w in resolved:
        assert w.duration_s > 0
        assert w.tss > 0
        assert 0 < w.if_ < 1.5
        assert "{" not in w.name and "{" not in w.name_zh
        assert any(s.kind in ("work", "steady") for s in _leaves(w))
        for it in w.items:
            if isinstance(it, Repeat):
                assert it.count >= 1
                assert all(isinstance(s, Step) for s in it.steps)
        for s in _leaves(w):
            assert s.duration_s > 0
            if s.kind == "freeride":
                assert s.tss_assume is not None and s.lo is None and s.hi is None
            else:
                assert s.lo is not None and s.hi is not None
        if w.target == "HR":
            assert w.note_zh is not None and "{" not in w.note_zh
        if w.outdoor and w.target == "POWER":
            assert w.note_zh is not None and "{" not in w.note_zh


@pytest.mark.parametrize("tid", sorted(p.stem for p in LIBRARY_DIR.glob("*.yaml")))
def test_raw_steps_have_no_nested_repeats(lib: dict[str, Template], tid: str) -> None:
    t = lib[tid]
    lists = [t.steps] + ([t.hr_fallback["steps"]] if t.hr_fallback else [])
    for steps in lists:
        for item in steps:
            if "repeat" in item:
                assert all("repeat" not in leaf for leaf in item["steps"])


@pytest.mark.parametrize("tid", sorted(p.stem for p in LIBRARY_DIR.glob("*.yaml")))
def test_every_grid_point_resolves(lib: dict[str, Template], tid: str) -> None:
    t = lib[tid]
    grid = param_grid(t)
    assert grid, "at least the defaults"
    assert t.defaults() in grid
    for params in grid:
        w = resolve(t, params, outdoor=not t.indoor_ok)
        assert w.duration_s > 0


@pytest.mark.parametrize(
    ("tid", "tss", "minutes"),
    [(tid, tss, m) for tid, (tss, m) in _readme_table().items() if not tss.endswith("*")],
)
def test_readme_table_tss_and_minutes(
    lib: dict[str, Template], tid: str, tss: str, minutes: str
) -> None:
    # The §9 table is the closed-form value of the plain power steps (no outdoor transforms).
    got_tss, got_s = nominal_tss(lib[tid])
    assert abs(got_tss - float(tss)) <= 1.0
    assert abs(got_s / 60 - float(minutes)) <= 1.0


def test_readme_section6_example_ss_3x_n_defaults(lib: dict[str, Template]) -> None:
    w = resolve(lib["ss_3x_n"])
    assert w.tss == pytest.approx(55, abs=1)
    assert w.duration_s == 60 * 60
    assert w.if_ == pytest.approx(0.74, abs=0.01)
    assert w.name == "Sweet Spot 3x10"


def test_outdoor_transforms_over_unders(lib: dict[str, Template]) -> None:
    w = resolve(lib["over_unders_3x_n"], outdoor=True)
    warm = w.items[0]
    assert isinstance(warm, Step)
    assert (warm.kind, warm.lo, warm.hi) == ("steady", 50, 72)  # ramps_as_range, not widened
    recover = w.items[4]
    assert isinstance(recover, Step)
    assert (recover.kind, recover.tss_assume, recover.cue) == ("freeride", 50, "Recover")
    assert w.note_zh is not None and w.note_zh.startswith("每組")
    assert w.tss == pytest.approx(66, abs=1)
    assert w.duration_s == 65 * 60


def test_widen_is_clamped_at_30() -> None:
    t = load_template(LIBRARY_DIR / "recovery_spin.yaml")
    w = resolve(t, outdoor=True)
    assert all(s.lo is None or s.lo >= 30 for s in _leaves(w))


def test_hr_note_and_outdoor_placeholders(lib: dict[str, Template]) -> None:
    t = lib["climb_repeats_goal_power"]
    w = resolve(t, {"climb_min": 9}, outdoor=True)
    assert w.note_zh is not None and "9 分鐘" in w.note_zh
    assert w.name == "Climb Repeats 4x9 @ Goal Power (112%)"
    hr = resolve(t, outdoor=True, target="HR")
    assert hr.note_zh is not None and "LTHR" in hr.note_zh
    assert all(s.kind != "freeride" or s.tss_assume in (40, 55) for s in _leaves(hr))


def test_closed_form_tss_counts_repeats() -> None:
    items = (
        Step("steady", 3600, 100, 100),
        Repeat(
            2, "x", (Step("work", 1800, 90, 110), Step("freeride", 600, None, None, None, None, 60))
        ),
    )
    tss, dur = closed_form_tss(items)
    assert dur == 3600 + 2 * 2400
    assert tss == pytest.approx(100 + 2 * (50 + 600 / 3600 * 36))


# ------------------------------------------------------------------------------------------
# Expression grammar


def test_evaluate_precedence() -> None:
    assert evaluate("2+3*4", {}) == 14
    assert evaluate("2*3+4", {}) == 10
    assert evaluate("10-4-3", {}) == 3  # left to right
    assert evaluate("100/8/5", {}) == 3  # 2.5 -> 3, rounded only at the end
    assert evaluate("7/2*2", {}) == 7
    assert evaluate("1/3", {}) == 0
    assert evaluate("5/2", {}) == 3  # half rounds up
    assert evaluate(" 4 + 1 ", {}) == 5
    assert evaluate(12, {}) == 12


def test_evaluate_readme_example() -> None:
    p = {"total_min": 210, "ss_reps": 2, "ss_min": 15}
    assert evaluate("{total_min}-{ss_reps}*{ss_min}-{ss_reps}*5-35", p) == 135
    assert evaluate("{pct}+4", {"pct": 88}) == 92


@pytest.mark.parametrize("expr", ["{nope}+1", "2+", "(1+2)", "1 2", "abc", "", "4/0"])
def test_evaluate_errors(expr: str) -> None:
    with pytest.raises(TemplateError):
        evaluate(expr, {"pct": 1})


def test_long_ride_late_ss_endurance_duration(lib: dict[str, Template]) -> None:
    w = resolve(lib["long_ride_late_ss"], outdoor=True)
    endurance = w.items[1]
    assert isinstance(endurance, Step)
    assert endurance.duration_s == 135 * 60


# ------------------------------------------------------------------------------------------
# resolve() errors


def test_param_specs(lib: dict[str, Template]) -> None:
    t = lib["ss_3x_n"]
    assert t.params["rest_min"] == ParamSpec(4, 4, 4, 1, fixed=True)
    assert t.params["work_min"] == ParamSpec(8, 20, 10, 2)
    assert t.defaults() == {"reps": 3, "work_min": 10, "pct": 88, "rest_min": 4}
    assert len(param_grid(t)) == 3 * 7 * 7


@pytest.mark.parametrize(
    "params",
    [
        {"reps": 5},  # out of range
        {"pct": 85},  # out of range
        {"work_min": 11},  # off step (8, 10, 12, ...)
        {"rest_min": 5},  # fixed param
        {"bogus": 1},  # unknown
    ],
)
def test_resolve_rejects_bad_params(lib: dict[str, Template], params: dict[str, int]) -> None:
    with pytest.raises(TemplateError):
        resolve(lib["ss_3x_n"], params)


def test_resolve_partial_params_merge_defaults(lib: dict[str, Template]) -> None:
    w = resolve(lib["ss_3x_n"], {"work_min": 12})
    assert w.params == {"reps": 3, "work_min": 12, "pct": 88, "rest_min": 4}


def test_resolve_venue_and_target_errors(lib: dict[str, Template]) -> None:
    with pytest.raises(TemplateError):
        resolve(lib["vo2_4x5"], target="HR")  # hr_fallback: null
    with pytest.raises(TemplateError):
        resolve(lib["ramp_test"], outdoor=True)  # indoor only
    with pytest.raises(TemplateError):
        resolve(lib["climb_repeats_goal_power"])  # outdoor only, indoor requested


# ------------------------------------------------------------------------------------------
# Loader validation


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _mutated(src: str, old: str, new: str) -> str:
    assert old in src
    return src.replace(old, new, 1)


SS = (LIBRARY_DIR / "ss_3x_n.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("intent: sweetspot", "intent: fartlek"),
        ("phases: [base, build]", "phases: [base, peak]"),
        ("indoor_ok: true\noutdoor_ok: true", "indoor_ok: false\noutdoor_ok: false"),
        ("tss_model: closed_form", "tss_model: pmc"),
        ('duration: "{work_min}m"', 'duration: "{work_mins}m"'),
        ('name: "Sweet Spot {reps}x{work_min}"', 'name: "Sweet Spot {sets}"'),
        ("default: 10, step: 2", "default: 11, step: 2"),
        ("duration: 8m", "duration: 8"),
        ("kind: steady, duration: 8m", "kind: cruise, duration: 8m"),
        (
            '      - {kind: rest, duration: "{rest_min}m", lo: 50, hi: 55}\n  - {cue: Cooldown',
            "      - {repeat: 2, steps: [{kind: rest, duration: 1m, lo: 50, hi: 55}]}\n"
            "  - {cue: Cooldown",
        ),
        # HR fallback must mirror the power duration
        (
            "    - {cue: Cooldown, kind: steady, duration: 8m, lo: 60, hi: 68}",
            "    - {cue: Cooldown, kind: steady, duration: 9m, lo: 60, hi: 68}",
        ),
        ("hr_fallback:\n  note_zh", "hr_fallback_x:\n  note_zh"),
    ],
)
def test_loader_rejects_invalid(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(TemplateError):
        load_template(_write(tmp_path, "ss_3x_n", _mutated(SS, old, new)))


def test_loader_null_hr_fallback_requires_reason(tmp_path: Path) -> None:
    src = (LIBRARY_DIR / "vo2_4x5.yaml").read_text(encoding="utf-8")
    src = re.sub(r"(?m)^hr_fallback_reason_zh:.*\n", "", src)
    with pytest.raises(TemplateError):
        load_template(_write(tmp_path, "vo2_4x5", src))


def test_load_library_requires_id_equal_to_stem(tmp_path: Path) -> None:
    _write(tmp_path, "other_name", SS)
    with pytest.raises(TemplateError):
        load_library(tmp_path)


def test_loader_rejects_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(TemplateError):
        load_template(_write(tmp_path, "x", "id: [unclosed"))
