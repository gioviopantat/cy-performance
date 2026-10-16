"""Render a :class:`~cyp.reports.ride_model.RideReport` to a zh-TW PDF (docs/specs/ride-report.md).

HTML (Jinja ``templates/ride.html.j2`` + ``ride.css``) -> PDF with WeasyPrint. Charts come
from :mod:`cyp.reports.charts` as SVG ``data:`` URIs with their text already as paths, so
WeasyPrint only needs a zh-TW font for the body text (``ride.css`` lists Heiti TC
on macOS (see the comment there on PingFang) and Noto on Linux).

WeasyPrint loads pango/harfbuzz/fontconfig with ``dlopen``. A uv-managed Python on macOS does
not search Homebrew's lib dir, so :func:`_write_pdf` adds it to ``DYLD_FALLBACK_LIBRARY_PATH``
(read by ``ctypes.util.find_library``, which cffi falls back to) before the first import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from cyp.reports.charts import report_charts
from cyp.reports.render import _num, _pct, _signed, _weekday
from cyp.reports.ride_model import RideReport

HOMEBREW_LIB_DIRS = ("/opt/homebrew/lib", "/usr/local/lib")


def _mmss(seconds: int | float | None) -> str:
    if seconds is None:
        return "–"
    total = round(seconds)
    return f"{total // 60}:{total % 60:02d}"


def environment() -> Environment:
    """Jinja environment for the HTML template (autoescaped)."""
    env = Environment(
        loader=PackageLoader("cyp.reports", "templates"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(num=_num, pct=_pct, signed=_signed, weekday=_weekday, mmss=_mmss)
    return env


def render_html(report: RideReport) -> str:
    """The report as one self-contained HTML page (CSS inline, charts as ``data:`` URIs)."""
    template = environment().get_template("ride.html.j2")
    return template.render(r=report, charts=report_charts(report))


def _write_pdf(html: str, pdf: Path) -> None:
    if sys.platform == "darwin":
        current = [p for p in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":") if p]
        extra = [d for d in HOMEBREW_LIB_DIRS if Path(d).is_dir() and d not in current]
        if extra:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join([*current, *extra])
    import weasyprint

    weasyprint.HTML(string=html, base_url=str(pdf.parent)).write_pdf(pdf)


def pdf_name(report: RideReport) -> str:
    """``<date>_<activity_id>.pdf``."""
    return f"{report.ride.start_local.date().isoformat()}_{report.ride.activity_id}.pdf"


def render_ride_pdf(report: RideReport, out_dir: Path) -> Path:
    """Write ``<out_dir>/<date>_<activity_id>.pdf`` and return its path.

    The facts ``.json`` is written beside it.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / pdf_name(report)
    _write_pdf(render_html(report), pdf)
    pdf.with_suffix(".json").write_text(report.model_dump_json(indent=1), encoding="utf-8")
    return pdf
