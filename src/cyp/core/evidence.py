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
Status = Literal["accepted", "proposed"]

#: Grades that may drive a verdict or warning on their own.
VERDICT_GRADES: frozenset[str] = frozenset({"A", "B"})

#: Date of the initial review (docs/specs/research-review.md "Proposed grades",
#: docs/research/2026-10.md).
INITIAL_REVIEW = dt.date(2026, 10, 16)


@dataclass(frozen=True)
class Claim:
    """One claim we rely on, its grade and the key evidence behind it.

    ``glossary`` is ``None`` for a model that has no glossary page yet. ``status`` is
    ``"proposed"`` until a human approves the claim (the monthly review adds claims that way);
    a proposed claim is still graded and used, so the badge is honest from day one.
    """

    id: str
    glossary: str | None
    claim: str
    grade: Grade
    citations: tuple[str, ...]
    reviewed: dt.date = INITIAL_REVIEW
    status: Status = "accepted"


def _proposed(
    claim_id: str, glossary: str | None, claim: str, grade: Grade, *citations: str
) -> Claim:
    return Claim(claim_id, glossary, claim, grade, tuple(citations), status="proposed")


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
            "Chorley A, Bott RP, Marwood S, Lamb KL 2022, Eur J Appl Physiol 122(3):677-689, "
            "doi:10.1007/s00421-021-04874-3, https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8854279/",
            "Skiba PF, Clarke DC 2021, Int J Sports Physiol Perform 16(11):1561-1572, "
            "PMID 34686611",
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
        (
            "Marchal A et al. 2025, Sci Rep 15:3706, doi:10.1038/s41598-025-88153-7, "
            "PMC11779798 (speed skaters, not cyclists)",
        ),
    ),
    # --- ramp_rate_acwr_monotony
    Claim(
        "ramp_rate_acwr_monotony.acwr_injury",
        "ramp_rate_acwr_monotony",
        "ACWR > 1.3 means higher injury risk.",
        "C",
        (
            "Impellizzeri FM et al. 2020, Int J Sports Physiol Perform 15(6):907-913",
            "Andrade R, Wik EH et al. 2020, Sports Med 50(9):1613-1635, PMID 32572824",
            "Qin W, Li R, Chen L 2025, BMC Sports Sci Med Rehabil 17(1):285, PMID 41029871",
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
            "Borszcz FK et al. 2019, Int J Sports Physiol Perform 14(8):1029-1035, PMID 30676826",
            "Lillo-Bevia JR et al. 2022, J Strength Cond Res 36(1):167-173, PMID 31714462",
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
        "A",
        (
            "Maunder E et al. 2021, Sports Med 51(8):1619-1628, PMID 33886100",
            "Sanchez-Jimenez JL et al. 2026 (online 2025), Eur J Appl Physiol 126:163-179, "
            "doi:10.1007/s00421-025-05885-0, PMID 40613880",
            "Mateo-March M et al. 2024, J Sci Med Sport 27(6):430-434, PMID 38604818",
            "Grade reviewed in docs/research/2026-10.md, approved 2026-10-16",
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
            "Oliveira PS, Boppre G, Fonseca H 2024, Sports Med 54(8):2071-2095, "
            "doi:10.1007/s40279-024-02034-z, PMC11329428",
            "Rosenblat MA et al. 2025, Sports Med 55(3):655-673, PMID 39888556",
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
        "HRV-guided training beats a fixed plan (meta-analyses: no significant advantage).",
        "C",
        (
            "Granero-Gallegos A et al. 2020, Int J Environ Res Public Health: "
            "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7663087/",
            "Duking P et al. 2021, J Sci Med Sport 24(11):1180-1192, PMID 34489178",
            "Manresa-Rocamora A et al. 2021, Int J Environ Res Public Health 18(19):10299, "
            "PMC8507742",
            "Grade reviewed in docs/research/2026-10.md, approved 2026-10-16",
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
        "C",
        (
            "Molmen KS et al. 2019, Open Access J Sports Med 10:145-160, PMID 31802956 "
            "(6 studies pooled, low quality)",
            "Grade reviewed in docs/research/2026-10.md, approved 2026-10-16",
        ),
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
        "A pre-race taper cuts volume 41-60 % and keeps intensity and frequency.",
        "A",
        (
            "Bosquet L et al. 2007, Med Sci Sports Exerc 39(8):1358-1365, PMID 17762369",
            "Wang Z et al. 2023, PLoS One 18(5):e0282838, PMC10171681",
            "Grade reviewed in docs/research/2026-10.md, approved 2026-10-16",
        ),
    ),
    Claim(
        "periodization_3_1.recovery_week_cut",
        "periodization_3_1",
        "Mid-block recovery weeks cut TSS to 0.6x, long ride 0.7x, one hard session fewer.",
        "C",
        (
            "Extrapolated from taper studies (Bosquet L et al. 2007, Med Sci Sports Exerc, "
            "PMID 17762369), which keep frequency and intensity",
            "Grade reviewed in docs/research/2026-10.md, approved 2026-10-16",
        ),
    ),
    # --- guardrails
    Claim(
        "guardrails.hit_spacing",
        "guardrails",
        "At least 48 h between hard sessions.",
        "B",
        ("Stanley J, Peake JM, Buchheit M 2013, Sports Med 43(12):1259-1277, PMID 23912805",),
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
    # --- proposed by the 2026-10 review (docs/research/2026-10.md §3), awaiting approval
    _proposed(
        "decoupling_hr_lag.durability_kj_buckets",
        "decoupling_hr_lag",
        "Efficiency after 1000 kJ of absolute work tracks durability (cut-offs are ours).",
        "B",
        "Barsumyan A et al. 2025, BMC Sports Sci Med Rehabil 17:192, PMID 40640875",
        "Mateo-March M et al. 2024, J Sci Med Sport 27(6):430-434, PMID 38604818",
    ),
    _proposed(
        "decoupling_hr_lag.ride_status_rules",
        "decoupling_hr_lag",
        "The ride's TSS alone sets the post-ride status and the next day (>= 150 easy, "
        ">= 250 overreached, >= 300 rest); decoupling and HR lag never do (ADR-0012).",
        "C",
        "Roete AJ et al. 2021, Int J Sports Physiol Perform 16(8):1065-1073, PMID 34108275 "
        "(overreaching lowers HR at a given power, so high decoupling cannot mean overreached)",
        "Le Meur Y et al. 2013, Med Sci Sports Exerc 45(11):2061-2071, PMID 24136138",
        "TSS cut-offs are ours (no study links one ride's TSS to the next day)",
    ),
    _proposed(
        "decoupling_hr_lag.hr_lag_blunted",
        "decoupling_hr_lag",
        "HR lag is shown as context only; no lag cut-off marks a blunted response (ADR-0012).",
        "C",
        "Bellenger CR et al. 2016, Sports Med 46(10):1461-1486, PMID 26888648 (no lag cut-off)",
    ),
    _proposed(
        "readiness_v1.hrv_single_day",
        "readiness_v1",
        "The 7-day rolling mean of ln rMSSD (>= 3 nights) against the baseline mean +/- 0.5 SD "
        "reflects readiness; a single night does not (readiness_v2, ADR-0013).",
        "C",
        "Plews DJ et al. 2013, Int J Sports Physiol Perform 8(6):688-691, PMID 23479420 "
        "(7-day mean r=.72, single day r=-.06)",
        "Plews DJ et al. 2014, Int J Sports Physiol Perform 9(5):783-790, PMID 24334285 "
        "(three valid nights per week suffice)",
        "Manresa-Rocamora A et al. 2021, Int J Environ Res Public Health 18(19):10299 "
        "(HRV-guided trials use the 7-day mean vs baseline +/- 0.5 SD)",
    ),
    _proposed(
        "planning.overdone_easing",
        None,
        "A day over max(1.3x its slot target, target + 40 TSS) eases the next day by half the "
        "excess (floor 60 %, hard -> endurance, > +100 TSS -> an easy Z2 hour); no forced "
        "recovery spin (ADR-0017).",
        "C",
        "Ours (ADR-0017); direction from Frandsen J et al. 2025, Br J Sports Med 59(17):1203-1210 "
        "(single-session spikes matter, week-to-week ratios do not)",
    ),
    _proposed(
        "personal_scales.rpe_offset",
        None,
        "RPE is read against the athlete's own offset per ride class: median(RPE - table) over "
        "90 days, >= 5 rides per class (else all classes), shrunk n/(n+5), within +-2 (ADR-0019).",
        "C",
        "Direction: Seiler S et al. 2017 / Inoue A et al. 2022 (session RPE differs by person "
        "and session type); window, minimum, k = 5 and +-2 are ours",
    ),
    _proposed(
        "personal_scales.feel_baseline",
        None,
        "A post-ride feel counts as bad only 2 points worse than the athlete's 90-day median "
        "(>= 8 answers) and never stricter than the fixed rule (ADR-0019).",
        "C",
        "Ours (ADR-0019): a personal baseline instead of the scale's middle",
    ),
    _proposed(
        "planning.easy_day_habit",
        None,
        "Easy days are planned near the athlete's own 28-day median non-hard-day load (>= 6 "
        "days, shrunk to the Z2-60 template, within x[1, 1.8]) as 60-90 min Z2 with IF <= 0.70; "
        "not below AS_PLANNED readiness, not in a week already ahead, never for 60+ (D2).",
        "C",
        "Ours (spec adaptive-planning D2): a plan the athlete actually rides makes compliance "
        "meaningful; review §2.1 (Roger never rode a 45-min spin)",
    ),
    _proposed(
        "planning.progression_gate",
        None,
        "Each Monday the next week goes up (+1 step; +1 extra after two easy ups, <= 2 per "
        "block), holds, goes down (-1 step, TSS x0.85) or becomes an early recovery week from "
        "last week's key sessions (2/2 achieved; 2 missed with RPE +2), personal RPE delta, the "
        "7-day HRV band (5 days below), EASY/REST days (<= 1 / >= 3), completion 0.85-1.15 and "
        "fatigue z >= 1.5; a >= 10-day break holds and steps back 1 (> 21 days: 2); from Friday "
        "a week >= 15 % ahead eases its easy days (ADR-0018).",
        "C",
        "Direction: down / hold / up by recovery state, Nuuttila OP et al. 2022, Med Sci Sports "
        "Exerc 54(10):1690, PMID 35975912; Kiviniemi AM et al. 2007, PMID 17849143 and 2010, "
        "PMID 20575165; detraining, Mujika I, Padilla S 2000, Sports Med 30(2):79, PMID "
        "10966148. Every threshold and step size is ours, to be checked by replay",
    ),
    _proposed(
        "readiness_v1.overreached_rule",
        "readiness_v1",
        "The 7-day HRV mean 1.25 SD or more below baseline and RHR z > +1.5 together mean "
        "acute stress (rest).",
        "C",
        "Bellenger CR et al. 2016, Sports Med 46(10):1461-1486, PMID 26888648",
    ),
    Claim(
        "readiness_v1.missing_shrinkage",
        "readiness_v1",
        "When the present inputs weigh less than 0.5, the missing weight counts as a neutral 0 "
        "instead of renormalising onto the few inputs left (ADR-0016).",
        "C",
        ("Ours: a shrinkage choice so one grade-C input (e.g. TSB alone) cannot decide the day",),
        reviewed=dt.date(2026, 10, 17),
        status="proposed",
    ),
    _proposed(
        "readiness_v1.sleep",
        "readiness_v1",
        "Poor sleep lowers next-day endurance performance.",
        "B",
        "Craven J et al. 2022, Sports Med 52(11):2669-2690, PMID 35708888",
    ),
    _proposed(
        "readiness_v1.rpe_surprise",
        "readiness_v1",
        "An RPE 3 or more above expected caps the next day at EASY.",
        "C",
        "Inoue A et al. 2022, Sports Med Open 8:35 (meta-analysis, very low certainty)",
    ),
    _proposed(
        "guardrails.long_ride_hit_gap",
        "guardrails",
        "No hard session the day after a long ride over 150 TSS.",
        "C",
        "No direct evidence; Stanley J et al. 2013, Sports Med, PMID 23912805",
    ),
    _proposed(
        "guardrails.weekly_tss_cap",
        "guardrails",
        "Weekly TSS at most 1.15 x the 4-week mean.",
        "C",
        "No direct evidence; Frandsen J et al. 2025, Br J Sports Med 59(17):1203-1210, "
        "doi:10.1136/bjsports-2024-109380 (runners: week-to-week ratio unrelated to injury)",
    ),
    _proposed(
        "guardrails.single_ride_cap",
        "guardrails",
        "A single ride at most 1.6 x the longest of the last 6 weeks.",
        "C",
        "Frandsen J et al. 2025, Br J Sports Med 59(17):1203-1210, "
        "doi:10.1136/bjsports-2024-109380 (runners: > 10 % over the 30-day longest raises risk)",
    ),
    _proposed(
        "guardrails.senior_caps",
        "guardrails",
        "Lower ramp and hard-session caps for athletes 60+.",
        "C",
        "No age-specific load-cap study found; conservative design choice",
    ),
    _proposed(
        "guardrails.tsb_floor",
        "guardrails",
        "A TSB floor (-30 / -25 / 0 by phase) keeps fatigue safe.",
        "C",
        "Safety net only; TSB has no individual predictive value (Marchal A et al. 2025, "
        "Sci Rep 15:3706, doi:10.1038/s41598-025-88153-7)",
    ),
    _proposed(
        "eftp.cp_to_ftp",
        "eftp",
        "FTP = 0.97 x CP.",
        "C",
        "Karsten B et al. 2021, Front Physiol 11:613151, PMC7862708 (n=17)",
    ),
    _proposed(
        "eftp.proposal_3pct",
        "eftp",
        "An estimate 3 % above FTP for 14 days justifies an FTP change proposal.",
        "C",
        "Lillo-Bevia JR et al. 2022, J Strength Cond Res 36(1):167-173, PMID 31714462 "
        "(smallest detectable change about 3 %: 3 % sits at the noise edge)",
    ),
    _proposed(
        "time_in_zone_tid.phase_targets",
        "time_in_zone_tid",
        "Phase TID targets 80/15/5, 75/15/10, 75/5/20 counted by sessions; time in zone is "
        "reference only (ADR-0014).",
        "C",
        "Seiler KS, Kjerland GO 2006, Scand J Med Sci Sports 16(1):49-56 (session-goal counting)",
        "Mateo-March M et al. 2026, Biol Sport 43(4):933-940, doi:10.5114/biolsport.2026.156234 "
        "(WorldTour time above CP 4-7 %)",
        "Sylta O et al. 2014, Int J Sports Physiol Perform 9(1):100-107, PMID 24408353 "
        "(time-based vs session-goal mid/high shares differ about threefold)",
    ),
    _proposed(
        "eftp.ramp_075",
        "eftp",
        "Ramp-test FTP = 0.75 x best 1-minute power.",
        "C",
        "No peer-reviewed validation found",
    ),
    # --- proposed with the evidence mechanism (report wording), awaiting approval
    _proposed(
        "ramp_rate_acwr_monotony.acwr_low",
        "ramp_rate_acwr_monotony",
        "ACWR < 0.8 marks a load drop worth noting outside a recovery week.",
        "C",
        "Gabbett TJ 2016, Br J Sports Med 50(5):273-280 (team-sport sweet spot 0.8-1.3)",
    ),
    # --- ride report models (docs/specs/ride-report.md); ours, no glossary page yet
    _proposed(
        "ride_intent.model",
        None,
        "ride_intent_v1.1: time in the planned power band, counted reps and rep minutes judge "
        "the stimulus.",
        "C",
        "Ours (docs/04-analysis-engine.md, ride report models); not validated",
    ),
    _proposed(
        "outdoor_context.model",
        None,
        "outdoor_context_v1: heat, stop and coasting cut-offs mark numbers as not comparable.",
        "C",
        "Ours (docs/04-analysis-engine.md, ride report models); not validated",
    ),
    _proposed(
        "goal_projection.model",
        None,
        "goal_projection_v1: a Theil-Sen trend of FTP estimates projects the goal date.",
        "C",
        "Ours (docs/04-analysis-engine.md, ride report models); not validated",
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


def for_glossary(glossary_id: str | None) -> tuple[Claim, ...]:
    """Claims documented in ``docs/glossary/<glossary_id>.md``, registry order."""
    return tuple(c for c in REGISTRY if c.glossary == glossary_id)


def verdict_allowed(claim_ids: Iterable[str]) -> bool:
    """A verdict or warning may rest on these claims only if at least one is grade A/B."""
    return any(grade(c) in VERDICT_GRADES for c in claim_ids)
