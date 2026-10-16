"""Evidence grades: one registry of the claims we rely on (docs/specs/research-review.md).

Every scientific claim a report or the planner leans on is a :class:`Claim` in :data:`REGISTRY`,
graded per claim (not per model):

- **A**: meta-analyses / several controlled studies agree, or strong physiological consensus.
- **B**: supported at group level; individual error is material.
- **C**: practitioner heuristic, proprietary formula, single small study, team-sport data only,
  or contested. Shown as context (with the weak-evidence badge), never the sole basis of a verdict.

Grades are data: the monthly research review proposes changes, a human approves them, and the
change lands here together with the evidence-grade table of the glossary file (a test checks
they agree). Code refers to a claim by id, e.g. ``MethodRef(claim="banister_pmc.load_history")``
or a report warning's ``claim``; unknown ids raise.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Grade = Literal["A", "B", "C"]

#: Grades that may drive a verdict or warning on their own.
VERDICT_GRADES: frozenset[str] = frozenset({"A", "B"})

#: Date of the initial review (docs/specs/research-review.md "Proposed grades").
INITIAL_REVIEW = dt.date(2026, 10, 16)


@dataclass(frozen=True)
class Claim:
    """One claim we rely on, its grade and the key evidence behind it."""

    id: str
    glossary: str
    claim: str
    grade: Grade
    citations: tuple[str, ...]
    reviewed: dt.date = INITIAL_REVIEW


REGISTRY: tuple[Claim, ...] = (
    # --- cp_wprime
    Claim(
        "cp_wprime.cp_boundary",
        "cp_wprime",
        "CP marks the heavy/severe boundary; P(t) = CP + W'/t holds over 2-15 min.",
        "A",
        (
            "Jones AM et al. 2010, Med Sci Sports Exerc (review)",
            "Monod H, Scherrer J 1965, Ergonomics",
        ),
    ),
    Claim(
        "cp_wprime.wbal_prediction",
        "cp_wprime",
        "W'bal (Skiba 2012 single-exponential) predicts which interval rep fails.",
        "C",
        (
            "Chorley A et al., Eur J Appl Physiol (2021 or 2022, year to confirm): "
            "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8854279/",
            "Skiba PF, Clarke DC 2021 (cited via https://arxiv.org/pdf/2108.04510, unverified)",
        ),
    ),
    # --- coggan_np_if_tss
    Claim(
        "coggan_np_if_tss.tss_dose",
        "coggan_np_if_tss",
        "TSS is a usable dose for fitness change.",
        "B",
        ("Sanders D et al. 2017, Int J Sports Physiol Perform 12(5):668-675",),
    ),
    Claim(
        "coggan_np_if_tss.np_fourth_power",
        "coggan_np_if_tss",
        "NP's 4th-power weighting reflects metabolic cost.",
        "C",
        ("No peer-reviewed validation found (Coggan, practitioner formula)",),
    ),
    # --- banister_pmc
    Claim(
        "banister_pmc.load_history",
        "banister_pmc",
        "CTL/ATL describe the athlete's load history.",
        "B",
        (
            "Banister EW et al. 1975, Aust J Sports Med",
            "Sanders D et al. 2017, Int J Sports Physiol Perform 12(5):668-675",
        ),
    ),
    Claim(
        "banister_pmc.tsb_performance",
        "banister_pmc",
        "TSB predicts individual performance on a given day.",
        "C",
        ("Marchal et al. 2025, Sci Rep (abstract only): https://hal.inrae.fr/hal-04999009v1",),
    ),
    # --- ramp_rate_acwr_monotony
    Claim(
        "ramp_rate_acwr_monotony.acwr_injury",
        "ramp_rate_acwr_monotony",
        "ACWR > 1.3 means higher injury risk.",
        "C",
        (
            "Impellizzeri FM et al. 2020, Int J Sports Physiol Perform 15(6):907-913",
            "Wik EH et al. 2020, Sports Med",
            "2025 meta-analysis: https://pubmed.ncbi.nlm.nih.gov/41029871/ (PMID 41029871)",
        ),
    ),
    Claim(
        "ramp_rate_acwr_monotony.monotony_overtraining",
        "ramp_rate_acwr_monotony",
        "Monotony / strain (Foster 1998) flag overtraining.",
        "C",
        ("Foster C 1998, Med Sci Sports Exerc 30(7):1164-1168 (single study)",),
    ),
    Claim(
        "ramp_rate_acwr_monotony.ramp_cap",
        "ramp_rate_acwr_monotony",
        "A per-week CTL ramp cap keeps load increases safe.",
        "C",
        ("Practitioner heuristic",),
    ),
    # --- eftp
    Claim(
        "eftp.ftp_mlss",
        "eftp",
        "FTP approximates MLSS (the threshold our zones hang on).",
        "B",
        (
            "Borszcz FK et al. 2019, Int J Sports Physiol Perform",
            "Lillo-Bevia JR et al. 2022, J Strength Cond Res (abstract only)",
        ),
    ),
    Claim(
        "eftp.icu_value",
        "eftp",
        "The intervals.icu eFTP value estimates FTP.",
        "C",
        ("Proprietary; no published validation",),
    ),
    # --- decoupling_hr_lag
    Claim(
        "decoupling_hr_lag.durability",
        "decoupling_hr_lag",
        "Durability (power/HR decay with accumulated work) matters for performance.",
        "B",
        (
            "Maunder E et al. 2021, Sports Med",
            "2025 systematic review (abstract only): "
            "https://link.springer.com/article/10.1007/s00421-025-05885-0",
        ),
    ),
    Claim(
        "decoupling_hr_lag.pwhr_5pct",
        "decoupling_hr_lag",
        "Pw:Hr decoupling < 5 % means aerobically ready.",
        "C",
        ("Friel J, practitioner heuristic (book/blog)",),
    ),
    # --- efficiency_factor
    Claim(
        "efficiency_factor.z2_trend",
        "efficiency_factor",
        "The EF trend within Z2 rides tracks aerobic base.",
        "C",
        ("Practitioner metric; heat and sleep confound it",),
    ),
    # --- time_in_zone_tid
    Claim(
        "time_in_zone_tid.low_volume",
        "time_in_zone_tid",
        "Most training volume should be at low intensity.",
        "A",
        (
            "Seiler S 2010, Int J Sports Physiol Perform",
            "Seiler S, Kjerland GO 2006, Scand J Med Sci Sports",
        ),
    ),
    Claim(
        "time_in_zone_tid.polarized_superior",
        "time_in_zone_tid",
        "Polarised beats pyramidal intensity distribution (evidence points against it).",
        "C",
        (
            "Oliveira PS et al. 2024, Sports Med (meta-analysis): "
            "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11329428/",
            "Second 2024 meta-analysis, 13 studies (secondary summary, unverified)",
        ),
    ),
    # --- readiness_v1
    Claim(
        "readiness_v1.subjective_wellness",
        "readiness_v1",
        "Subjective wellness tracks the training response.",
        "B",
        ("Saw AE et al. 2016, Br J Sports Med (systematic review)",),
    ),
    Claim(
        "readiness_v1.hrv_guided",
        "readiness_v1",
        "HRV-guided training beats a fixed plan (mixed evidence).",
        "B",
        (
            "Granero-Gallegos A et al. 2020, Int J Environ Res Public Health: "
            "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7663087/",
            "2021 J Sci Med Sport review, 8 studies (abstract only)",
        ),
    ),
    Claim(
        "readiness_v1.weights",
        "readiness_v1",
        "Our v1 readiness weights and cut-offs.",
        "C",
        ("Ours; a starting point (readiness_v1 glossary)",),
    ),
    # --- periodization_3_1
    Claim(
        "periodization_3_1.block",
        "periodization_3_1",
        "Block organisation beats traditional organisation.",
        "B",
        ("Molmen KS et al. 2019, Open Access J Sports Med (meta-analysis)",),
    ),
    Claim(
        "periodization_3_1.ratio_3_1",
        "periodization_3_1",
        "3:1 loading specifically (rather than 2:1).",
        "C",
        ("Convention (Friel); no comparative trial found",),
    ),
    Claim(
        "periodization_3_1.taper_volume",
        "periodization_3_1",
        "Recovery weeks / tapers cut volume 40-60 % and keep intensity.",
        "A",
        ("Bosquet L et al. 2007, Med Sci Sports Exerc (meta-analysis)",),
    ),
    # --- guardrails
    Claim(
        "guardrails.hit_spacing",
        "guardrails",
        "At least 48 h between hard sessions.",
        "B",
        ("Seiler S et al. 2007, Med Sci Sports Exerc (small n)",),
    ),
    # --- ftp_target_season
    Claim(
        "ftp_target_season.intervals_4_8",
        "ftp_target_season",
        "4-8 min intervals develop threshold / VO2max.",
        "B",
        (
            "Seiler S et al. 2013, Scand J Med Sci Sports",
            "Ronnestad BR et al. 2020, Scand J Med Sci Sports",
        ),
    ),
    Claim(
        "ftp_target_season.gain_20pct",
        "ftp_target_season",
        "+20 % FTP in 26 weeks is reachable.",
        "C",
        ("Atypical; trained riders usually gain 5-10 % per season",),
    ),
    # --- workout_library
    Claim(
        "workout_library.ronnestad_30_15",
        "workout_library",
        "Ronnestad 30/15 short intervals.",
        "B",
        ("Ronnestad BR et al. 2020, Scand J Med Sci Sports (elite cyclists)",),
    ),
    Claim(
        "workout_library.template_structure",
        "workout_library",
        "The other templates' specific structure.",
        "C",
        ("Practitioner design",),
    ),
)

CLAIMS: dict[str, Claim] = {c.id: c for c in REGISTRY}


def get(claim_id: str) -> Claim:
    """The registered claim.

    Raises:
        KeyError: unknown id (a typo must fail loudly).
    """
    if claim_id not in CLAIMS:
        raise KeyError(f"unknown evidence claim {claim_id!r}")
    return CLAIMS[claim_id]


def grade(claim_id: str) -> Grade:
    """Grade of ``claim_id`` (raises ``KeyError`` for an unknown id)."""
    return get(claim_id).grade


def is_weak(claim_id: str | None) -> bool:
    """Whether ``claim_id`` is grade C (``None`` = no claim attached = not weak)."""
    return claim_id is not None and grade(claim_id) not in VERDICT_GRADES


def for_glossary(glossary_id: str) -> tuple[Claim, ...]:
    """Claims documented in ``docs/glossary/<glossary_id>.md``, registry order."""
    return tuple(c for c in REGISTRY if c.glossary == glossary_id)


def verdict_allowed(claim_ids: Iterable[str]) -> bool:
    """A verdict or warning may rest on these claims only if at least one is grade A/B."""
    return any(grade(c) in VERDICT_GRADES for c in claim_ids)
