"""Per-ride PDF rendering (docs/specs/ride-report.md): template, charts, WeasyPrint output."""

from __future__ import annotations

import datetime as dt
import json
import warnings
from pathlib import Path

import pytest
import yaml
from pypdf import PdfReader

from cyp.reports import charts, ride_pdf
from cyp.reports.ride_model import (
    ClimbCompare,
    ClimbEffort,
    Feel,
    FtpPoint,
    Goal,
    Headline,
    Intent,
    OutdoorContext,
    RideReport,
    RideSummary,
    Segment,
    Series,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ride_report.sample.json"
STRINGS = yaml.safe_load((FIXTURE.parent / "ride_report.strings.yaml").read_text("utf-8"))
SECTIONS: list[str] = STRINGS["sections"]
MIN = STRINGS["minimal"]


def _sample() -> RideReport:
    return RideReport.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def _minimal() -> RideReport:
    """Only the required fields: every optional section is absent, the ride is unrated."""
    return RideReport(
        profile="sample",
        athlete_name=MIN["athlete_name"],
        generated_at=dt.datetime(2026, 10, 11, 10, 0),
        ride=RideSummary(
            activity_id=1234,
            name=MIN["ride_name"],
            start_local=dt.datetime(2026, 10, 11, 7, 0),
            sport="Ride",
            indoor=True,
            moving_min=45,
        ),
        headline=Headline(text_zh=MIN["headline"], verdict="unplanned"),
        feel=Feel(rated=False, rate_url="http://127.0.0.1:8000/rides/1234/feel"),
    )


@pytest.fixture
def need_weasyprint(tmp_path: Path) -> None:
    try:
        ride_pdf._write_pdf("<p>probe</p>", tmp_path / "probe.pdf")
    except OSError as exc:  # pango/harfbuzz not installed on this machine
        pytest.skip(f"WeasyPrint system libraries missing: {exc}")


def _fonts(reader: PdfReader) -> set[str]:
    names: set[str] = set()
    for page in reader.pages:
        for ref in page["/Resources"].get("/Font", {}).values():
            names.add(str(ref.get_object()["/BaseFont"]))
    return names


def test_sample_pdf_has_zh_text_and_facts_json(tmp_path: Path, need_weasyprint: None) -> None:
    report = _sample()
    pdf = ride_pdf.render_ride_pdf(report, tmp_path / "out")

    assert pdf.name == "2026-10-11_9876543210.pdf"
    reader = PdfReader(pdf)
    text = "".join(page.extract_text() for page in reader.pages)
    for heading in SECTIONS:
        assert heading in text
    assert report.headline.text_zh[:12] in text
    assert STRINGS["climb_name"] in text
    facts = pdf.with_suffix(".json")
    assert RideReport.model_validate(json.loads(facts.read_text(encoding="utf-8"))) == report


def test_body_text_uses_a_zh_font_that_subsets_correctly(
    tmp_path: Path, need_weasyprint: None
) -> None:
    """A CJK face is embedded (no tofu), and never PingFang: WeasyPrint subsets a later face of
    PingFang.ttc with the wrong glyphs (ride.css)."""
    pdf = ride_pdf.render_ride_pdf(_sample(), tmp_path)
    fonts = _fonts(PdfReader(pdf))
    cjk = ("Heiti", "STHeiti", "Noto", "SourceHan", "Arial-Unicode")
    if charts.cjk_font() is None:
        pytest.skip("no zh-TW font installed")
    assert any(any(c in f for c in cjk) for f in fonts), fonts
    assert not any("PingFang" in f for f in fonts), fonts


def test_sections_follow_spec_order() -> None:
    html = ride_pdf.render_html(_sample())
    positions = [html.index(h) for h in SECTIONS]
    assert positions == sorted(positions)
    assert html.count('<img src="data:image/svg+xml;base64,') == 3 + len(_sample().climbs)


def test_optional_sections_are_omitted(tmp_path: Path, need_weasyprint: None) -> None:
    report = _minimal()
    html = ride_pdf.render_html(report)
    for absent in STRINGS["optional_sections"]:
        assert absent not in html
    for present in STRINGS["always_sections"]:
        assert present in html
    assert "http://127.0.0.1:8000/rides/1234/feel" in html
    assert "<img" not in html

    pdf = ride_pdf.render_ride_pdf(report, tmp_path)
    text = PdfReader(pdf).pages[0].extract_text()
    assert MIN["headline"] in text


def test_grade_badges_and_missing_grade() -> None:
    html = ride_pdf.render_html(_sample())
    for grade in STRINGS["grades"]:
        assert grade in html
    assert STRINGS["weak_grade"] in html
    assert STRINGS["no_grade"] in html


def _is_svg(data: bytes) -> bool:
    return b"<svg" in data[:2000] and len(data) > 500


@pytest.fixture
def no_missing_glyphs():
    """Fail on matplotlib's 'Glyph ... missing from font' warning (tofu in the chart)."""
    if charts.cjk_font() is None:
        pytest.skip("no zh-TW font installed")
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=".*missing from font.*")
        yield


def test_sample_charts_render_without_tofu(no_missing_glyphs: None) -> None:
    out = charts.report_charts(_sample())
    for key in ("intent", "goal", "outdoor"):
        assert out[key].startswith("data:image/svg+xml;base64,")
    assert len(out["climbs"]) == 2


def test_charts_handle_empty_and_single_point_inputs(no_missing_glyphs: None) -> None:
    today = dt.date(2026, 10, 11)
    lone_goal = Goal(
        target_w=300, target_date=dt.date(2027, 6, 30), current_ftp_w=268, current_source="icu eFTP"
    )
    assert _is_svg(charts.ftp_trajectory(lone_goal, today))
    one_point = lone_goal.model_copy(
        update={
            "history": [FtpPoint(day=today, ftp_w=268, source="icu")],
            "projected_date": dt.date(2027, 8, 1),
        }
    )
    assert _is_svg(charts.ftp_trajectory(one_point, today))

    intent = Intent(
        planned_name="SST",
        kind="work",
        purpose_zh=MIN["purpose"],
        target_zone="Z3",
        target_min=40,
        achieved_min=0,
        verdict="no_data",
    )
    assert _is_svg(charts.intent_timeline(intent, None))
    empty = Series(step_s=30, power_w=[None, None], temp_c=[None, None])
    assert _is_svg(charts.intent_timeline(intent, empty))

    outdoor = OutdoorContext(stops_n=0, stopped_min=0)
    assert _is_svg(charts.outdoor_strip(outdoor, None))
    with_stop = outdoor.model_copy(update={"stops": [Segment(start_s=60, end_s=120)]})
    assert _is_svg(charts.outdoor_strip(with_stop, empty))

    bare = ClimbCompare(name=MIN["climb"], this=ClimbEffort(day=today, time_s=300))
    assert _is_svg(charts.climb_history(bare))
    mixed = ClimbCompare(
        name="mixed",
        this=ClimbEffort(day=today, power_w=240, time_s=300, kj_before=None, temp_c=28),
        history=[ClimbEffort(day=dt.date(2026, 9, 1), w_kg=3.5, time_s=310)],
    )
    assert _is_svg(charts.climb_history(mixed))


def test_units_precision_and_local_time() -> None:
    html = ride_pdf.render_html(_sample())
    # coasting_pct is already a percent (0-100), never multiplied again
    assert '<span class="value">18</span><span class="unit">%</span>' in html
    assert STRINGS["climb_badge"] in html
    # one precision for RPE, expected RPE and their gap
    for value in ("7.0", "5.6", "+1.4"):
        assert f'<span class="value">{value}</span>' in html
    # generated_at is UTC in the facts; the footer shows the athlete's local time
    assert "2026-10-11 10:42" in html
    assert "2026-10-11 11:42" in ride_pdf.render_html(_sample(), tz="Asia/Tokyo")


def test_ftp_chart_with_settings_only_and_ceiling_intent(no_missing_glyphs: None) -> None:
    today = dt.date(2026, 10, 11)
    goal = Goal(
        target_w=280,
        target_date=dt.date(2027, 6, 30),
        current_ftp_w=250,
        current_source="setting:icu",
        trend_level_w=255,
        projected_date=dt.date(2027, 5, 1),
        history=[FtpPoint(day=dt.date(2026, 6, 1), ftp_w=240, source="setting:icu")],
    )
    assert _is_svg(charts.ftp_trajectory(goal, today))
    ceiling = Intent(
        planned_name="Recovery",
        kind="ceiling",
        purpose_zh=MIN["purpose"],
        target_zone="Z1",
        target_high_w=140,
        target_min=45,
        achieved_min=44,
        verdict="achieved",
    )
    assert _is_svg(charts.intent_timeline(ceiling, Series(step_s=30, power_w=[120, 130, 150])))


def test_builder_golden_renders_if_present(tmp_path: Path, need_weasyprint: None) -> None:
    """The ride-facts session's golden (tests/fixtures/ride_report.full.json) renders end to end."""
    golden = FIXTURE.parent / "ride_report.full.json"
    if not golden.exists():
        pytest.skip("ride_report.full.json arrives with the ride-facts branch")
    report = RideReport.model_validate_json(golden.read_text(encoding="utf-8"))
    pdf = ride_pdf.render_ride_pdf(report, tmp_path)
    text = "".join(page.extract_text() for page in PdfReader(pdf).pages)
    assert report.headline.text_zh[:10] in text
    if report.outdoor and report.outdoor.coasting_pct is not None:
        assert f"{report.outdoor.coasting_pct:.0f}%" in text.replace(" ", "")
