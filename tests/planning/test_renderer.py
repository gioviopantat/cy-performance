"""intervals.icu text renderer: README §7 examples byte-for-byte, plus formatting rules."""

from __future__ import annotations

import re

import pytest

from cyp.planning.renderer import format_duration, render
from cyp.planning.templates import (
    LIBRARY_DIR,
    Repeat,
    ResolvedWorkout,
    Step,
    Template,
    load_library,
    resolve,
)


@pytest.fixture(scope="module")
def lib() -> dict[str, Template]:
    return load_library()


def _readme_example(n: int) -> str:
    text = (LIBRARY_DIR / "README.md").read_text(encoding="utf-8")
    m = re.search(rf"### Example {n} .*?\n```\n(.*?)\n```", text, re.S)
    assert m is not None
    return m.group(1)


def test_example_1_ss_3x_n_indoor(lib: dict[str, Template]) -> None:
    w = resolve(lib["ss_3x_n"], {"reps": 3, "work_min": 12, "pct": 88, "rest_min": 4})
    assert render(w) == _readme_example(1)
    assert w.name == "Sweet Spot 3x12"
    assert w.tss == pytest.approx(63, abs=1)


def test_example_2_over_unders_outdoor(lib: dict[str, Template]) -> None:
    w = resolve(
        lib["over_unders_3x_n"], {"cycles": 3, "under_pct": 93, "over_pct": 105}, outdoor=True
    )
    assert render(w) == _readme_example(2)
    assert w.tss == pytest.approx(66, abs=1)
    assert w.duration_s == 65 * 60


def test_example_3_long_ride_late_tempo_hr(lib: dict[str, Template]) -> None:
    t = lib["long_ride_late_tempo"]
    w = resolve(t, {"total_min": 210, "tempo_min": 30}, outdoor=True, target="HR")
    text = render(w)
    assert text == _readme_example(3)
    assert "%" in text and all(
        line.endswith("LTHR") or "LTHR " in line for line in text.splitlines() if "%" in line
    )
    # The power version quoted under Example 3 (indoor_ok is false, so the steps resolve outdoor;
    # the quoted lines are the untransformed targets, checked through the raw step ranges).
    p = resolve(t, {"total_min": 210, "tempo_min": 30}, outdoor=True)
    assert p.tss == pytest.approx(168, abs=1)
    assert p.duration_s == 210 * 60


def test_every_template_renders_without_mixing_targets(lib: dict[str, Template]) -> None:
    for t in lib.values():
        variants = []
        if t.indoor_ok:
            variants.append(resolve(t))
        if t.outdoor_ok:
            variants.append(resolve(t, outdoor=True))
        if t.hr_fallback is not None:
            variants.append(resolve(t, outdoor=not t.indoor_ok, target="HR"))
        for w in variants:
            text = render(w)
            assert "mtr" not in text and not re.search(r"\d+[wW]\b", text)
            lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
            for ln in lines:
                if w.target == "HR":
                    assert "freeride" in ln or "% LTHR" in ln
                else:
                    assert "LTHR" not in ln
            assert "\n\n\n" not in text
            assert not text.startswith("\n") and not text.endswith("\n")


def test_repeat_blocks_have_blank_lines_and_untitled_sections() -> None:
    w = ResolvedWorkout(
        template_id="x",
        template_version=1,
        params={},
        target="POWER",
        outdoor=False,
        items=(
            Step("steady", 300, 50, 50),
            Repeat(4, None, (Step("work", 30, 120, 125, "100rpm"), Step("rest", 90, 50, 50))),
            Step("freeride", 600, None, None, tss_assume=45),
            Step("ramp", 3600 + 1800, 60, 40, cue="Cool"),
        ),
        name="x",
        name_zh="x",
        duration_s=0,
        tss=0.0,
        if_=0.0,
        note_zh=None,
    )
    assert render(w) == (
        "- 5m 50%\n"
        "\n"
        "4x\n"
        "- 30s 120-125% 100rpm\n"
        "- 1m30s 50%\n"
        "\n"
        "- 10m freeride\n"
        "\n"
        "Cool\n"
        "- 90m ramp 60-40%"
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(30, "30s"), (60, "1m"), (600, "10m"), (8700, "145m"), (90, "1m30s"), (5400, "90m")],
)
def test_format_duration(seconds: int, text: str) -> None:
    assert format_duration(seconds) == text
