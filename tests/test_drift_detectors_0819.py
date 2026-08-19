"""Guards for the two drift detectors — 2026-08-19.

★ ONE LESSON, LEARNED TWICE IN ONE DAY, NOW ENFORCED IN CODE

1. `dchub-mcp-server#202` (2026-08-18 06:31Z) changed WHO COUNTS as a real
   external caller. Measured across the deploy boundary on IP origin:

       48h BEFORE   GHA 327 / 526 calls = 62.2%   (6 distinct GHA IPs)
       ~24h SINCE   GHA   0 / 229 calls =  0.0%   (0 distinct GHA IPs)
       non-CI HELD: 199 -> 229

   weekly-series would have published that as a demand collapse. The fix was a
   MANUAL marker in `_DEFINITION_CHANGES`, which only works if a human adds
   one. `check_unmarked_population_shift` is the guard for when nobody does.

2. `r-challenge-after-value` (2026-08-15) moved the Claude-connector OAuth
   challenge from `initialize` to `tools/call`. The init-only counter decays to
   0 BY DESIGN. An operator reading it that day called "4,008 challenges -> 3
   identities" the biggest leak on the board. It was a RETIRED SERIES —
   `mcp_retention.py` documents this at its own call site and the reader still
   got it wrong, because nothing refused the division.

★ THE RULE: a ratio or delta whose window straddles a declared break is not a
rate. Refuse it. The hardest thing to test about these two detectors is their
SILENCE, so the silence branches are tested explicitly — a detector that is
quiet for the wrong reason is indistinguishable from one that works.

Static/pure: routes/brain_consistency_radar.py is enormous and DB-bound, so the
contract is asserted by AST plus direct exercise of the pure helper.
"""
import ast
import datetime as _dt
import os

import pytest

_RADAR = os.path.join("routes", "brain_consistency_radar.py")
_SRC = open(_RADAR, encoding="utf-8").read()
_TREE = ast.parse(_SRC)


def _func(name):
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _src(name):
    return ast.get_source_segment(_SRC, _func(name)) or ""


# The pure helper, exec'd without importing the module (which needs a DB).
_NS = {}
_start = _SRC.index("_SERIES_BREAKS = {")
exec(_SRC[_start:_SRC.index("def check_unmarked_population_shift")], _NS)
_straddles_break = _NS["_straddles_break"]
_SERIES_BREAKS = _NS["_SERIES_BREAKS"]


# ── the shared series-break rule ─────────────────────────────────────────────

def test_a_window_straddling_the_break_is_refused():
    """30d window on 2026-08-19 contains the 2026-08-15 switch."""
    got = _straddles_break("oauth_connector_identity", 30,
                           today=_dt.date(2026, 8, 19))
    assert got == "2026-08-15"


def test_a_window_clear_of_the_break_is_allowed():
    """★ FALSE BRANCH. If this never returned None the step would be dead
    code forever, and 'silent' would mean 'broken', not 'careful'."""
    assert _straddles_break("oauth_connector_identity", 30,
                            today=_dt.date(2026, 9, 20)) is None


def test_the_break_boundary_is_exclusive_on_the_old_side():
    """The exact day the step becomes measurable again.

    ★ The first draft of this test read `== "2026-08-15" or True`, which cannot
    fail — and it asserted the wrong day besides. A 30d look-back from D opens
    at D-30, and the guard is `brk > D-30`, so a break on 08-15 leaves the
    window when D-30 reaches 08-15, i.e. D = 2026-09-14. Not 09-15.
    """
    assert _straddles_break("oauth_connector_identity", 30,
                            today=_dt.date(2026, 9, 13)) == "2026-08-15"
    assert _straddles_break("oauth_connector_identity", 30,
                            today=_dt.date(2026, 9, 14)) is None


def test_an_unknown_step_has_no_break_and_is_not_silently_refused():
    assert _straddles_break("no_such_step", 30) is None


def test_a_malformed_break_date_does_not_refuse_everything():
    ns = {}
    exec(_SRC[_start:_SRC.index("def check_unmarked_population_shift")], ns)
    ns["_SERIES_BREAKS"] = {"x": "not-a-date"}
    assert ns["_straddles_break"]("x", 30) is None


# ── detector 1: unmarked population shift ────────────────────────────────────

def test_population_detector_exists_and_is_registered():
    assert _func("check_unmarked_population_shift")
    assert _SRC.count("check_unmarked_population_shift") >= 2, (
        "defined but never added to the sweep tuple — it would never run")


def test_it_requires_BOTH_a_class_collapse_and_a_held_residual():
    """★ The discriminator. Demand collapse moves everything; a definition
    change deletes one class and leaves the rest. Without the residual test
    this fires on every quiet week."""
    src = _src("check_unmarked_population_shift")
    assert "_PRIOR_SHARE_FLOOR" in src and "_NOW_SHARE_CEIL" in src
    assert "_RESIDUAL_LO" in src and "_RESIDUAL_HI" in src
    assert "if not (collapsed and held):" in src, (
        "the two conditions must be ANDed — either alone is not the signature")


def test_a_declared_change_silences_it():
    """That is the entire point of declaring one.

    ★ ASSERTED ON THE `if` NODE, NOT ON THE TEXT. The first draft did
    `src.split("_changes_in")[1][:200]` and SURVIVED a mutation to `if False:`
    — because `_changes_in` also appears on the import line, so the split
    matched there and found an unrelated `return findings` downstream. Third
    time today an assertion was satisfied by the wrong occurrence. Walk the
    tree: find the `If` whose test actually calls `_changes_in`.
    """
    fn = _func("check_unmarked_population_shift")
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.If)
              and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                      and c.func.id == "_changes_in"
                      for c in ast.walk(n.test))]
    assert guards, ("no `if _changes_in(...)` guard — a declared change does "
                    "not suppress the finding")
    for g in guards:
        assert any(isinstance(s, ast.Return) for s in g.body), (
            "the guard must RETURN, not merely annotate")


def test_unreadable_ranges_yield_no_finding_rather_than_a_clean_verdict():
    src = _src("check_unmarked_population_shift")
    assert "if not nets:" in src
    assert "UNMEASURED" in src


def test_it_records_the_two_signals_that_did_not_work():
    """Both were tried against live data and failed. Without this note the
    next person rebuilds them."""
    src = _src("check_unmarked_population_shift")
    assert "PASS-RATE" in src and "BURST COUNT" in src
    assert "47.6" in src, "the measured pass-rate counter-example is missing"


def test_it_states_its_own_blind_spot():
    src = _src("check_unmarked_population_shift")
    assert "LIMIT, stated" in src, (
        "CI is the only identifiable class; a detector that hides its coverage "
        "gap reads as full coverage")


# ── detector 2: funnel step collapse ─────────────────────────────────────────

def test_funnel_detector_exists_and_is_registered():
    assert _func("check_funnel_step_collapse")
    assert _SRC.count("check_funnel_step_collapse") >= 2


def test_it_refuses_the_ratio_across_the_series_break():
    """★ THE SILENCE THAT MATTERS. This is the exact division that produced
    '4,008 -> 3 is our biggest leak' off a retired series."""
    src = _src("check_funnel_step_collapse")
    i = src.index("_straddles_break")
    assert "return findings" in src[i:i + 200], (
        "must return BEFORE querying — computing then discarding still risks "
        "someone logging the number")
    assert src.index("_straddles_break") < src.index("SELECT"), (
        "the break check must precede the query")


def test_dormant_gateway_is_not_zero_conversions():
    src = _src("check_funnel_step_collapse")
    assert "if not beats:" in src
    assert "DORMANT" in src


def test_it_has_an_input_floor_so_a_quiet_step_is_not_a_collapse():
    src = _src("check_funnel_step_collapse")
    assert "_INPUT_FLOOR" in src
    assert "challenges < _INPUT_FLOOR" in src


def test_zero_identities_does_not_crash_and_counts_as_collapse():
    """inf is the correct ratio for N inputs and no outputs — not a ZeroDivision
    and not a skip."""
    src = _src("check_funnel_step_collapse")
    assert 'float("inf")' in src


def test_it_names_where_to_look():
    src = _src("check_funnel_step_collapse")
    assert "_claudeChallengeEligible" in src


@pytest.mark.parametrize("fn", ["check_unmarked_population_shift",
                                "check_funnel_step_collapse"])
def test_every_detector_closes_its_connection(fn):
    """check_unsafe_db_conn_pattern exists because these leak."""
    src = _src(fn)
    assert "finally:" in src and "conn.close()" in src


@pytest.mark.parametrize("fn", ["check_unmarked_population_shift",
                                "check_funnel_step_collapse"])
def test_every_detector_emits_a_stable_issue_key(fn):
    src = _src(fn)
    assert '"issue":' in src
    assert '"count":' in src, "the board ranks on count"
