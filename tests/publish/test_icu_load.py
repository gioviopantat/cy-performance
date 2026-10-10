"""Verification expects intervals.icu's load: freeride counted at ICU_FREERIDE_PCT."""

from __future__ import annotations

from cyp.planning.templates import icu_expected_tss, load_library, resolve
from cyp.publish.publisher import LOAD_TOLERANCE, Publisher, verify_load
from tests.publish.conftest import FakeCalendar


def test_climb_repeats_verify_against_icu_load() -> None:
    library = load_library()
    w = resolve(library["climb_ss_repeats"], None, outdoor=True)
    icu_seen = 80.0  # what intervals.icu computed for this workout (2026-10-13 needs_review)
    assert verify_load(w.tss, icu_seen, LOAD_TOLERANCE) is False  # our own TSS: 71
    assert verify_load(icu_expected_tss(w), icu_seen, LOAD_TOLERANCE) is True
    assert w.tss < icu_expected_tss(w)  # planning keeps the easier descent


def test_no_freeride_no_difference() -> None:
    library = load_library()
    w = resolve(library["vo2_4x5"], None, outdoor=False)
    assert icu_expected_tss(w) == w.tss


def test_publisher_verifies_against_icu_load() -> None:
    from tests.publish.test_publish import NOW, WINDOW, spec

    cal = FakeCalendar(load_per_min=1.0)  # icu "computes" 90 for 90 minutes
    ours = spec(2, minutes=90, load=78.0).model_copy(update={"icu_load": 88.0})
    r = Publisher(cal).run([ours], window=WINDOW, now_local=NOW, mode="apply", allow_write=True)
    assert r.verified == [ours.external_id] and r.needs_review == []
