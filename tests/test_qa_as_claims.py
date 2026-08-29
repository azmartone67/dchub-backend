"""tests/test_qa_as_claims.py — a QA green is re-asked on a clock (2026-08-29).

Lane 6 of the wiring shell.

Every PASS on the QA board is true at the moment the harness ran and then
STANDS UNCHALLENGED until a human runs it again. That is the silent green this
platform keeps rediscovering: /api/land-power/status returned a hardcoded
"healthy" for four months, and get_backup_status reported 9/9 feeds healthy
while news content was dated two months in the FUTURE. Nothing lied at the
moment of the check — the check stopped being re-asked.

routes/claim_ledger already solves that for other producers: register an
assertion with a HORIZON and let a verifier that is not the author judge it.

Ways this could go wrong, one test each:
  (1) ★ IMPLIED COVERAGE — the run reports claims registered and reads as if
      the whole board were claim-backed. Most checks CANNOT be claims.
  (2) ★ A CLAIM THAT CAN NEVER MEASURE — `sources` resolves to a list, _num()
      refuses lists, so the claim judges `unobserved` forever. That looks like
      coverage and refutes nothing, which is worse than no claim.
  (3) HALF A CLAIM — a metric with no expectation confirms itself, the exact
      shape the canon fix removed from the ledger.
  (4) REDS AND BLINDS REGISTERED — a BLIND was never observed and a RED is
      already known-bad; neither is a green worth re-asking.
  (5) LEDGER FAILURE SWALLOWED — the ledger is down and the run reports
      success anyway.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_qa_as_claims.py -v
"""
from __future__ import annotations

import pytest

from tools.qa_superuser import claims as C
from tools.qa_superuser.finding import (BLIND, GAUGE, INFO, MAJOR, PASS, RED,
                                        Finding)


def _f(key, verdict=PASS, metric="", expect="", severity=INFO):
    return Finding(key=key, surface="contract", seat="anon",
                   title="a check", verdict=verdict, severity=severity,
                   evidence="observed", basis="GET /x", red_when="it breaks",
                   claim_metric=metric, claim_expect=expect)


# ── (3) half a claim is not a claim ──────────────────────────────────────

def test_a_metric_without_an_expectation_is_refused_at_construction():
    """★REGRESSION (3). An instrument with no expectation cannot be refuted;
    it would register as 'measured nothing in particular' and confirm itself."""
    with pytest.raises(ValueError, match="together"):
        _f("k", metric="get:/x n")


def test_an_expectation_without_an_instrument_is_refused_too():
    with pytest.raises(ValueError, match="together"):
        _f("k", expect=">= 1")


def test_a_complete_pair_is_accepted():
    f = _f("k", metric="get:/x n", expect=">= 1")
    assert f.claim_backed is True


# ── (4) only a green is worth re-asking ──────────────────────────────────

def test_a_red_is_not_registered():
    """A RED is already known-bad. Re-asserting it as a claim would record a
    refutation when someone FIXES it."""
    f = _f("k", verdict=RED, severity=MAJOR, metric="get:/x n", expect=">= 1")
    assert f.claim_backed is False
    assert "not a PASS" in C.plan([f])["unbacked"]["k"]


def test_a_blind_is_not_registered():
    """★ rule 1: BLIND is NOT observed. Registering it would turn 'we could
    not look' into an assertion about the world."""
    f = _f("k", verdict=BLIND, metric="get:/x n", expect=">= 1")
    assert f.claim_backed is False


def test_a_gauge_is_not_registered():
    f = _f("k", verdict=GAUGE, metric="get:/x n", expect=">= 1")
    assert f.claim_backed is False


# ── (1) ★ coverage is reported, never implied ────────────────────────────

def test_the_plan_names_every_unbacked_check_and_why():
    """★REGRESSION (1). Most checks here are multi-request behavioural
    assertions from a seat; the ledger's instruments are single readings. A
    result that reported only 'registered: 1' would read as full coverage."""
    findings = [
        _f("backed", metric="get:/x n", expect=">= 1"),
        _f("behavioural"),                      # a PASS with no instrument
        _f("red", verdict=RED, severity=MAJOR),
    ]
    p = C.plan(findings)
    assert p["backed"] == ["backed"]
    assert "behavioural" in p["unbacked"]
    assert "no re-measurable instrument" in p["unbacked"]["behavioural"]
    assert "red" in p["unbacked"]


def test_coverage_is_measured_against_passes_not_the_whole_board():
    """Counting REDs and BLINDs as legitimately-unbacked would flatter the
    ratio — they are unbacked for a reason nobody needs to fix."""
    findings = [
        _f("a", metric="get:/x n", expect=">= 1"),
        _f("b"),
        _f("red", verdict=RED, severity=MAJOR),
        _f("blind", verdict=BLIND),
    ]
    p = C.plan(findings)
    assert p["passes"] == 2
    assert p["coverage_of_passes"] == 0.5, "coverage was computed over the board"


def test_a_board_with_no_passes_reports_no_coverage_rather_than_zero():
    p = C.plan([_f("red", verdict=RED, severity=MAJOR)])
    assert p["coverage_of_passes"] is None, \
        "0.0 would read as 'we have coverage and it is bad'"


# ── (2) ★ a claim that can never measure is not coverage ─────────────────

def test_a_list_valued_field_needs_the_len_suffix_to_be_measurable():
    """★REGRESSION (2). dig('sources') returns the LIST and _num() correctly
    refuses lists, so `sources >= 8` judges `unobserved` forever — a claim
    that looks like coverage and can never refute anything."""
    from routes.claim_ledger import dig, judge
    payload = {"sources": [1, 2, 3, 4, 5, 6, 7, 8]}
    assert judge(dig(payload, "sources"), ">= 8") == "unobserved"
    assert dig(payload, "sources#len") == 8
    assert judge(dig(payload, "sources#len"), ">= 8") == "confirmed"
    assert judge(dig(payload, "sources#len"), ">= 9") == "refuted"


def test_the_len_suffix_is_explicit_not_implicit():
    """Silently turning a list into its length would change what every
    existing metric means, and absent/present genuinely care about the
    container rather than its size."""
    from routes.claim_ledger import dig
    payload = {"sources": [1, 2]}
    assert isinstance(dig(payload, "sources"), list)


def test_len_of_something_unsized_is_not_measured_not_zero():
    from routes.claim_ledger import dig
    assert dig({"n": 5}, "n#len") is None, "an unsized value became a 0"
    assert dig({"n": 5}, "missing#len") is None


def test_the_shipped_freshness_check_uses_a_measurable_metric():
    """The one check wired in this PR must actually be measurable — otherwise
    the mechanism ships listed but not delivered."""
    import inspect
    from tools.qa_superuser import probe_contract
    src = inspect.getsource(probe_contract)
    assert "get:/api/v1/data-freshness sources#len" in src, \
        "the freshness claim lost its #len and can no longer be measured"


# ── (5) a ledger failure is reported, not swallowed ──────────────────────

def test_registration_outcomes_are_reported(monkeypatch):
    calls = []

    def fake_register(**kw):
        calls.append(kw)
        return {"ok": True, "id": 1}

    out = C.register_run_claims([_f("a", metric="get:/x n", expect=">= 1")],
                                register=fake_register)
    assert out["registered"] == 1
    assert calls[0]["kind"] == "qa"
    assert calls[0]["subject"] == "qa:a"
    assert calls[0]["horizon_hours"] == C.DEFAULT_HORIZON_HOURS
    assert calls[0]["shipped"] is True


def test_a_refusal_is_surfaced_not_counted_as_registered():
    def refusing(**kw):
        return {"ok": False, "refused": True, "error": "bad comparator"}

    out = C.register_run_claims([_f("a", metric="get:/x n", expect=">= 1")],
                                register=refusing)
    assert out["registered"] == 0
    assert out["refused"] and out["refused"][0]["key"] == "a"


def test_a_raising_ledger_is_recorded_and_does_not_kill_the_run():
    """★REGRESSION (5). The QA run must not die because the ledger is down —
    and must not report success either."""
    def boom(**kw):
        raise RuntimeError("db gone")

    out = C.register_run_claims([_f("a", metric="get:/x n", expect=">= 1")],
                                register=boom)
    assert out["registered"] == 0
    assert out["errors"] and "db gone" in out["errors"][0]


def test_an_already_open_claim_is_not_double_counted():
    out = C.register_run_claims([_f("a", metric="get:/x n", expect=">= 1")],
                                register=lambda **kw: {"ok": True, "already": True, "id": 7})
    assert out["registered"] == 0
    assert out["already"] == 1


def test_qa_is_a_registered_claim_kind():
    from routes.claim_ledger import KINDS
    assert "qa" in KINDS


def test_the_outcome_vocabulary_is_untouched():
    from routes.claim_ledger import OUTCOMES
    assert OUTCOMES == ("confirmed", "refuted", "retracted", "unobserved")
