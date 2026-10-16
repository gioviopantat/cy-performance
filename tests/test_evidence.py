"""Evidence grades (docs/07 §6, docs/specs/research-review.md).

The registry and the glossary evidence-grade tables agree; every claim id used in code exists;
a verdict (explanation headline or report warning) resting on a grade-C claim is always shown
as weak-evidence context, never as a plain verdict; ACWR / monotony read as context, not risk.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

import pytest

from cyp.analysis import readiness as rd
from cyp.analysis.longitudinal import pdc, pmc
from cyp.core import evidence
from cyp.core.explain import Explanation, MethodRef
from cyp.reports import facts as facts_mod
from cyp.reports.render import WEAK_BADGE_ZH, render

ROOT = Path(__file__).resolve().parents[1]
GLOSSARY = ROOT / "docs" / "glossary"
#: Glossary section heading and the proposed-claim marker (athlete-facing, zh-TW).
SECTION_ZH = "證據等級"
PROPOSED_ZH = "（提議）"
ROW = re.compile(
    rf"^\| `([\w.]+)` .+ \| \*\*([ABC])\*\*({PROPOSED_ZH})? \| .+ \| (\d{{4}}-\d{{2}}-\d{{2}}) \|$",
    re.M,
)
#: Fitness values that trip every report warning.
HOT = {"acwr": 1.62, "ramp_rate": 7.4, "tsb": -34.0, "monotony": 2.41}


def _section(text: str) -> str:
    head = f"\n## {SECTION_ZH}\n"
    assert head in text
    return text.split(head, 1)[1].split("\n## ", 1)[0]


# ----------------------------------------------------------------------------- registry


def test_registry_ids_unique_and_well_formed() -> None:
    ids = [c.id for c in evidence.REGISTRY]
    assert len(ids) == len(set(ids))
    for c in evidence.REGISTRY:
        assert c.glossary is None or c.id.startswith(c.glossary + "."), c.id
        assert c.grade in ("A", "B", "C") and c.status in ("accepted", "proposed")
        assert c.glossary is not None or c.status == "proposed", c.id
        assert c.citations and all(s.strip() for s in c.citations), c.id
        assert c.claim.strip() and c.reviewed <= dt.date.today()


def test_every_glossary_file_has_a_matching_grade_table() -> None:
    files = sorted(p for p in GLOSSARY.glob("*.md") if p.name != "README.md")
    assert {p.stem for p in files} == {c.glossary for c in evidence.REGISTRY if c.glossary}
    for p in files:
        rows = ROW.findall(_section(p.read_text(encoding="utf-8")))
        want = [
            (c.id, c.grade, PROPOSED_ZH if c.status == "proposed" else "", c.reviewed.isoformat())
            for c in evidence.for_glossary(p.stem)
        ]
        assert rows == want, f"{p.name}: evidence-grade table and core/evidence.py disagree"


def test_lookup_helpers() -> None:
    assert evidence.grade("cp_wprime.cp_boundary") == "A"
    assert evidence.is_weak("cp_wprime.wbal_prediction")
    assert not evidence.is_weak(None)
    assert evidence.verdict_allowed(["cp_wprime.wbal_prediction", "cp_wprime.cp_boundary"])
    assert not evidence.verdict_allowed(["cp_wprime.wbal_prediction"])
    assert not evidence.verdict_allowed([])
    with pytest.raises(KeyError):
        evidence.grade("cp_wprime.typo")


def test_every_claim_id_used_in_src_exists() -> None:
    prefixes = "|".join(sorted({c.id.split(".")[0] for c in evidence.REGISTRY}))
    used = re.compile(rf"""["']((?:{prefixes})\.[a-z][a-z0-9_]*)["']""")
    found: set[str] = set()
    for p in (ROOT / "src" / "cyp").rglob("*"):
        if p.suffix in (".py", ".j2") and p.name != "evidence.py":
            found |= set(used.findall(p.read_text(encoding="utf-8")))
    assert found, "expected claim ids in reports"
    assert found <= set(evidence.CLAIMS), found - set(evidence.CLAIMS)


# ----------------------------------------------------------------------------- MethodRef


def test_method_ref_claim_is_optional_and_old_json_loads() -> None:
    old = {"model_id": "banister_pmc", "version": "1", "inputs": {}, "doc": "x.md"}
    assert MethodRef.model_validate(old).claim is None
    ref = MethodRef(**old, claim="banister_pmc.load_history")
    assert MethodRef.model_validate(ref.model_dump(mode="json")) == ref


def test_explanation_headline_badged_when_claim_is_weak() -> None:
    def expl(claim: str) -> dict[str, Any]:
        return Explanation(
            key="k",
            headline_zh="頭條",
            method=MethodRef(model_id="m", version="1", doc="d", claim=claim),
            confidence="low",
        ).to_json_dict()

    weak = render(
        "daily",
        _daily({"ftp_proposal": {"x": 1}, "ftp_proposal_explanation": expl("eftp.icu_value")}),
    )
    strong = render(
        "daily",
        _daily({"ftp_proposal": {"x": 1}, "ftp_proposal_explanation": expl("eftp.ftp_mlss")}),
    )
    assert f"頭條（{WEAK_BADGE_ZH}）" in weak
    assert WEAK_BADGE_ZH not in strong.split("## 5.")[1]


# ----------------------------------------------------------------------------- warnings


def _real_explanations() -> list[Explanation]:
    """Explanations built by the real analysis code from synthetic inputs."""
    day = dt.date(2026, 10, 2)
    hist = [
        rd.WellnessPoint(day - dt.timedelta(days=30 - i), hrv=60.0 + (i % 2), resting_hr=48)
        for i in range(30)
    ]
    readiness = rd.compute_readiness(
        rd.ReadinessInputs(day, rd.WellnessPoint(day, hrv=60.0, resting_hr=48), hist, tsb=0.0)
    )
    loads = {day - dt.timedelta(days=i): 60.0 for i in range(20)}
    series = pmc.replay(pmc.PMCState(day - dt.timedelta(days=20), 40, 40), loads, day)
    fit = pdc.fit_cp_2p({t: 270.0 + 20000.0 / t for t in (120, 180, 300, 600, 1200)})
    assert fit is not None
    est = {day - dt.timedelta(days=i): 278.0 for i in range(20)}
    prop = pdc.ftp_proposal(265.0, est, as_of=day, sources=["icu_eftp"])
    out = [
        readiness.explanation,
        pmc.explain_pmc(series[-1], None),
        pdc.explain_fit(fit, pdc.compare_with_icu(fit, None), as_of=day),
        prop.explanation,
    ]
    return [e for e in out if e is not None]


def test_real_explanations_name_known_claims_and_weak_ones_are_badged() -> None:
    expls = _real_explanations()
    assert len(expls) == 4
    seen = set()
    for e in expls:
        assert e.method is not None and e.method.claim in evidence.CLAIMS, e.key
        seen.add(evidence.is_weak(e.method.claim))
        md = render(
            "daily",
            _daily({"ftp_proposal": {"x": 1}, "ftp_proposal_explanation": e.to_json_dict()}),
        )
        headline = next(line for line in md.splitlines() if e.headline_zh in line)
        # A verdict resting on a grade-C claim must never read as a plain verdict.
        assert (WEAK_BADGE_ZH in headline) == evidence.is_weak(e.method.claim), e.key
    assert seen == {True, False}  # both paths exercised


def test_report_warnings_name_known_claims_and_weak_ones_are_not_verdicts() -> None:
    warnings = facts_mod._warnings(HOT, None)
    assert len(warnings) == 4
    md = render("daily", _daily())
    for w in warnings:
        assert w["claim"] in evidence.CLAIMS
        line = next(x for x in md.splitlines() if w["text_zh"] in x)
        if evidence.is_weak(w["claim"]):
            assert line.startswith(f"- {WEAK_BADGE_ZH}：") and "⚠️" not in line
        else:
            assert line.startswith("- ⚠️ ")


def test_acwr_warning_no_longer_claims_injury_risk() -> None:
    high = facts_mod._warnings({"acwr": 1.62}, None)
    low = facts_mod._warnings({"acwr": 0.61}, None)
    assert "ACWR 1.62 > 1.3" in high[0]["text_zh"]
    assert "ACWR 0.61 < 0.8" in low[0]["text_zh"]
    assert high[0]["claim"] == "ramp_rate_acwr_monotony.acwr_injury"
    assert low[0]["claim"] == "ramp_rate_acwr_monotony.acwr_low"
    for w in high + low:
        assert "受傷風險上升" not in w["text_zh"] and "體能會掉" not in w["text_zh"]
        assert w["weak"]
    assert facts_mod._warnings({"tsb": -40.0}, None)[0]["claim"] == "guardrails.tsb_floor"
    mono = facts_mod._warnings({"monotony": 2.41}, None)
    assert "單調度 2.41 > 2.0" in mono[0]["text_zh"] and mono[0]["weak"]


def _fitness() -> dict[str, Any]:
    return {"ctl": 60.0, "atl": 80.0, "source": "icu", **HOT}


def _daily(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    fit = _fitness()
    return {
        "kind": "daily",
        "date": dt.date(2026, 10, 16),
        "yesterday": dt.date(2026, 10, 15),
        "season_week": None,
        "ftp": 250,
        "goal": None,
        "activities": [],
        "readiness": None,
        "fitness": fit,
        "warnings": facts_mod._warnings(fit, None),
        "plan": [],
        "ftp_proposal": None,
        "ftp_proposal_explanation": None,
        "trends_as_of": None,
        **(extra or {}),
    }


def test_daily_report_badges_acwr_and_monotony() -> None:
    md = render("daily", _daily())
    assert f"ACWR · {WEAK_BADGE_ZH}" in md
    assert f"單調度 · {WEAK_BADGE_ZH}" in md
    assert f"- {WEAK_BADGE_ZH}：ACWR 1.62 > 1.3" in md
    assert "⚠️ ACWR" not in md and "受傷風險上升" not in md
    assert "1.62" in md and "2.41" in md  # numbers kept


def test_weekly_report_badges_acwr() -> None:
    fit = _fitness()
    md = render(
        "weekly",
        {
            "kind": "weekly",
            "week_start": dt.date(2026, 10, 12),
            "week_end": dt.date(2026, 10, 18),
            "iso_week": dt.date(2026, 10, 12).isocalendar(),
            "season_week": None,
            "ftp": 250,
            "goal": None,
            "days": [],
            "totals": {
                "hours": 8.0,
                "load": 500,
                "planned_load": None,
                "n_rides": 5,
                "budget_hours": None,
            },
            "tid": None,
            "fitness_start": fit,
            "fitness_end": fit,
            "warnings": facts_mod._warnings(fit, None),
            "cp_fits": {},
            "ftp_proposal": None,
            "ftp_proposal_explanation": None,
            "durability": [],
            "durability_explanation": None,
            "limiters": [],
            "planner_bias": {},
            "climbs": [],
            "trends_as_of": None,
        },
    )
    assert f"ACWR 1.62（{WEAK_BADGE_ZH}）" in md
    assert f"- {WEAK_BADGE_ZH}：單調度 2.41 > 2.0" in md
    assert "⚠️" not in md
