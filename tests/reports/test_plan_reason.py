"""The weekly digest's plan-reason section fed by the progression gate (ADR-0018)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cyp.core.explain import Explanation, Reason
from cyp.planning.gate import GateDecision, decision_zh
from cyp.reports import digest_pdf, email_copy
from cyp.reports.digest_model import WeeklyDigest
from cyp.reports.digest_weekly import build_plan_reason

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MON = dt.date(2026, 10, 19)


def _gate() -> GateDecision:
    expl = Explanation(
        key=f"plan.gate.{MON}",
        headline_zh=f"{MON} {decision_zh('down')}",
        because=[Reason(text_zh="reason one"), Reason(text_zh="reason two")],
        confidence="medium",
    )
    return GateDecision(MON, "down", 2, 3, tss_factor=0.85, explanation=expl)


def _weekly() -> WeeklyDigest:
    raw = (FIXTURES / "digest_weekly.sample.json").read_text(encoding="utf-8")
    return WeeklyDigest.model_validate_json(raw)


def test_section_quotes_the_gate_explanation() -> None:
    assert build_plan_reason(None) is None
    r = build_plan_reason(_gate())
    assert r is not None and r.decision == "down"
    assert r.headline_zh == _gate().explanation.headline_zh  # type: ignore[union-attr]
    assert r.reasons_zh == ["reason one", "reason two"]
    assert r.explanation is not None and r.explanation["key"] == f"plan.gate.{MON}"


def test_pdf_and_email_show_it_only_when_present() -> None:
    plain = _weekly()
    assert plain.plan_reason is None
    assert "plan-reason" not in digest_pdf.render_html(plain)
    with_reason = plain.model_copy(update={"plan_reason": build_plan_reason(_gate())})
    html = digest_pdf.render_html(with_reason)
    assert "plan-reason" in html and "reason two" in html
    headline = with_reason.plan_reason.headline_zh  # type: ignore[union-attr]
    assert headline in html
    mail = email_copy.compose_weekly(with_reason, to_name="Rider", today=MON)
    assert headline in mail.text and headline in mail.html
    assert headline not in email_copy.compose_weekly(plain, to_name="Rider", today=MON).text
