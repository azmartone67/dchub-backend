"""tests/test_claim_second_measurer.py — a confirmation needs two instruments
(2026-08-29).

Lane 4 of the wiring shell, generalising the canon fix of 2026-08-23.

That fix produced this loop's first genuine self-refutation, and the reason it
worked is the point: the expectation and the measurement stopped coming from
the same place. Claim 100945 carried `pinned 1,800+ / expected == 1,900+` and
was judged `confirmed` IN PRODUCTION because the expectation was also taken
from resolve_canon() — actual == expected by construction.

resolve_metric() was still ONE reader per scheme. A claim it confirms is
confirmed on the word of a single instrument, and an instrument that is wrong
the same way twice is indistinguishable from one that is right.

Ways this could go wrong, one test each:
  (1) ★ SILENCE READ AS AGREEMENT — the second reader fails and the claim is
      confirmed anyway, with a corroboration stamp that looks like proof.
  (2) ★ CONFIRMED ON ONE READER — the whole defect.
  (3) REFUTATION SUPPRESSED — a disagreeing second reader buries a real
      failure. Confirmation and refutation are NOT symmetric here.
  (4) SELF-CORROBORATION — the second reader shares the first's code or its
      injected fetch, so it agrees with itself by construction. That is claim
      100945 again, one level up.
  (5) FAKED COVERAGE — a scheme with no independent path claims one.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_claim_second_measurer.py -v
"""
from __future__ import annotations

import pytest


def _cl():
    from routes import claim_ledger as c
    return c


# ── (2) ★ a confirmation on one reader is not a confirmation ─────────────

def test_a_contradicted_confirmation_is_downgraded(monkeypatch):
    """★REGRESSION (2). The first reader says the canon key matches; the
    served surface says otherwise. That is not a confirmation."""
    c = _cl()
    monkeypatch.setattr(c, "second_reading",
                        lambda m, cur=None, fetch=None: ("1,800+", {"path": "x"}))
    verdict, note = c.corroborate("confirmed", "canon:facilities.count", "== 1,900+")
    assert verdict == "unobserved", "a contradicted confirmation stayed confirmed"
    assert note["corroboration"] == "disagree"
    assert note["downgraded_from"] == "confirmed"


def test_an_agreeing_second_reader_leaves_the_confirmation_alone(monkeypatch):
    """THE PAIRED CONTROL. If corroboration cannot pass, it is not a check —
    it is an outage that quietly empties the ledger of confirmations."""
    c = _cl()
    monkeypatch.setattr(c, "second_reading",
                        lambda m, cur=None, fetch=None: ("1,900+", {"path": "x"}))
    verdict, note = c.corroborate("confirmed", "canon:facilities.count", "== 1,900+")
    assert verdict == "confirmed"
    assert note["corroboration"] == "agree"


# ── (1) ★ silence is not agreement ───────────────────────────────────────

def test_a_silent_second_reader_is_unavailable_not_agreement(monkeypatch):
    """★REGRESSION (1). The second reader returning None means it did not
    measure. Recording that as agreement would manufacture confidence out of
    an outage — the exact move this codebase keeps deleting."""
    c = _cl()
    monkeypatch.setattr(c, "second_reading",
                        lambda m, cur=None, fetch=None: (None, {"status": "empty_or_failed"}))
    verdict, note = c.corroborate("confirmed", "canon:x", "== 1")
    assert verdict == "confirmed", "silence must not downgrade a verdict either"
    assert note["corroboration"] == c.CORROBORATION_UNAVAILABLE
    assert note["corroboration"] != "agree"


def test_an_unobserved_second_reading_is_not_a_disagreement(monkeypatch):
    c = _cl()
    monkeypatch.setattr(c, "second_reading",
                        lambda m, cur=None, fetch=None: ("not-a-number", {}))
    verdict, note = c.corroborate("confirmed", "canon:x", ">= 5")
    assert note["corroboration"] == c.CORROBORATION_UNAVAILABLE
    assert verdict == "confirmed"


# ── (3) the asymmetry: a refutation stands ───────────────────────────────

def test_a_refutation_is_not_suppressed_by_a_disagreeing_reader(monkeypatch):
    """★REGRESSION (3). Confirmation and refutation are deliberately NOT
    symmetric. Downgrading a refutation because a second instrument disagreed
    renders a failure as a non-result, which is the defect this whole shell
    exists to remove."""
    c = _cl()
    monkeypatch.setattr(c, "second_reading",
                        lambda m, cur=None, fetch=None: ("1,900+", {"path": "x"}))
    verdict, note = c.corroborate("refuted", "canon:x", "== 1,900+")
    assert verdict == "refuted", "a refutation was suppressed"
    assert note["corroboration"] == "disagree", "the disagreement must still be recorded"


# ── (4) ★ the second reader must not be the first one again ──────────────

def test_the_get_scheme_refuses_to_corroborate_with_an_injected_fetch():
    """★REGRESSION (4). `fetch` IS the first reader. Reusing it would
    corroborate a reading with itself — claim 100945's defect one level up."""
    c = _cl()
    calls = []

    def spy(path):
        calls.append(path)
        return {"n": 5}

    actual, ev = c.second_reading("get:/api/v1/x n", cur=None, fetch=spy)
    assert actual is None
    assert "no_independent_path" in ev.get("status", "")
    assert not calls, "the second reader called the FIRST reader's fetch"


def test_the_canon_second_reader_does_not_call_resolve_canon(monkeypatch):
    """The first reader is resolve_canon(). The second must read the SERVED
    surface instead, or it is the same instrument twice."""
    c = _cl()
    seen = {}

    def fake_fetch(path):
        seen["path"] = path
        return {"phrases": {"facilities.count": "1,900+"}}

    actual, ev = c.second_reading("canon:facilities.count", cur=None, fetch=fake_fetch)
    assert seen["path"] == "/api/v1/canon/phrases"
    assert actual == "1,900+"


def test_the_finding_second_reader_uses_http_not_the_cursor():
    """The first reader is direct SQL on brain_findings. A second reader on
    the same cursor would be the same transport, the same process and the
    same failure modes."""
    c = _cl()
    used_cursor = {"n": 0}

    class Cur:
        def execute(self, *a, **k):
            used_cursor["n"] += 1

        def fetchone(self):
            return None

    def fake_fetch(path):
        return {"recent": [{"url": "https://dchub.cloud/x", "status": "open"}]}

    actual, ev = c.second_reading("finding:https://dchub.cloud/x status",
                                  cur=Cur(), fetch=fake_fetch)
    assert used_cursor["n"] == 0, "the second reader queried the first's cursor"
    assert actual == "open"


def test_a_finding_outside_the_recent_window_is_silence_not_disagreement():
    c = _cl()
    actual, ev = c.second_reading("finding:https://dchub.cloud/missing status",
                                  cur=None, fetch=lambda p: {"recent": []})
    assert actual is None
    assert ev["status"] == "not_in_recent_window"


# ── (5) coverage is declared honestly ────────────────────────────────────

def test_linkedin_declares_no_independent_reader():
    """★REGRESSION (5). The only other LinkedIn reader is LinkedIn's own API.
    A corroboration that is really the same read twice is WORSE than none,
    because it looks like agreement."""
    c = _cl()
    actual, ev = c.second_reading("linkedin:123 impressions", cur=None, fetch=None)
    assert actual is None
    assert ev["status"] == c.CORROBORATION_UNAVAILABLE
    assert "linkedin" not in c._SECOND_READERS


def test_every_declared_second_reader_is_callable():
    c = _cl()
    assert c._SECOND_READERS, "no second readers registered at all"
    for scheme, fn in c._SECOND_READERS.items():
        assert callable(fn), scheme


def test_the_outcome_vocabulary_is_not_widened():
    """`disputed` would be a fifth outcome that every existing consumer —
    NEGATIVE_LESSON_CORPORA selects on IN ('refuted','retracted'), the
    dashboards, the self-model — has never heard of."""
    c = _cl()
    assert c.OUTCOMES == ("confirmed", "refuted", "retracted", "unobserved")


def test_a_second_reader_that_raises_is_caught_as_silence():
    c = _cl()

    def boom(path):
        raise RuntimeError("network is down")

    actual, ev = c.second_reading("canon:x", cur=None, fetch=boom)
    assert actual is None
    assert ev["status"] == "second_reader_failed"


def test_a_literal_dotted_canon_key_is_not_treated_as_a_path():
    """★ dig() splits on dots. A canon key IS a dotted literal
    ('facilities.count'), so digging walks into a 'facilities' node that does
    not exist and returns None — silence, for EVERY canon claim. A
    corroborator that can never corroborate is worse than none: it reports
    'unavailable' forever and looks like coverage."""
    c = _cl()
    flat = lambda p: {"facilities.count": "1,900+"}
    nested_under_phrases = lambda p: {"phrases": {"facilities.count": "1,900+"}}
    for shape in (flat, nested_under_phrases):
        actual, ev = c.second_reading("canon:facilities.count", cur=None, fetch=shape)
        assert actual == "1,900+", ev


def test_a_canon_key_absent_from_the_surface_says_so():
    c = _cl()
    actual, ev = c.second_reading("canon:missing.key", cur=None,
                                  fetch=lambda p: {"phrases": {"other": 1}})
    assert actual is None
    assert ev["status"] == "key_not_on_surface"
