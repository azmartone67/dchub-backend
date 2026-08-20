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
_AGENTS = "real_external_agents_complete_wk_wow_pct"
_CALLS = "real_external_calls_complete_wk_wow_pct"
_ROLL_A = "real_external_agents_wow_pct"


def _complete_wk_spans(today=None):
    today = today or _dt.date.today()
    mon = today - _dt.timedelta(days=today.weekday())
    return _week_spans([mon - _dt.timedelta(weeks=2),
                        mon - _dt.timedelta(weeks=1)])


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
    _mark(out, _rolling_spans(7, 2), (_ROLL_A,), "real_external_rolling_wow")
    comp = out["real_external_rolling_wow_comparability"]
    assert comp["crosses_definition_change"] is True
    assert comp["superseded_by_correction"] is False
    assert comp["quotable_as_trend"] is False
    assert out[_ROLL_A] is None


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
