"""Render report facts to zh-TW Markdown with Jinja2 and write them under ``reports_dir``.

Files: ``daily/YYYY-MM-DD.md`` (+ ``.json`` facts) and ``weekly/YYYY-Www.md`` (+ ``.json``).
Templates live in ``cyp/reports/templates``; every verdict is rendered together with its
Explanation (headline, reasons with their weights, model, glossary link) — docs/07 §3.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

REC_ZH = {
    "REST": "休息",
    "EASY": "輕鬆騎",
    "AS_PLANNED": "照課表",
    "UPGRADE": "可以加碼",
}
STATUS_ZH = {
    "FRESH": "新鮮",
    "NORMAL": "正常",
    "BLUNTED": "心率鈍化",
    "OVERREACHED": "負荷過頭",
    "SICK": "生病／受傷",
}
COMPONENT_ZH = {
    "hrv": "HRV",
    "rhr": "安靜心率",
    "sleep": "睡眠",
    "tsb": "TSB",
    "ride": "昨天騎乘反應",
    "subjective": "主觀感受",
}
CONFIDENCE_ZH = {"high": "高", "medium": "中", "low": "低"}
WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def _num(value: Any, fmt: str = ".0f", dash: str = "–") -> str:
    if value is None:
        return dash
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return str(value)


def _signed(value: Any, fmt: str = "+.1f") -> str:
    return _num(value, fmt)


def _pct(value: Any, fmt: str = ".0f") -> str:
    return "–" if value is None else f"{float(value) * 100:{fmt}} %"


def _weekday(value: dt.date | str) -> str:
    d = value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])
    return f"週{WEEKDAY_ZH[d.weekday()]}"


def _glossary(term: str) -> str:
    return f"[{term}](../../../docs/glossary/{term}.md)"


def environment() -> Environment:
    """Jinja environment with the report filters."""
    env = Environment(
        loader=PackageLoader("cyp.reports", "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters.update(
        num=_num,
        signed=_signed,
        pct=_pct,
        weekday=_weekday,
        glossary=_glossary,
        rec_zh=lambda v: REC_ZH.get(v, v or "–"),
        status_zh=lambda v: STATUS_ZH.get(v, v or "–"),
        component_zh=lambda v: COMPONENT_ZH.get(v, v),
        confidence_zh=lambda v: CONFIDENCE_ZH.get(v, v),
    )
    return env


def render(kind: str, facts: dict[str, Any]) -> str:
    """Render ``daily`` or ``weekly`` facts to Markdown."""
    text = environment().get_template(f"{kind}.md.j2").render(**facts)
    return re.sub(r"\n{3,}", "\n\n", text).rstrip() + "\n"


def _default(o: Any) -> Any:
    if isinstance(o, dt.date):
        return o.isoformat()
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(type(o).__name__)


def write(kind: str, facts: dict[str, Any], reports_dir: Path) -> Path:
    """Render and write ``{kind}/{stem}.md`` plus the facts as ``.json``; returns the .md path."""
    if kind == "daily":
        stem = facts["date"].isoformat()
    else:
        iso = facts["week_start"].isocalendar()
        stem = f"{iso.year}-W{iso.week:02d}"
    out_dir = Path(reports_dir) / kind
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"{stem}.md"
    md.write_text(render(kind, facts), encoding="utf-8")
    (out_dir / f"{stem}.json").write_text(
        json.dumps(facts, default=_default, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return md
