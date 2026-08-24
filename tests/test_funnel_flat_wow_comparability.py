"""Guard for the funnel's FLAT *_wow_pct keys — flask_mcp_endpoints (2026-08-20).

★ THE DEFECT THIS GUARD EXISTS TO RETIRE

/api/v1/reports/weekly-series learned on 2026-08-19/20 to refuse a delta across
a definition change: `crosses_definition_change` for a change landing inside a
week, `superseded_by_correction` for a correction landing after every week.

The funnel payload never asked. It publishes SEVEN bare `*_wow_pct` scalars —

    real_external_agents_complete_wk_wow_pct   real_external_agents_wow_pct
    real_external_calls_complete_wk_wow_pct    real_external_calls_wow_pct
    real_external_signals_wow_pct              tool_calls_wow_pct

— with no comparability attached to any of them, and the funnel DASHBOARD
renders those scalars directly. So after the source and the press headline were
both fixed, the screen still read:

    "+89.5% WoW on COMPLETE weeks (38 -> 72) — the trend number"

beside a rolling "-28.8%" crash. Both across dchub-mcp-server#202
(2026-08-18 06:31Z), which removed DC Hub's own GitHub Actions — 72.1% of
agents / 80.4% of calls in the 7d before it. A flag the renderer does not read
changes nothing, which is why these keys are NULLED, not merely annotated.

The two pairs fail via DIFFERENT hazards, and the guard pins both:
  * complete weeks (08-03, 08-10) both END before the correction -> SUPERSEDED
  * rolling 7d ending now CONTAINS the correction, prior window does not
    -> CROSSES

Pure functions: no DB, no network, no Flask app.
"""
import datetime as _dt
from datetime import datetime, timezone

_SRC = open("flask_mcp_endpoints.py", encoding="utf-8").read()
_NS = {"datetime": datetime, "timezone": timezone}
exec(_SRC[_SRC.index("def _week_spans"):_SRC.index("def _build_press_headline")], _NS)

_week_spans = _NS["_week_spans"]
_rolling_spans = _NS["_rolling_spans"]
_mark = _NS["_mark_wow_comparability"]

_CHANGE_AT = _dt.datetime(2026, 8, 18, 6, 31, tzinfo=_dt.timezone.utc)

# ★2026-08-24 — THE SPANS ARE PINNED, NOT LIVE, and that is the whole point.
# _CHANGE_AT is a FIXED historical instant, so a span built from the wall clock
# silently changes WHICH SCENARIO this file tests. It did: _complete_wk_spans()
# read date.today(), and when UTC rolled into Monday 2026-08-24 the two
# "complete weeks" advanced to 08-10 and 08-17 — and 08-17..08-24 CONTAINS the
# correction, so the SUPERSEDED pair began reporting CROSSES and `unit-tests`
# went red on main for every open PR. The product was right both times; only the
# fixture had moved.
#
# date.today() is LOCAL, not UTC, so it detonated at a different instant on
# every machine — a US/Mountain laptop still passed for seven hours after CI
# started failing, which is the worst possible way to learn a guard has rotted.
#
# _ASOF sits in the week AFTER the correction, so the complete weeks are 08-03
# and 08-10 — both ending before it, exactly the scenario the module docstring
# describes. test_the_pinned_asof_still_describes_the_documented_scenario below
# asserts that by its defining PROPERTY, so the anchor cannot quietly stop
# meaning what it says.
_ASOF_DATE = _dt.date(2026, 8, 20)          # Thu of the week the guard was written
_ASOF = _dt.datetime(2026, 8, 20, 12, 0, tzinfo=_dt.timezone.utc)
_AGENTS = "real_external_agents_complete_wk_wow_pct"
_CALLS = "real_external_calls_complete_wk_wow_pct"
_ROLL_A = "real_external_agents_wow_pct"


def _complete_wk_spans(today=None):
    today = today or _ASOF_DATE
    mon = today - _dt.timedelta(days=today.weekday())
    return _week_spans([mon - _dt.timedelta(weeks=2),
                        mon - _dt.timedelta(weeks=1)])


def _rolling_spans_asof(days=7, count=2, now=None):
    """flask_mcp_endpoints._rolling_spans' arithmetic, at a PINNED `now`.

    The shipped function reads the wall clock and takes no `now`, so a test that
    needs a window positioned relative to _CHANGE_AT cannot use it directly: the
    live trailing 7d window stops containing a fixed 2026-08-18 06:31Z instant
    at 2026-08-25 06:31Z, which would have detonated this file a second time
    about thirty hours after the first.

    The shipped function itself stays covered — for shape by
    test_rolling_spans_are_consecutive_and_most_recent_first against the live
    clock, and for ARITHMETIC by test_the_pinned_rolling_mirror_matches_the_
    shipped_arithmetic, which replays it here and demands an exact match. A
    mirror nobody compares is just a second implementation waiting to disagree.
    """
    now = now or _ASOF
    return [(now - _dt.timedelta(days=days * (i + 1)),
             now - _dt.timedelta(days=days * i))
            for i in range(count)]


# ── the span builders ────────────────────────────────────────────────────────

def test_week_spans_are_half_open_monday_to_monday():
    spans = _week_spans([_dt.date(2026, 8, 10)])
    lo, hi = spans[0]
    assert lo == _dt.datetime(2026, 8, 10, tzinfo=_dt.timezone.utc)
    assert hi == _dt.datetime(2026, 8, 17, tzinfo=_dt.timezone.utc)
    assert lo.tzinfo is not None and hi.tzinfo is not None, (
        "a naive span cannot be compared to an aware effective_at")


def test_rolling_spans_are_consecutive_and_most_recent_first():
    spans = _rolling_spans(7, 2)
    assert len(spans) == 2
    assert spans[0][0] == spans[1][1], "windows must abut, not overlap or gap"
    assert spans[0][1] > spans[1][1]
    assert (spans[0][1] - spans[0][0]).days == 7


# ── the complete-week pair: SUPERSEDED ───────────────────────────────────────

def test_the_live_plus_89_5_is_withheld_not_rendered():
    """★ THE REGRESSION — the exact scalar the dashboard called "the trend"."""
    out = {_AGENTS: 89.5, _CALLS: -11.3, "real_external_agents_complete_wk": 72}
    _mark(out, _complete_wk_spans(), (_AGENTS, _CALLS),
          "real_external_complete_wk")
    assert out[_AGENTS] is None, "the dashboard renders this scalar directly"
    assert out[_CALLS] is None
    comp = out["real_external_complete_wk_comparability"]
    assert comp["superseded_by_correction"] is True
    assert comp["quotable_as_trend"] is False
    assert comp["superseded_by"][0]["ref"] == "dchub-mcp-server#202"


def test_the_arithmetic_is_kept_under_a_name_that_says_what_it_is():
    """Withholding must not DESTROY the number — only stop it being quoted."""
    out = {_AGENTS: 89.5, _CALLS: -11.3}
    _mark(out, _complete_wk_spans(), (_AGENTS, _CALLS),
          "real_external_complete_wk")
    assert out[f"{_AGENTS}_withheld"] == 89.5
    assert out[f"{_CALLS}_withheld"] == -11.3
    assert "real_external_complete_wk_withheld_reason" in out
    assert "SUPERSEDED" in out["real_external_complete_wk_withheld_reason"]


def test_the_level_keys_are_never_touched():
    """★ Only the DELTA is withheld. 72 is a true count of a real week."""
    out = {_AGENTS: 89.5,
           "real_external_agents_complete_wk": 72,
           "real_external_agents_prior_complete_wk": 38,
           "real_external_calls_complete_wk": 2100}
    _mark(out, _complete_wk_spans(), (_AGENTS,), "real_external_complete_wk")
    assert out["real_external_agents_complete_wk"] == 72
    assert out["real_external_agents_prior_complete_wk"] == 38
    assert out["real_external_calls_complete_wk"] == 2100


# ── the rolling pair: CROSSES (a different hazard) ───────────────────────────

def test_rolling_pair_is_withheld_as_CROSSING_not_superseded():
    """The window ending now CONTAINS the correction; the prior does not.

    Distinguishing the two matters: a reader told "these weeks predate a
    correction" about a window that straddles one would go looking for the
    wrong thing.
    """
    out = {_ROLL_A: -27.7}
    _mark(out, _rolling_spans_asof(7, 2), (_ROLL_A,), "real_external_rolling_wow")
    comp = out["real_external_rolling_wow_comparability"]
    assert comp["crosses_definition_change"] is True
    assert comp["superseded_by_correction"] is False
    assert comp["quotable_as_trend"] is False
    assert out[_ROLL_A] is None


# ── the anchor itself ───────────────────────────────────────────────────────

def test_the_pinned_asof_still_describes_the_documented_scenario():
    """Pin the two scenarios by their DEFINING PROPERTY, not by a remembered date.

    A pinned date is only as good as the relationship it encodes. Asserting
    "_ASOF == 2026-08-20" would pass forever while meaning nothing; these
    assertions fail the moment the anchor stops producing the SUPERSEDED and
    CROSSES setups the tests below are named for.
    """
    wk = _complete_wk_spans()
    assert [lo.date() for lo, _ in wk] == [_dt.date(2026, 8, 3), _dt.date(2026, 8, 10)], (
        "the complete-week pair is no longer 08-03/08-10 — the SUPERSEDED "
        "scenario has moved out from under the tests that assert it")
    assert all(hi <= _CHANGE_AT for _, hi in wk), (
        "SUPERSEDED requires BOTH complete weeks to END before the correction")

    roll = _rolling_spans_asof(7, 2)
    assert roll[0][0] <= _CHANGE_AT < roll[0][1], (
        "CROSSES requires the recent rolling window to CONTAIN the correction")
    assert not (roll[1][0] <= _CHANGE_AT < roll[1][1]), (
        "...and the prior window not to — otherwise the two hazards are "
        "indistinguishable and the test proves nothing")


def test_the_pinned_rolling_mirror_matches_the_shipped_arithmetic():
    """The mirror must not drift from _rolling_spans.

    Replay the shipped function's own output through the mirror: feeding it the
    live `now` (which is spans[0][1] by construction) must reproduce the live
    result exactly. If the shipped windowing changes, this fails instead of the
    mirror silently testing arithmetic the product no longer performs.
    """
    live = _rolling_spans(7, 2)
    assert _rolling_spans_asof(7, 2, now=live[0][1]) == live, (
        "_rolling_spans_asof no longer mirrors flask_mcp_endpoints._rolling_spans")


# ── FALSE BRANCHES — a guard that fires on everything is not a guard ─────────

def test_a_clean_post_correction_pair_is_left_alone():
    """★ THE FALSE BRANCH. If this withheld too, no trend could ever ship."""
    spans = _week_spans([_dt.date(2026, 9, 7), _dt.date(2026, 9, 14)])
    out = {_AGENTS: 12.5}
    _mark(out, spans, (_AGENTS,), "real_external_complete_wk")
    assert out[_AGENTS] == 12.5, "an unaffected delta must publish unchanged"
    assert out["real_external_complete_wk_comparability"]["quotable_as_trend"] is True
    assert f"{_AGENTS}_withheld" not in out
    assert "real_external_complete_wk_withheld_reason" not in out


def test_an_already_null_pct_gets_no_withheld_twin():
    """A zero baseline already yields None — that is not a withholding."""
    out = {_AGENTS: None}
    _mark(out, _complete_wk_spans(), (_AGENTS,), "real_external_complete_wk")
    assert out[_AGENTS] is None
    assert f"{_AGENTS}_withheld" not in out
    assert "real_external_complete_wk_withheld_reason" not in out, (
        "nothing was withheld, so naming a reason would be a false explanation")


def test_marking_is_fail_soft():
    """Metadata about honesty must never cost the payload."""
    out = {_AGENTS: 89.5}
    _mark(out, "not-a-span-list", (_AGENTS,), "real_external_complete_wk")
    assert out[_AGENTS] == 89.5, "a broken marker must not null a real number"


def test_missing_keys_are_not_invented():
    out = {}
    _mark(out, _complete_wk_spans(), (_AGENTS,), "real_external_complete_wk")
    assert _AGENTS not in out
    assert "real_external_complete_wk_comparability" in out


# ── the wiring: the shipped call sites must actually pass these keys ────────

def _marked_keys():
    """Every string constant passed as an ARGUMENT to _mark_wow_comparability.

    ★ Substring search over the whole file does NOT work here and fails in the
    dangerous direction: each of these keys also appears on its own
    `out[...] = ...` assignment line, so `'"key" in _SRC'` stays true after the
    call site is unwired. A mutation that replaced the key tuple with () left
    the first draft of this test GREEN. Read the call's arguments.
    """
    import ast
    keys = set()
    for node in ast.walk(ast.parse(_SRC)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_mark_wow_comparability"):
            for arg in node.args:
                for c in ast.walk(arg):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        keys.add(c.value)
    return keys


def test_both_pct_pairs_are_wired_in_the_funnel_payload():
    """★ A helper nothing calls is not a fix.

    Asserts the shipped source passes each dashboard-rendered scalar to
    _mark_wow_comparability, so unwiring a call site fails HERE rather than
    silently restoring the bare number to the screen.
    """
    marked = _marked_keys()
    assert marked, "no _mark_wow_comparability call site found — fence is blind"
    for key in (_AGENTS, _CALLS, _ROLL_A, "real_external_calls_wow_pct"):
        assert key in marked, (
            f"{key} is rendered by the dashboard but never passed to the "
            f"comparability marker — it will ship as a bare scalar")


def test_the_basis_string_no_longer_recommends_the_delta_unconditionally():
    """The 'prefer this for any trend claim' sentence IS what got quoted."""
    i = _SRC.index("COMPLETE ISO weeks (Mon-Sun)")
    basis = _SRC[i:i + 1600]
    assert "quotable_as_" in basis, (
        "the basis still tells readers to prefer this delta without naming "
        "the condition under which it is quotable")


# ── the THIRD surface, found by nulling the first two ────────────────────────
# ★ 2026-08-20 — static/mcp-dashboard.html walks a FALLBACK CHAIN for its
# agents WoW: complete-week key -> rolling key -> fetch /api/v1/reach and read
# wow.real_agents_pct. Withholding the first two (#2978) routed the card
# straight to that third pair, which had NO comparability, and it rendered
# -26.6% across #202 with no caveat. Nulling a number is only safe if every
# link the renderer falls through to is guarded the same way.

_DASH = open("static/mcp-dashboard.html", encoding="utf-8").read()


def test_reach_wow_pair_is_wired_to_the_marker():
    """/api/v1/reach.wow is the dashboard's last fallback — it must refuse too."""
    marked = _marked_keys()
    for key in ("real_agents_pct", "real_calls_pct"):
        assert key in marked, (
            f"/api/v1/reach wow.{key} is the dashboard's fallback and is not "
            f"passed to the comparability marker — nulling the funnel keys "
            f"just routes the same uncaveated delta through this one")


def test_reach_wow_uses_the_same_rolling_windows_as_the_funnel_pair():
    """Same 7d-vs-prior-7d division, so it must carry the same verdict."""
    out_reach, out_funnel = {"real_agents_pct": -26.6}, {_ROLL_A: -27.7}
    _mark(out_reach, _rolling_spans(7, 2), ("real_agents_pct",), "rolling")
    _mark(out_funnel, _rolling_spans(7, 2), (_ROLL_A,), "real_external_rolling_wow")
    a = out_reach["rolling_comparability"]
    b = out_funnel["real_external_rolling_wow_comparability"]
    assert a["quotable_as_trend"] == b["quotable_as_trend"] is False
    assert a["crosses_definition_change"] == b["crosses_definition_change"] is True
    assert out_reach["real_agents_pct"] is None


# ── the renderer: a withheld delta must be NAMED, not fallen through ────────

def test_dashboard_names_a_withheld_delta_before_any_fallback():
    """★ ORDER IS THE GUARD.

    The withheld branch must run BEFORE the rolling-key fallback and before
    the /api/v1/reach fetch. If it ran after, a withheld delta would be
    indistinguishable from a missing one and the card would silently reach for
    the next number instead of explaining the refusal.
    """
    withheld = _DASH.index("quotable_as_trend === false")
    roll_fb = _DASH.index("if (!agentsWowTxt) agentsWowTxt = "
                          "_pct(d.real_external_agents_wow_pct);")
    reach_fb = _DASH.index("/api/v1/reach?_cb=")
    assert withheld < roll_fb < reach_fb, (
        "the withheld branch must precede both fallbacks")


def test_dashboard_reads_quotable_as_trend_not_a_bare_presence_check():
    """`if (comp)` would be true for a QUOTABLE verdict too and suppress a
    perfectly good delta. The branch must test the boolean."""
    assert "quotable_as_trend === false" in _DASH


def test_dashboard_still_null_guards_the_pct_helper():
    """Nulling the keys must not print 'null%' — _pct returns '' on null."""
    assert "(v == null) ? ''" in _DASH


def test_dashboard_says_the_levels_are_unaffected():
    """A refusal that does not say what IS still true reads as an outage."""
    i = _DASH.index("WoW withheld")
    assert "Levels below are unaffected" in _DASH[i:i + 900]


# ── the last two of the seven ────────────────────────────────────────────────
# ★ 2026-08-20 — found by RENDERING the page after #2980 deployed, not by
# reading the payload. The TOOL CALLS card still printed "WoW -19.7%": seven
# flat *_wow_pct keys were named in #2978's own description and only four were
# wired. Two were left, and one of them was on screen.

def test_tool_calls_wow_is_wired():
    """Built on _deloop_real_calls_predicate() — exactly what #202 changed."""
    assert "tool_calls_wow_pct" in _marked_keys(), (
        "the TOOL CALLS card renders this and it is computed on the predicate "
        "the correction moved")


def test_signals_wow_is_wired():
    """mcp_funnel_real excludes `mcp_client LIKE 'dchub-%'`, so post-#202 CI
    signals leave the view while pre-#202 they wrote the generic 'mcp', which
    it does not exclude — the population moved at the same instant."""
    assert "real_external_signals_wow_pct" in _marked_keys()


def test_all_seven_flat_wow_keys_are_accounted_for():
    """★ THE COMPLETENESS FENCE.

    Every `out["..._wow_pct"] = ` assignment in the funnel payload must be
    passed to the marker. Enumerated from the SOURCE, not from a hand-written
    list, so a NEW *_wow_pct key added later fails here instead of shipping
    bare the way these seven did.
    """
    import ast, re
    marked = _marked_keys()
    assigned = set()
    for node in ast.walk(ast.parse(_SRC)):
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if (isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                    and isinstance(t.slice.value, str)
                    and re.search(r"_(wow_)?pct$", t.slice.value)
                    and "wow" in t.slice.value):
                assigned.add(t.slice.value)
    assert assigned, "found ZERO *_wow_pct assignments — fence is blind"
    missing = assigned - marked - {k + "_withheld" for k in marked}
    assert not missing, (
        f"these *_wow_pct keys ship with no comparability and a renderer will "
        f"print them bare: {sorted(missing)}")


def test_tool_calls_pair_uses_date_anchored_spans():
    """The complete-DAYS variant is [CURRENT_DATE-7d, CURRENT_DATE), not
    [now-7d, now). Anchoring it to NOW would shift both bounds by the time of
    day and could place a boundary correction in the wrong window."""
    import datetime as d
    t0 = d.date.today()
    spans = _week_spans([t0 - d.timedelta(days=14), t0 - d.timedelta(days=7)])
    assert spans[0][0].time() == d.time.min, "must start at midnight UTC"
    assert spans[1][1] == d.datetime.combine(t0, d.time.min,
                                             tzinfo=d.timezone.utc), (
        "the current window must end at CURRENT_DATE, excluding today")
