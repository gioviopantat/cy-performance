"""Render a :class:`~cyp.reports.ride_model.RideReport` to a zh-TW PDF (docs/specs/ride-report.md).

HTML (Jinja ``templates/ride.html.j2`` + ``report.css`` / ``ride.css``) -> PDF with WeasyPrint;
the shared plumbing (Jinja environment, local time, the WeasyPrint call) is
:mod:`cyp.reports.pdf`. Charts come from :mod:`cyp.reports.charts`.
"""

from __future__ import annotations

from pathlib import Path

from cyp.reports.charts import report_charts
from cyp.reports.pdf import DEFAULT_TZ, environment, local
from cyp.reports.pdf import write_pdf as _write_pdf
from cyp.reports.ride_model import RideReport

__all__ = ["DEFAULT_TZ", "pdf_name", "render_html", "render_ride_pdf"]


def render_html(report: RideReport, *, tz: str = DEFAULT_TZ) -> str:
    """The report as one self-contained HTML page (CSS inline, charts as ``data:`` URIs)."""
    template = environment().get_template("ride.html.j2")
    return template.render(
        r=report, charts=report_charts(report), generated_local=local(report.generated_at, tz)
    )


def pdf_name(report: RideReport) -> str:
    """``<date>_<activity_id>.pdf``."""
    return f"{report.ride.start_local.date().isoformat()}_{report.ride.activity_id}.pdf"


def render_ride_pdf(report: RideReport, out_dir: Path, *, tz: str = DEFAULT_TZ) -> Path:
    """Write ``<out_dir>/<date>_<activity_id>.pdf`` and return its path.

    The facts ``.json`` is written beside it. ``tz`` (the athlete's ``cyp_timezone``) is used
    for timestamps printed in the PDF; the ride's own times are already local.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / pdf_name(report)
    _write_pdf(render_html(report, tz=tz), pdf)
    pdf.with_suffix(".json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
    return pdf
