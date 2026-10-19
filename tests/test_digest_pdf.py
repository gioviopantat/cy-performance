"""Weekly / monthly digest PDFs (docs/specs/digest-reports.md): templates, charts, WeasyPrint."""

from __future__ import annotations

import datetime as dt
import json
import re
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from pypdf import PdfReader

from cyp.reports import charts, digest_charts, digest_pdf, pdf
from cyp.reports.digest_model import (
    Consistency,
    DigestHeadline,
    Distribution,
    GoalProgress,
    LimiterRank,
    LimiterShift,
    Load,
    MonthlyDigest,
    Period,
    PowerCurve,
    Recovery,
    RecoveryDay,
    WeeklyDigest,
)
from cyp.reports.ride_model import FtpPoint

FIXTURES = Path(__file__).parent / "fixtures"
STRINGS = yaml.safe_load((FIXTURES / "digest.strings.yaml").read_text("utf-8"))
MIN = STRINGS["minimal"]
IMG = '<img src="data:image/svg+xml;base64,'


def _weekly(name: str = "digest_weekly.sample.json") -> WeeklyDigest:
    return WeeklyDigest.model_validate_json((FIXTURES / name).read_text(encoding="utf-8"))


def _monthly() -> MonthlyDigest:
    raw = (FIXTURES / "digest_monthly.sample.json").read_text(encoding="utf-8")
    return MonthlyDigest.model_validate_json(raw)


def _period(start: dt.date, end: dt.date, label: str) -> Period:
    return Period(start=start, end=end, label_zh=label)


def _minimal_weekly() -> WeeklyDigest:
    """Only the required fields: every optional section is absent."""
    return WeeklyDigest(
        profile="sample",
        athlete_name=MIN["athlete_name"],
        generated_at=dt.datetime(2026, 3, 16, 7, 0),
        period=_period(dt.date(2026, 3, 9), dt.date(2026, 3, 15), MIN["label"]),
        headline=DigestHeadline(text_zh=MIN["headline"], verdict="no_data"),
    )


def _minimal_monthly() -> MonthlyDigest:
    return MonthlyDigest(
        profile="sample",
        athlete_name=MIN["athlete_name"],
        generated_at=dt.datetime(2026, 5, 1, 7, 0),
        period=_period(dt.date(2026, 4, 1), dt.date(2026, 4, 30), MIN["month_label"]),
        headline=DigestHeadline(text_zh=MIN["headline"], verdict="no_data"),
    )


def _text(path: Path) -> str:
    return "".join(page.extract_text() for page in PdfReader(path).pages)


def _squash(text: str) -> str:
    return "".join(text.split())


@pytest.fixture
def need_weasyprint(tmp_path: Path) -> None:
    try:
        pdf.write_pdf("<p>probe</p>", tmp_path / "probe.pdf")
    except OSError as exc:  # pango/harfbuzz not installed on this machine
        pytest.skip(f"WeasyPrint system libraries missing: {exc}")


@pytest.fixture
def no_missing_glyphs() -> Iterator[None]:
    """Fail on matplotlib's 'Glyph ... missing from font' warning (tofu in a chart)."""
    if charts.cjk_font() is None:
        pytest.skip("no zh-TW font installed")
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=".*missing from font.*")
        yield


def _headings(html: str) -> list[str]:
    """Text of every section heading (``<h2>``), in order."""
    return [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h2>(.*?)</h2>", html, re.S)]


def _heading_index(html: str, heading: str) -> int | None:
    return next((i for i, h in enumerate(_headings(html)) if heading in h), None)


def _is_svg(data: bytes) -> bool:
    return b"<svg" in data[:2000] and len(data) > 500


@pytest.mark.parametrize(
    ("digest", "name", "sections"),
    [
        (_weekly(), "weekly_2026-03-09.pdf", "weekly_sections"),
        (_monthly(), "monthly_2026-04-01.pdf", "monthly_sections"),
    ],
)
def test_sample_pdfs_have_zh_text_and_facts_json(
    tmp_path: Path,
    need_weasyprint: None,
    no_missing_glyphs: None,
    digest: WeeklyDigest | MonthlyDigest,
    name: str,
    sections: str,
) -> None:
    out = digest_pdf.render_digest_pdf(digest, tmp_path / "out")

    assert out.name == name
    text = _squash(_text(out))
    for heading in STRINGS[sections]:
        assert _squash(heading) in text, heading
    assert _squash(digest.headline.text_zh) in text
    facts = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert type(digest).model_validate(facts) == digest


def test_body_text_embeds_a_zh_font_never_pingfang(tmp_path: Path, need_weasyprint: None) -> None:
    out = digest_pdf.render_digest_pdf(_weekly(), tmp_path)
    fonts: set[str] = set()
    for page in PdfReader(out).pages:
        for ref in page["/Resources"].get("/Font", {}).values():
            fonts.add(str(ref.get_object()["/BaseFont"]))
    if charts.cjk_font() is None:
        pytest.skip("no zh-TW font installed")
    assert any(any(c in f for c in ("Heiti", "Noto", "SourceHan", "Arial-Unicode")) for f in fonts)
    assert not any("PingFang" in f for f in fonts), fonts


@pytest.mark.parametrize(
    ("digest", "sections"),
    [(_weekly(), "weekly_sections"), (_monthly(), "monthly_sections")],
)
def test_sections_follow_spec_order(digest: WeeklyDigest | MonthlyDigest, sections: str) -> None:
    html = digest_pdf.render_html(digest)
    positions = [_heading_index(html, h) for h in STRINGS[sections]]
    assert None not in positions
    assert positions == sorted(positions, key=lambda p: p or 0)
    assert len(_headings(html)) == len(STRINGS[sections])


def test_weekly_charts_and_grid() -> None:
    digest = _weekly()
    html = digest_pdf.render_html(digest)
    # load, distribution, goal, recovery, one climb
    assert html.count(IMG) == 5
    grid = digest_pdf.week_grid(digest.period.start, digest.period.end, digest.sessions)
    assert [d for d, _ in grid] == [dt.date(2026, 3, 9) + dt.timedelta(days=i) for i in range(7)]
    assert html.count('class="day-cell') == 7
    for key, badge in STRINGS["badges"].items():
        assert (badge in html) is (key != "break"), badge
    assert STRINGS["climb_name"] in html
    assert STRINGS["this_week"] in html
    assert STRINGS["readiness_easy"] in html
    assert STRINGS["durability_label"] in html
    assert "97 %" in html  # durability ratio as a percent


def test_monthly_charts() -> None:
    html = digest_pdf.render_html(_monthly())
    # consistency, ctl, power curve, goal, distribution, limiters, recovery
    assert html.count(IMG) == 7
    assert STRINGS["best_since"] in html


def test_grade_badges_and_missing_grade() -> None:
    html = digest_pdf.render_html(_weekly())
    for grade in STRINGS["grades"]:
        assert grade in html
    assert STRINGS["weak_grade"] in html
    assert STRINGS["no_grade"] in html


def test_break_week_is_one_short_page(tmp_path: Path, need_weasyprint: None) -> None:
    digest = _weekly("digest_weekly.break.json")
    assert digest.period.is_break
    html = digest_pdf.render_html(digest)
    for absent in STRINGS["weekly_optional"]:
        assert _heading_index(html, absent) is None, absent
    assert html.count(IMG) == 1  # CTL drift only
    assert STRINGS["badges"]["break"] in html

    out = digest_pdf.render_digest_pdf(digest, tmp_path)
    reader = PdfReader(out)
    assert len(reader.pages) == 1
    text = _squash(_text(out))
    for heading in STRINGS["break_sections"]:
        assert _squash(heading) in text
    assert _squash(digest.period.break_reason_zh) in text
    assert "56.3" in text  # CTL end of the break


def test_monthly_break_without_sessions_renders(tmp_path: Path, need_weasyprint: None) -> None:
    month = _minimal_monthly()
    month.period.is_break = True
    month.consistency = Consistency(planned=6, done=0, achieved=0)
    html = digest_pdf.render_html(month)
    assert STRINGS["break_sections"][1] in html
    assert len(PdfReader(digest_pdf.render_digest_pdf(month, tmp_path)).pages) == 1


@pytest.mark.parametrize(
    ("digest", "optional"),
    [(_minimal_weekly(), "weekly_optional"), (_minimal_monthly(), "monthly_optional")],
)
def test_optional_sections_are_omitted(
    tmp_path: Path,
    need_weasyprint: None,
    digest: WeeklyDigest | MonthlyDigest,
    optional: str,
) -> None:
    html = digest_pdf.render_html(digest)
    for absent in STRINGS[optional]:
        assert _heading_index(html, absent) is None, absent
    assert "<img" not in html
    assert len(_headings(html)) == 1
    assert STRINGS["weekly_sections"][0] in _headings(html)[0]

    out = digest_pdf.render_digest_pdf(digest, tmp_path)
    assert _squash(MIN["headline"]) in _squash(_text(out))


def test_local_time_in_footer() -> None:
    digest = _weekly()  # generated_at 07:00 UTC
    assert "2026-03-16 15:00" in digest_pdf.render_html(digest)
    assert "2026-03-16 16:00" in digest_pdf.render_html(digest, tz="Asia/Tokyo")


def test_single_week_month_and_empty_inputs(no_missing_glyphs: None) -> None:
    """Edge cases: one week, no FTP history, no recovery signals, no curve, only new limiters."""
    month = _minimal_monthly()
    week = dt.date(2026, 3, 30)
    dist = Distribution(
        sessions={"low": 1, "mid": 0, "high": 0},
        session_frac={"low": 1.0, "mid": 0.0, "high": 0.0},
        verdict="no_target",
    )
    month.consistency = Consistency(planned=1, done=1, achieved=1, by_week=[(week, 1, 1, 1)])
    month.distribution_by_week = [(week, dist)]
    month.load = Load(planned_tss=50, actual_tss=40, hours=1, rides=1, ctl_series=[(week, 40.0)])
    month.power_curve = PowerCurve(
        durations_s=[5, 60], this_month_w=[None, None], last_month_w=[], season_best_w=[]
    )
    month.limiters = LimiterShift(
        lines=[LimiterRank(limiter_id="threshold", name_zh=MIN["limiter"], rank_now=1)]
    )
    month.recovery = Recovery(days=[RecoveryDay(day=week)])
    out = digest_charts.digest_charts(month)
    for key in ("consistency", "ctl", "power_curve", "distribution", "limiters", "recovery"):
        assert out[key] is not None and out[key].startswith("data:image/svg+xml;base64,"), key
    html = digest_pdf.render_html(month)
    assert html.count(IMG) == 6

    goal = GoalProgress(target_w=270, target_date=dt.date(2026, 9, 30), current_ftp_w=250)
    assert _is_svg(digest_charts.goal_trajectory(goal, dt.date(2026, 3, 15)))
    one = goal.model_copy(
        update={"history": [FtpPoint(day=dt.date(2026, 3, 1), ftp_w=250, source="icu_eftp")]}
    )
    assert _is_svg(digest_charts.goal_trajectory(one, dt.date(2026, 3, 15)))

    day = dt.date(2026, 3, 9)
    only_rhr = Recovery(days=[RecoveryDay(day=day, rhr=52)])
    only_hrv = Recovery(days=[RecoveryDay(day=day, hrv_z7=-0.4)])
    assert _is_svg(digest_charts.recovery_days(only_rhr))
    assert _is_svg(digest_charts.recovery_days(only_hrv))
    empty_load = Load(planned_tss=0, actual_tss=0, hours=0, rides=0, daily=[(day, 0.0, 0.0)])
    assert _is_svg(digest_charts.load_week(empty_load))
    assert _is_svg(digest_charts.ctl_path(Load(planned_tss=0, actual_tss=0, hours=0, rides=0)))


@pytest.mark.parametrize(
    ("name", "model"),
    [("digest_weekly.full.json", WeeklyDigest), ("digest_monthly.full.json", MonthlyDigest)],
)
def test_builder_golden_renders_if_present(
    tmp_path: Path,
    need_weasyprint: None,
    name: str,
    model: type[WeeklyDigest] | type[MonthlyDigest],
) -> None:
    """The digest-facts session's goldens render end to end."""
    golden = FIXTURES / name
    if not golden.exists():
        pytest.skip(f"{name} arrives with the digest-facts branch")
    digest = model.model_validate_json(golden.read_text(encoding="utf-8"))
    out = digest_pdf.render_digest_pdf(digest, tmp_path)
    assert _squash(digest.headline.text_zh)[:12] in _squash(_text(out))
