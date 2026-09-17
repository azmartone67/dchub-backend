"""Lane F: money arrived and the funnel attributes none of it.

MEASURED 2026-09-17, /api/v1/mcp/handoff-funnel:

    30d   human_acted 7  ->  identified 0  ->  paid_attributed 0
     7d   human_acted 7  ->  identified 0  ->  paid_attributed 0
    relayed_checkout_payments {payments: 0, matched: 0, attributable: 0}

Stripe can show a sale while `paid_attributed` reads 0, and nothing on the
dead-man board said so. This lane says it — but only when it is true.

★★ THE CONTROL IS `payments`, AND IT IS THE POINT. A funnel that sold nothing
publishes paid_attributed 0 because that IS the answer. A lane that went red on
that would be permanently red on a true statement, which is precisely the
failure routes/lane_triage was written about: a board where most red is
structurally unclearable trains everyone to scroll past all of it. So zero
payments is `?`, never FAIL.

The verdict is a PURE function, tested here against every branch. The lane
function's own job is to feed it counts built by routes/handoff_definition —
never restated here — so the lane and the funnel it alarms on cannot drift.
"""
import ast
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = (ROOT / "routes" / "relay_closure_master_shell.py").read_text(encoding="utf-8")

from routes.relay_closure_master_shell import (  # noqa: E402
    MONEY_WINDOW_DAYS, verdict_money_reaches_the_funnel as V)


# ── the control ──────────────────────────────────────────────────────────
def test_no_payment_is_not_a_failure():
    """★ Nothing was bought, so 0 attributed is CORRECT."""
    status, note = V(payments=0, human_acted=7, identified=0,
                     paid_attributed=0, matched=0, attributable=0)
    assert status == "?"
    assert "correct" in note.lower()


def test_an_unreadable_payment_count_is_unknown_never_zero():
    """A failed read that renders as 0 would fire this lane on an outage."""
    status, note = V(payments=None, human_acted=None, identified=None,
                     paid_attributed=None, matched=None, attributable=None)
    assert status == "?"
    assert "unreadable" in note.lower()


def test_human_acted_rising_alone_does_not_fire_it():
    """★ The shape Grok named — human_acted up, paid 0 — is NOT evidence on
    its own. Without a known checkout there is nothing to have lost."""
    assert V(payments=0, human_acted=7, identified=0,
             paid_attributed=0, matched=0, attributable=0)[0] == "?"
    assert V(payments=0, human_acted=999, identified=0,
             paid_attributed=0, matched=0, attributable=0)[0] == "?"


# ── the alarm ────────────────────────────────────────────────────────────
def test_a_payment_with_nothing_attributed_is_a_failure():
    status, note = V(payments=1, human_acted=7, identified=0,
                     paid_attributed=0, matched=1, attributable=1)
    assert status == "FAIL"
    assert "1 paid checkout" in note


def test_a_payment_that_reached_the_stage_passes():
    status, note = V(payments=1, human_acted=7, identified=1,
                     paid_attributed=1, matched=1, attributable=1)
    assert status == "PASS"
    assert "1 attributed" in note


@pytest.mark.parametrize("matched,attributable,expect", [
    (0, 0, "did not arrive through a relayed link"),
    (2, 0, "NONE of those clicks carried a session"),
    (2, 2, "self-traffic exclusion"),
])
def test_the_failure_names_which_half_of_the_join_broke(matched, attributable, expect):
    """★ Actionable, not merely red. The three causes want three different
    fixes — a missing click, a missing session field, an over-broad exclusion."""
    status, note = V(payments=2, human_acted=7, identified=0,
                     paid_attributed=0, matched=matched, attributable=attributable)
    assert status == "FAIL"
    assert expect in note


def test_the_note_always_carries_the_side_by_side_rungs():
    """Grok's item 6: relays | human_acted | identified | paid_attributed read
    together. A verdict that names only its own number hides the shape."""
    for kwargs in ({"payments": 0, "matched": 0, "attributable": 0},
                   {"payments": 3, "matched": 0, "attributable": 0}):
        _s, note = V(human_acted=7, identified=0, paid_attributed=0, **kwargs)
        assert "human_acted=7" in note
        assert "identified=0" in note


# ── the lane feeds it from the SSOT ──────────────────────────────────────
def _lane_body():
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_lane_f_money")
    body = ast.get_source_segment(SRC, fn) or ""
    assert "verdict_money_reaches_the_funnel" in body, (
        "the scanned segment is not the lane body")
    return body


def test_every_count_is_built_by_handoff_definition():
    """★ The lane alarms ON the published funnel, so it must read the funnel's
    own SQL. A hand-written count here could go red while the dashboard it
    points at reads green, or the reverse."""
    body = _lane_body()
    for builder in ("_relayed_checkout_payments_sql(",
                    "_human_acted_count_sql(",
                    "_paid_attributed_count_sql("):
        assert builder in body, "%s is not called — a count was restated" % builder
    # And those names resolve to handoff_definition, not to a local copy.
    assert "from routes.handoff_definition import (" in SRC
    for name in ("human_acted_count_sql as _human_acted_count_sql",
                 "paid_attributed_count_sql as _paid_attributed_count_sql",
                 "relayed_checkout_payments_sql as _relayed_checkout_payments_sql"):
        assert name in SRC


def test_the_lane_is_actually_assembled_in_both_branches():
    """★ A lane nothing appends reports nothing — and the no-database branch
    must carry it too, or the board silently loses a lane on an outage."""
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_state")
    body = ast.get_source_segment(SRC, fn) or ""
    assert "_lane_f_money(cur)" in body
    assert '"lane": "F/money_reaches_the_funnel", "status": "?"' in body


def test_a_fail_counts_as_red():
    """The shell's `reds` list is what the dead-man board reads."""
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "_state")
    body = ast.get_source_segment(SRC, fn) or ""
    assert '"FAIL"' in body and 'out["reds"]' in body


def test_the_window_is_the_seven_days_the_brief_asked_for():
    assert MONEY_WINDOW_DAYS == 7
