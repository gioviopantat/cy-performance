"""Render a weekly / monthly digest to a zh-TW PDF (docs/specs/digest-reports.md).

Same look as the per-ride PDF: ``templates/digest_weekly.html.j2`` / ``digest_monthly.html.j2``
extend ``_digest.html.j2`` and share ``report.css`` and the ``_report.html.j2`` macros with
``ride.html.j2``; :mod:`cyp.reports.pdf` turns the HTML into a PDF and
:mod:`cyp.reports.digest_charts` draws the charts. A break period (``period.is_break``) renders
the short version. The templates only format; the one piece of layout prepared here is the
weekly grid (each day of the week with its session lines, in order).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cyp.reports.digest_charts import digest_charts
from cyp.reports.digest_model import MonthlyDigest, SessionLine, WeeklyDigest
from cyp.reports.pdf import DEFAULT_TZ, environment, local, write_pdf


def kind(digest: WeeklyDigest | MonthlyDigest) -> str:
    """``weekly`` or ``monthly``."""
    return "weekly" if isinstance(digest, WeeklyDigest) else "monthly"


def pdf_name(digest: WeeklyDigest | MonthlyDigest) -> str:
    """``<weekly|monthly>_<period start>.pdf``."""
    return f"{kind(digest)}_{digest.period.start.isoformat()}.pdf"


def week_grid(
    start: dt.date, end: dt.date, sessions: list[SessionLine]
) -> list[tuple[dt.date, list[SessionLine]]]:
    """Every day from ``start`` to ``end`` with its session lines (an empty list is a rest day)."""
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    return [(d, [s for s in sessions if s.day == d]) for d in days]


def render_html(digest: WeeklyDigest | MonthlyDigest, *, tz: str = DEFAULT_TZ) -> str:
    """The digest as one self-contained HTML page (CSS inline, charts as ``data:`` URIs)."""
    template = environment().get_template(f"digest_{kind(digest)}.html.j2")
    grid = (
        week_grid(digest.period.start, digest.period.end, digest.sessions)
        if isinstance(digest, WeeklyDigest)
        else []
    )
    return template.render(
        d=digest,
        charts=digest_charts(digest),
        grid=grid,
        generated_local=local(digest.generated_at, tz),
    )


def render_digest_pdf(
    digest: WeeklyDigest | MonthlyDigest, out_dir: Path, *, tz: str = DEFAULT_TZ
) -> Path:
    """Write ``<out_dir>/<weekly|monthly>_<period start>.pdf`` and return its path.

    The facts ``.json`` is written beside it. ``tz`` (the athlete's ``cyp_timezone``) is used
    for the generation timestamp printed in the PDF; period dates are already local.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / pdf_name(digest)
    write_pdf(render_html(digest, tz=tz), pdf)
    pdf.with_suffix(".json").write_text(digest.model_dump_json(indent=1), encoding="utf-8")
    return pdf
