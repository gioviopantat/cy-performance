"""Email copy for the reports: subject, plain text and HTML (docs/specs/ride-report.md).

The voice is cy-performance's coach team: addressed to the recipient by name, a one-line
verdict, up to three highlights, what happens tomorrow, a call to rate the ride, and a footer
saying who the email is for. Every sentence comes from ``i18n/email.zh-TW.yaml``; every number
comes from the report contract (docs/07: nothing computed here beyond formatting). Variants
rotate by activity id, so a resend is identical.
"""

from __future__ import annotations

import datetime as dt
import html
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from cyp.planning.adapt import OVERDONE_FACTOR
from cyp.reports.digest_model import MonthlyDigest, WeeklyDigest
from cyp.reports.ride_model import RideReport

COPY_PATH = Path(__file__).resolve().parent / "i18n" / "email.zh-TW.yaml"
#: At most this many highlight bullets.
MAX_HIGHLIGHTS = 3
#: A climb is a highlight when it ranks this high among its ascents.
CLIMB_HIGHLIGHT_RANK = 2
#: A feel gap (RPE vs expected) this large is a highlight.
FEEL_HIGHLIGHT_GAP = 1.5


@dataclass(frozen=True)
class EmailContent:
    """What the mailer sends: a subject, a plain-text part and an HTML part."""

    subject: str
    text: str
    html: str


@dataclass
class _Body:
    """The email as blocks, rendered once as text and once as HTML."""

    hello: str
    paragraphs: list[str] = field(default_factory=list)
    bullets_title: str = ""
    bullets: list[str] = field(default_factory=list)
    sections: list[tuple[str, str]] = field(default_factory=list)
    cta: tuple[str, str] | None = None  # (text, url)
    closing: list[str] = field(default_factory=list)
    footer: str = ""


@cache
def _copy() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COPY_PATH.read_text(encoding="utf-8"))
    return data


def _pick(options: str | Sequence[str], seed: int) -> str:
    return options if isinstance(options, str) else options[seed % len(options)]


def _md(day: dt.date) -> str:
    return f"{day.month}/{day.day}"


def _num(v: float, digits: int = 0) -> str:
    return f"{v:.{digits}f}"


def _part_of_day(hour: int) -> str:
    if hour < 11:
        return "morning"
    if hour < 14:
        return "noon"
    if hour < 18:
        return "afternoon"
    return "evening"


def _case(r: RideReport) -> str:
    i = r.intent
    if i is None:
        return "unplanned" if r.ride.np_w is not None or r.ride.tss is not None else "no_data"
    if (
        i.verdict == "missed"
        and i.kind == "ceiling"
        and i.load_ratio is not None
        and i.load_ratio > OVERDONE_FACTOR
    ):
        return "overdone"
    return i.verdict if i.verdict in ("achieved", "partial", "missed") else "no_data"


def _highlights(r: RideReport, case: str, c: dict[str, Any]) -> list[str]:
    h = c["highlight"]
    out: list[str] = []
    if r.intent is not None:
        out.append(h["intent"].format(verdict=c["verdict"][case]))
    o = r.outdoor
    if o is not None and not r.ride.indoor:
        temp = h["outdoor_temp"].format(temp=_num(o.temp_avg_c)) if o.temp_avg_c is not None else ""
        caveat = h["outdoor_caveat"] if o.caveats_zh else ""
        out.append(h["outdoor"].format(stops=o.stops_n, temp=temp, caveat=caveat))
    ranked = [x for x in r.climbs if x.rank is not None and x.rank <= CLIMB_HIGHLIGHT_RANK]
    if ranked:
        best = min(ranked, key=lambda x: x.rank or 0)
        temp = (
            h["climb_temp"].format(temp=_num(best.this.temp_c))
            if best.this.temp_c is not None
            else ""
        )
        out.append(h["climb"].format(climb=best.name, of_n=best.of_n, rank=best.rank, temp=temp))
    f = r.feel
    if f.rated and f.gap is not None and f.rpe is not None and abs(f.gap) >= FEEL_HIGHLIGHT_GAP:
        key = "feel_high" if f.gap > 0 else "feel_low"
        out.append(h[key].format(rpe=_num(f.rpe), gap=_num(abs(f.gap), 1)))
    g = r.goal
    if g is not None and g.projected_date is not None:
        out.append(h["goal"].format(target=_num(g.target_w), date=g.projected_date.isoformat()))
    return out[:MAX_HIGHLIGHTS]


def compose_ride(
    report: RideReport, *, to_name: str, cc_names: Sequence[str] = (), today: dt.date
) -> EmailContent:
    """The email for one ride report."""
    c = _copy()["ride"]
    r = report
    seed = r.ride.activity_id
    case = _case(r)
    day = r.ride.start_local.date()
    planned = r.intent.planned_name if r.intent is not None else ""
    km = _num(r.ride.distance_km, 1) if r.ride.distance_km is not None else "–"
    subject = c["subject"][case].format(
        md=_md(day),
        name=r.ride.name,
        planned=planned,
        tss=r.ride.tss if r.ride.tss is not None else "–",
        km=km,
    )

    part = c["part"][_part_of_day(r.ride.start_local.hour)]
    when = (
        c["when_today"].format(part=part)
        if day == today
        else c["when_day"].format(md=_md(day), part=part)
    )
    facts = "、".join(
        x
        for x in (
            f"{km} km" if r.ride.distance_km is not None else "",
            f"{r.ride.moving_min} 分鐘",
        )
        if x
    )
    why = ""
    if r.intent is not None and r.intent.explanation:
        why = str(r.intent.explanation.get("headline_zh", ""))
        why = f"（{why}）" if why else ""
    ratio = f"{r.intent.load_ratio:.0%}" if r.intent is not None and r.intent.load_ratio else "–"
    body = _Body(hello=c["hello"].format(to=to_name))
    body.paragraphs += [
        c["opener"].format(when=when, facts=facts),
        _pick(c["conclusion"][case], seed).format(planned=planned, ratio=ratio, why=why),
    ]
    bullets = _highlights(r, case, c)
    if bullets:
        body.bullets_title, body.bullets = c["highlights_title"], bullets

    t = r.tomorrow
    if t is not None and t.after_name:
        if t.changed and t.before_name:
            line = (
                c["tomorrow_changed"].format(
                    reason=t.reasons_zh[0], before=t.before_name, after=t.after_name
                )
                if t.reasons_zh
                else c["tomorrow_changed_no_reason"].format(
                    before=t.before_name, after=t.after_name
                )
            )
            if case == "overdone":
                line += c["tomorrow_easy"]
        else:
            line = c["tomorrow_same"].format(after=t.after_name)
        body.sections.append((c["tomorrow_title"], line))

    if not r.feel.rated and r.feel.rate_url:
        body.cta = (c["rate"].format(url="").rstrip("：: "), r.feel.rate_url)
    body.closing = [c["attachment"], "", c["signoff"], c["team"]]
    cc = c["footer_cc"].format(names="、".join(cc_names)) if cc_names else ""
    body.footer = c["footer"].format(to=to_name, cc=cc) + (
        c["footer_tomorrow"] if body.sections else ""
    )
    return EmailContent(subject=subject, text=_text(body), html=_html(subject, body))


def compose_weekly(
    digest: WeeklyDigest, *, to_name: str, cc_names: Sequence[str] = (), today: dt.date
) -> EmailContent:
    """The email for a weekly digest (docs/specs/digest-reports.md)."""
    raise NotImplementedError


def compose_monthly(
    digest: MonthlyDigest, *, to_name: str, cc_names: Sequence[str] = (), today: dt.date
) -> EmailContent:
    """The email for a monthly digest (docs/specs/digest-reports.md)."""
    raise NotImplementedError


def _text(b: _Body) -> str:
    lines = [b.hello, ""]
    for p in b.paragraphs:
        lines += [p]
    if b.bullets:
        lines += ["", b.bullets_title, *[f"• {x}" for x in b.bullets]]
    for title, text in b.sections:
        lines += ["", title, text]
    if b.cta:
        lines += ["", f"{b.cta[0]}：{b.cta[1]}"]
    lines += ["", *b.closing, "—", b.footer]
    return "\n".join(lines).strip() + "\n"


def _html(subject: str, b: _Body) -> str:
    e = html.escape
    parts = [
        f"<p style='margin:0 0 16px'>{e(b.hello)}</p>",
        *[f"<p style='margin:0 0 12px'>{e(p)}</p>" for p in b.paragraphs],
    ]
    if b.bullets:
        items = "".join(f"<li style='margin:4px 0'>{e(x)}</li>" for x in b.bullets)
        parts.append(
            f"<h3 style='margin:20px 0 8px;font-size:16px'>{e(b.bullets_title)}</h3>"
            f"<ul style='margin:0;padding-left:20px'>{items}</ul>"
        )
    for title, text in b.sections:
        parts.append(
            f"<h3 style='margin:20px 0 8px;font-size:16px'>{e(title)}</h3>"
            f"<p style='margin:0'>{e(text)}</p>"
        )
    if b.cta:
        parts.append(
            f"<p style='margin:20px 0'><a href='{e(b.cta[1])}' style='display:inline-block;"
            "padding:10px 16px;background:#1f6feb;color:#fff;border-radius:6px;"
            f"text-decoration:none'>{e(b.cta[0])}</a></p>"
        )
    closing = "<br>".join(e(x) for x in b.closing if x) if b.closing else ""
    parts.append(f"<p style='margin:20px 0 0'>{closing}</p>")
    parts.append(f"<p style='margin:24px 0 0;color:#6e7781;font-size:12px'>{e(b.footer)}</p>")
    return (
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        f"<title>{e(subject)}</title></head>"
        "<body style='margin:0;padding:24px;background:#f6f8fa'>"
        "<div style='max-width:560px;margin:0 auto;padding:24px;background:#fff;"
        'border-radius:8px;font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif;'
        "font-size:15px;line-height:1.6;color:#1f2328'>" + "".join(parts) + "</div></body></html>"
    )
