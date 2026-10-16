"""Shared HTML -> PDF plumbing for the zh-TW PDFs (per-ride report, weekly / monthly digests).

HTML (Jinja templates in ``cyp/reports/templates``, ``report.css`` inlined) -> PDF with
WeasyPrint. Charts come as SVG ``data:`` URIs with their text already as paths, so WeasyPrint
only needs a zh-TW font for the body text (``report.css`` lists Heiti TC on macOS (see the
comment there on PingFang) and Noto on Linux).

WeasyPrint loads pango/harfbuzz/fontconfig with ``dlopen``. A uv-managed Python on macOS does
not search Homebrew's lib dir, so :func:`write_pdf` adds it to ``DYLD_FALLBACK_LIBRARY_PATH``
(read by ``ctypes.util.find_library``, which cffi falls back to) before the first import.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from cyp.reports.render import _num, _pct, _signed, _weekday

HOMEBREW_LIB_DIRS = ("/opt/homebrew/lib", "/usr/local/lib")
DEFAULT_TZ = "Asia/Taipei"  # settings.Settings.cyp_timezone default


def mmss(seconds: int | float | None) -> str:
    """``m:ss`` (``–`` for ``None``)."""
    if seconds is None:
        return "–"
    total = round(seconds)
    return f"{total // 60}:{total % 60:02d}"


def environment() -> Environment:
    """Jinja environment for the HTML templates (autoescaped)."""
    env = Environment(
        loader=PackageLoader("cyp.reports", "templates"),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(num=_num, pct=_pct, signed=_signed, weekday=_weekday, mmss=mmss)
    return env


def local(moment: dt.datetime, tz: str) -> dt.datetime:
    """``moment`` in the athlete's timezone; a naive value is taken as already local."""
    return moment if moment.tzinfo is None else moment.astimezone(ZoneInfo(tz))


def write_pdf(html: str, pdf: Path) -> None:
    """Render ``html`` to ``pdf`` with WeasyPrint (``OSError`` when pango is missing)."""
    if sys.platform == "darwin":
        current = [p for p in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":") if p]
        extra = [d for d in HOMEBREW_LIB_DIRS if Path(d).is_dir() and d not in current]
        if extra:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join([*current, *extra])
    import weasyprint

    weasyprint.HTML(string=html, base_url=str(pdf.parent)).write_pdf(pdf)
