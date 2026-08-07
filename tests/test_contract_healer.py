"""Guards for routes/contract_healer.py — the CONTRACT healer (Shell #44).

Pure-function + injected-fetch tests. No DB, no network, never imports main
(green-main convention: nothing in tests/ may exit at module scope).

★ EVERY GUARD HERE IS PAIRED WITH A MUST-FAIL CONTROL. The pattern is: feed the
  check the ACTUAL defect it was built for, assert it goes RED; then feed it the
  corrected input, assert it goes GREEN. A test that only ever asserts the
  green path proves the check can pass, which is precisely what defect #9 also
  proved — it was a check that could ONLY pass, for months, at 78.5% against a
  90% bar under a name that meant 25%.

  The controls use the defects' VERBATIM strings, not paraphrases, because the
  thing being verified is that this code catches THAT input.

The invariants pinned below:
  1. three-valued everywhere: an unreadable side is None, never a pass and
     never a failure, and INDETERMINATE dominates a lane verdict;
  2. unknown is never rendered as 0, and never enters /heal/findings;
  3. lane A fails when a served page reads a key the live payload lacks;
  4. lane B fails on cross-surface disagreement beyond a RELATIVE band, and
     tolerates the fetch skew of a rolling "ending now" window;
  5. lane C catches an absent status defaulting to a success literal, and does
     NOT catch a domain state defaulting to "active";
  6. lane E flags a passing check whose own prose states a bar it exceeds, and
     stays silent on reds, on non-negating names, and on unmeasurables.
"""
import os
import textwrap

import pytest

from routes import contract_healer as ch


# ── the three-valued contract ────────────────────────────────────────────────
def test_lane_verdict_indeterminate_dominates_a_pass():
    """UNREADABLE IS NOT DRIFT — and it is not health either. One unmeasured
    check makes the lane INDETERMINATE even when every other check passed.
    This is the semantic that kept registry drift correctly FALSE when 11 of 16
    listings were unreadable."""
    passing = [ch._check("a", "n", True, "ok"), ch._check("b", "n", True, "ok")]
    assert ch._lane_verdict(passing) == "PASSED"
    assert ch._lane_verdict(passing + [ch._check("c", "n", None, "?")]) \
        == "INDETERMINATE"
    # and it outranks a failure too: we cannot call a lane FAILED when part of
    # it was never read — the unread part could change the story either way.
    assert ch._lane_verdict([ch._check("a", "n", False, "bad"),
                             ch._check("c", "n", None, "?")]) == "INDETERMINATE"


def test_lane_verdict_empty_is_not_a_pass():
    """Zero checks is zero evidence. A lane that ran nothing has not passed."""
    assert ch._lane_verdict([]) == "INDETERMINATE"


def test_unmeasured_never_reaches_heal_findings(monkeypatch):
    """Downstream, a finding IS a defect: master-heal opens issues from this
    list. An unread surface must never enter it — that is how '?' becomes '0'."""
    monkeypatch.setattr(ch, "run_contract_healer", lambda: {
        "lanes": [{"lane": "A", "checks": [
            ch._check("u", "unreadable thing", None, "NOT MEASURED"),
            ch._check("f", "real defect", False, "two things disagree"),
            ch._check("p", "fine", True, "ok"),
        ]}]})
    monkeypatch.setattr(ch, "_cache", {"at": 0.0, "findings": None})
    out = ch.scan_all(force=True)
    assert len(out) == 1
    assert out[0]["issue"].startswith("contract_")
    assert "real defect" in out[0]["issue"]


def test_fetch_failure_yields_a_reason_not_a_value(monkeypatch):
    """_fetch never substitutes a default for a failed read."""
    import requests

    def boom(*a, **k):
        raise OSError("connection reset")
    monkeypatch.setattr(requests, "get", boom)
    payload, reason = ch._fetch("/api/whatever")
    assert payload is None and reason


def test_fetch_non_2xx_is_a_reason_not_an_empty_payload(monkeypatch):
    """★ A 404 or a 502 must not arrive as {} — an empty payload would make
    every field look absent and every lane-A contract look broken, turning an
    outage into a page of fabricated defects."""
    import requests

    class _R:
        status_code = 502
        text = "bad gateway"

        def json(self):
            return {}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R())
    payload, reason = ch._fetch("/api/whatever")
    assert payload is None and "502" in reason


# ── lane A — field existence (defect #5) ─────────────────────────────────────
def _stub_fetch(pages: dict, payloads: dict):
    """Injected _fetch: dict lookup, and a missing key is UNREADABLE."""
    def _f(url, accept="application/json"):
        book = payloads if accept == "application/json" else pages
        if url not in book:
            return None, "HTTP 404"
        return book[url], None
    return _f


A_CONTRACT = [{"page": "/ai.html", "endpoint": "/api/ai/tracking",
               "field": "recent_activity", "why": "task_01b0a3f1"}]


def test_lane_a_MUST_FAIL_on_the_real_dead_surface(monkeypatch):
    """★ MUST-FAIL CONTROL — defect #5, verbatim.

    ai.html reads data.recent_activity; /api/ai/tracking stopped publishing the
    key. Both sides return 200 and both are well-formed, so no value check, no
    status check and no schema check sees anything. This is the shape the whole
    module exists for."""
    monkeypatch.setattr(ch, "FIELD_CONTRACTS", A_CONTRACT)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch(
        pages={"/ai.html": "<script>if (data.recent_activity?.length) renderFeed(data);</script>"},
        payloads={"/api/ai/tracking": {"platforms": {}, "requests_7d": 5}}))
    (c,) = ch._lane_field_existence()
    assert c["pass"] is False and c["critical"] is True
    # the failure must NAME THE TWO THINGS THAT DISAGREE
    assert "/ai.html" in c["detail"] and "/api/ai/tracking" in c["detail"]
    assert "recent_activity" in c["detail"]


def test_lane_a_green_when_the_payload_publishes_the_field(monkeypatch):
    """Revert the defect: the same page against a payload that carries the key."""
    monkeypatch.setattr(ch, "FIELD_CONTRACTS", A_CONTRACT)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch(
        pages={"/ai.html": "<script>if (data.recent_activity?.length) renderFeed(data);</script>"},
        payloads={"/api/ai/tracking": {"recent_activity": [], "requests_7d": 5}}))
    (c,) = ch._lane_field_existence()
    assert c["pass"] is True


def test_lane_a_finds_the_field_at_any_depth(monkeypatch):
    """A nested key is still published. Flagging it would be a false alarm."""
    monkeypatch.setattr(ch, "FIELD_CONTRACTS", A_CONTRACT)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch(
        pages={"/ai.html": "data.recent_activity"},
        payloads={"/api/ai/tracking": {"feed": {"inner": {"recent_activity": []}}}}))
    assert ch._lane_field_existence()[0]["pass"] is True


def test_lane_a_stale_entry_fails_so_the_registry_cannot_rot(monkeypatch):
    """The second direction. An entry naming a consumer that no longer reads the
    field is a check that can only ever pass — the exact defect class of a
    hardcoded watch list (the first immutable-asset guard covered 1 file of 31)."""
    monkeypatch.setattr(ch, "FIELD_CONTRACTS", A_CONTRACT)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch(
        pages={"/ai.html": "<script>nothing here</script>"},
        payloads={"/api/ai/tracking": {"recent_activity": []}}))
    (c,) = ch._lane_field_existence()
    assert c["pass"] is False and c["critical"] is False
    assert "STALE" in c["detail"].upper()


@pytest.mark.parametrize("pages,payloads", [
    ({}, {"/api/ai/tracking": {"recent_activity": []}}),          # page unreadable
    ({"/ai.html": "data.recent_activity"}, {}),                   # endpoint unreadable
    ({}, {}),                                                     # neither readable
])
def test_lane_a_unreadable_is_never_a_verdict(monkeypatch, pages, payloads):
    """★ The non-negotiable. A fetch failure renders '?'. Note the middle case
    especially: the page DOES read the field and the endpoint is unreadable —
    the tempting inference is 'so the field is missing', and that inference is
    exactly how an outage gets published as a defect."""
    monkeypatch.setattr(ch, "FIELD_CONTRACTS", A_CONTRACT)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch(pages, payloads))
    (c,) = ch._lane_field_existence()
    assert c["pass"] is None
    assert "NOT MEASURED" in c["detail"]


# ── lane B — one quantity, one number (defects #2, #3, #6, #7) ───────────────
B_Q = [{"key": "real_external_calls_7d", "publishers": ["/u1", "/u2"],
        "canonical": "/u1", "tolerance_pct": 1.0, "note": "PR #2261"}]


def test_lane_b_MUST_FAIL_on_defect_3s_actual_spread(monkeypatch):
    """★ MUST-FAIL CONTROL — defect #3, with its real numbers.

    /api/v1/mcp/funnel published 6,997 and 1,567 as 'real external weekly
    calls': two near-identically named fields, 4.5x apart, pointing in opposite
    directions. Both were individually correct over their own population."""
    monkeypatch.setattr(ch, "QUANTITIES", B_Q)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch({}, {
        "/u1": {"real_external_calls_7d": 6997},
        "/u2": {"metrics": {"real_external_calls_7d": 1567}}}))
    (c,) = ch._lane_one_quantity()
    assert c["pass"] is False and c["critical"] is True
    # both values AND both URLs, or the reader cannot act on it
    assert "6997" in c["detail"] and "1567" in c["detail"]
    assert "/u1" in c["detail"] and "/u2" in c["detail"]
    assert "4.46x" in c["detail"] or "4.4" in c["detail"]


def test_lane_b_MUST_FAIL_on_defect_2s_denominator_error(monkeypatch):
    """★ MUST-FAIL CONTROL — defect #2. Retention published 14.6% by dividing by
    the CURRENT window where the name implies the PRIOR cohort; true value 8.4%.
    74% apart, so a 1% band catches it with room to spare."""
    monkeypatch.setattr(ch, "QUANTITIES", [dict(B_Q[0], key="retention_pct")])
    monkeypatch.setattr(ch, "_fetch", _stub_fetch({}, {
        "/u1": {"retention_pct": 14.6}, "/u2": {"retention_pct": 8.4}}))
    assert ch._lane_one_quantity()[0]["pass"] is False


def test_lane_b_tolerates_rolling_window_fetch_skew(monkeypatch):
    """★ THE ANTI-WOLF CONTROL, and it is not optional.

    The first live run of this lane reported 6011 vs 6012 as a DISAGREEMENT.
    Nothing was wrong: the windows roll and end NOW, the two publishers are
    fetched seconds apart, and a call landed between the fetches. A guard that
    fires on the passage of time is muted within a day, and a muted guard is
    worth less than no guard because it also teaches distrust of the ones that
    are right."""
    monkeypatch.setattr(ch, "QUANTITIES", B_Q)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch({}, {
        "/u1": {"real_external_calls_7d": 6011},
        "/u2": {"real_external_calls_7d": 6012}}))
    assert ch._lane_one_quantity()[0]["pass"] is True


def test_lane_b_same_payload_duplication_is_caught(monkeypatch):
    """Defect #3 lived at TWO DEPTHS OF ONE PAYLOAD. Depth-blind collection is
    what makes that visible; a pinned json path would have walked past it."""
    monkeypatch.setattr(ch, "QUANTITIES", [dict(B_Q[0], publishers=["/u1"])])
    monkeypatch.setattr(ch, "_fetch", _stub_fetch({}, {
        "/u1": {"gate": {"real_external_calls_7d": 21.6},
                "trend": {"real_external_calls_7d": 25.1}}}))
    assert ch._lane_one_quantity()[0]["pass"] is False


def test_lane_b_one_unreadable_publisher_voids_the_comparison(monkeypatch):
    """★ Agreement among the surfaces we COULD read is not agreement. Two of
    three publishers matching says nothing about the third."""
    monkeypatch.setattr(ch, "QUANTITIES", B_Q)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch({}, {
        "/u1": {"real_external_calls_7d": 6997}}))
    (c,) = ch._lane_one_quantity()
    assert c["pass"] is None and "NOT MEASURED" in c["detail"]


def test_lane_b_single_occurrence_is_unmeasured_not_agreement(monkeypatch):
    """A quantity found once cannot disagree with itself. Calling that a PASS
    would turn a RENAMED field into a green check — a vacuous pass."""
    monkeypatch.setattr(ch, "QUANTITIES", B_Q)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch({}, {
        "/u1": {"real_external_calls_7d": 6997}, "/u2": {"renamed_field": 6997}}))
    assert ch._lane_one_quantity()[0]["pass"] is None


def test_lane_b_zero_scale_does_not_divide_by_zero(monkeypatch):
    monkeypatch.setattr(ch, "QUANTITIES", B_Q)
    monkeypatch.setattr(ch, "_fetch", _stub_fetch({}, {
        "/u1": {"real_external_calls_7d": 0}, "/u2": {"real_external_calls_7d": 0}}))
    assert ch._lane_one_quantity()[0]["pass"] is True


def test_lane_b_ignores_booleans(monkeypatch):
    """bool is a subclass of int in Python. A `"real_external_calls_7d": true`
    must not be compared against a count as if it were 1."""
    assert ch._collect({"k": True, "n": {"k": 5}}, "k") == [("/n/k", 5)]


# ── lane C — population declared (defects #1, #10) ───────────────────────────
DEFECT_10 = 'ran = [s for s in ex if (s.get("status") or "executed") == "executed"]\n'
NEUTRAL = 'ran = [s for s in ex if (s.get("status") or "") == "executed"]\n'
DOMAIN_STATE = 'status = attrs.get("STATUS") or attrs.get("status") or "active"\n'


def test_lane_c_MUST_FAIL_then_green_on_the_real_flattering_default(tmp_path):
    """★ MUST-FAIL CONTROL — defect #10, verbatim, then reverted.

    An absent step status defaulted to the flattering literal, so a step the
    planner recorded but never resolved was counted as having run, and coverage
    flipped partial -> complete with no error anywhere."""
    f = tmp_path / "meta_replays.py"
    f.write_text(DEFECT_10)
    hits = ch.scan_flattering_defaults(str(tmp_path))
    assert len(hits) == 1, "the guard did not catch the actual defect"
    assert hits[0][0] == "meta_replays.py"

    # revert -> green. A neutral default is the CORRECT fix, so it must not flag.
    f.write_text(NEUTRAL)
    assert ch.scan_flattering_defaults(str(tmp_path)) == []


def test_lane_c_does_not_flag_a_domain_state(tmp_path):
    """★ THE ANTI-WOLF CONTROL. `attrs.get("STATUS") or "active"` in the
    gas-plant ingester defaults a FACILITY's operational state. "active" is a
    domain value there, not a claim that anything ran. The first version of this
    list carried active/ok/healthy/green and scored 3 real in 16."""
    (tmp_path / "ingest.py").write_text(DOMAIN_STATE)
    assert ch.scan_flattering_defaults(str(tmp_path)) == []


def test_lane_c_does_not_flag_prose_describing_the_defect(tmp_path):
    """canonical_benchmarks.py documents its own fix by quoting the bad line.
    A guard that reports its own documentation is a guard nobody believes."""
    (tmp_path / "documented.py").write_text(
        '# ★ 2026-08-05: this read `(s.get("status") or "executed")` — an ABSENT\n'
        '#   status became the success value. Fixed to default to "".\n'
        'ran = [s for s in ex if (s.get("status") or "") == "executed"]\n')
    assert ch.scan_flattering_defaults(str(tmp_path)) == []


def test_lane_c_skips_its_own_source(tmp_path):
    """This module quotes the defect three times in its own prose."""
    assert not any(h[0].endswith("contract_healer.py")
                   for h in ch.scan_flattering_defaults(
                       os.path.dirname(os.path.dirname(os.path.abspath(ch.__file__)))))


def test_external_predicates_are_computed_not_pinned():
    """★ 'Do not pin literal strings you have not read.' A check in this repo
    once asserted verdict tokens that existed only as prose in a docstring, then
    reported their absence as a defect. These come from CALLING the canonical
    functions, so editing a predicate moves the assertion with it."""
    preds, err = ch._external_predicates()
    if err:
        pytest.skip(f"mcp_calls_deloop unavailable: {err}")
    from mcp_calls_deloop import external_platform_predicate, real_ua_predicate
    assert preds == [external_platform_predicate("platform"),
                     real_ua_predicate("user_agent")]
    assert all(isinstance(p, str) and p.strip() for p in preds)


# ── lane E — verdict matches prose (defects #8, #9) ──────────────────────────
# ★ VERBATIM from routes/agent_retention_master_shell.py at the moment it was
#   wrong: a 90.0 bar under a name that means 25, returning green at 78.5%.
D9_NAME = "no single platform carries reach"
D9_DETAIL = ("top platform 'chatgpt' = 78.5% of 12043 named crawler requests in "
             "7d. Above 25% a WoW built on one platform's burst is "
             "concentration, not growth.")


def test_lane_e_MUST_FAIL_on_defect_9_verbatim():
    """★ MUST-FAIL CONTROL — defect #9, its real name, prose and numbers."""
    assert ch.verdict_contradicts_prose(D9_NAME, D9_DETAIL, True) is True


def test_lane_e_green_once_the_threshold_is_corrected():
    """Revert: the same check below its own stated bar is not a contradiction."""
    ok = ("top platform 'chatgpt' = 18.2% of 12043 named crawler requests in "
          "7d. Above 25% a WoW built on one platform's burst is concentration, "
          "not growth.")
    assert ch.verdict_contradicts_prose(D9_NAME, ok, True) is False


@pytest.mark.parametrize("name,detail,passed,why", [
    (D9_NAME, D9_DETAIL, False, "a RED check that reads oddly is not a lie"),
    (D9_NAME, D9_DETAIL, None, "an unmeasured check makes no claim at all"),
    ("top caller share", D9_DETAIL, True, "no negation in the name -> out of scope"),
    (D9_NAME, "top platform = 78.5% of requests", True, "no bar stated -> nothing to compare"),
    (D9_NAME, "no crawler traffic in 7d — UNMEASURED", True, "no percentages at all"),
])
def test_lane_e_stays_silent_outside_its_narrow_window(name, detail, passed, why):
    """★ THE ANTI-WOLF CONTROLS. This heuristic fires only when all four hold:
    negating name, verdict True, a stated bar, an observed value above it.
    Everything else is None — NOT a pass, and not a finding."""
    assert ch.verdict_contradicts_prose(name, detail, passed) is not True, why


def test_lane_e_threshold_restatement_is_not_an_observation():
    """A detail that merely repeats its bar ('Above 25% ... target 25%') has no
    observation above it."""
    assert ch.verdict_contradicts_prose(
        "no single caller carries the trend",
        "top caller = 25% of calls. Above 25% the total tracks one caller.",
        True) is not True


def test_lane_e_reads_the_real_check_shape():
    """_iter_checks must find records in the {lanes:[{checks:[...]}]} payload the
    master shells actually persist — not a shape invented here."""
    payload = {"shell": "x", "lanes": [
        {"lane": "5", "checks": [{"id": "c", "name": D9_NAME,
                                  "detail": D9_DETAIL, "pass": True,
                                  "critical": False}]}]}
    found = list(ch._iter_checks(payload))
    assert len(found) == 1 and found[0]["name"] == D9_NAME
    assert ch.verdict_contradicts_prose(found[0]["name"], found[0]["detail"],
                                        found[0]["pass"]) is True


def test_lane_e_self_audit_catches_a_contradiction_in_this_shells_own_output():
    """A verdict guard that exempts itself is the defect it is looking for."""
    own = [ch._check("x", D9_NAME, True, D9_DETAIL)]
    checks = ch._lane_verdict_vs_prose(own)
    self_check = next(c for c in checks if c["id"] == "E.self")
    assert self_check["pass"] is False


def test_lane_e_without_a_database_is_unmeasured_not_clean(monkeypatch):
    """★ No connection means the shells were not read. 'Nothing found' and
    'nothing looked' are different answers, and only one of them is green."""
    monkeypatch.setattr(ch, "_ro_conn", lambda: None)
    checks = ch._lane_verdict_vs_prose([])
    shells = next(c for c in checks if c["id"] == "E.shells")
    assert shells["pass"] is None
    assert "NOT MEASURED" in shells["detail"]


# ── report-only ──────────────────────────────────────────────────────────────
def test_module_exposes_no_repair_path():
    """Report-only is a design constraint, not an omission. Which of two
    disagreeing numbers is canonical is a judgement call; an auto-fix would have
    to pick, and picking wrong launders a bug into canon."""
    exported = [n for n in dir(ch) if not n.startswith("_")]
    assert not [n for n in exported
                if n.startswith(("fix_", "repair_", "heal_", "apply_"))]
