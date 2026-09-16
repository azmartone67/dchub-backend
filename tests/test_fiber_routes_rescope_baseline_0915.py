"""The fiber_routes shrink was a re-scope, and moving a baseline is not silencing it.

★ MEASURED — Surveillance Sweep run 35034921758 (2026-09-15 23:16Z, 45cdad5b0)
hard-failed red on:

    [data_loss] CRITICAL — fiber_routes dropped 14.23%:
    baseline=67,836 (baseline_at 2026-09-10 03:23:30), current=58,183

The drop is CORRECT and it reconciles to the row:

    67,836 baseline  −  9,695 HIFLD transmission rows  =  58,141
    58,183 current   =  58,141 + 42 genuine fiber rows ingested since

#4565 stopped the fiber lane writing HIFLD power transmission lines into
fiber_routes; the owner then ran repair_fiber_routes_hifld_transmission.py to
delete the 9,695 it had already written. They were never fiber. canonical_stats
moved to 58,141 on 2026-09-13. sentinel_row_baselines did not, because it
ratchets UP and never down — the exact failure its own 'announcements' note
predicted for any table that legitimately shrinks.

★ THE LESSON: a count is not a diagnosis. 9,650 rows leaving a table looks
identical whether it was a documented re-scope, an unrelated DELETE of the same
size, or a loader that recreated the table — and a count also cannot tell you
the writer stayed removed. So the verdict gates on SHAPE (zero rows still match
the removal predicate) and on a floor that is the DOCUMENTED post-repair count,
never the current count. Comparing current against itself would be vacuous and
would mask a second loss layered on the first.

Both inputs are read from their existing homes — the predicate from the repair
script, the floor from canonical_stats — so this file also pins that they are
not re-pasted here, which is how the two would drift apart.

Static/pure: the endpoint is DB-bound, so the contract is asserted by exercising
the pure verdict directly (both branches, refusals included) plus AST over the
handler. The silence branch matters most: a check that is quiet for the wrong
reason is indistinguishable from one that works.
"""
import ast
import os

import pytest

from routes.surveillance_sweep import (
    _DRIFT_TABLES,
    _fiber_post_repair_floor,
    _fiber_removable_sql,
    fiber_shrink_verdict,
)

_SWEEP = os.path.join("routes", "surveillance_sweep.py")
_SRC = open(_SWEEP, encoding="utf-8").read()
_TREE = ast.parse(_SRC)

# The numbers this file was written against, so a future reader can see what
# "benign" actually meant on the day.
_MEASURED_CURRENT = 58183
_MEASURED_BASELINE = 67836
_MEASURED_REMOVED = 9695


def _func(name):
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {_SWEEP}")


# ── the check is NOT silenced ────────────────────────────────────────────

def test_fiber_routes_is_still_watched_for_drift():
    """The whole point: the baseline moves, the detector stays on.

    Dropping fiber_routes from _DRIFT_TABLES would turn the sweep green and
    make every future loss invisible. That is the fix this must never become.
    """
    assert "fiber_routes" in _DRIFT_TABLES


# ── the verdict gates on shape, not size ─────────────────────────────────

def test_the_measured_rescope_reads_benign():
    benign, why = fiber_shrink_verdict(_MEASURED_CURRENT, 0, 58141)
    assert benign is True
    assert "58,183" in why and "58,141" in why


def test_leftover_removable_rows_refuse_even_though_the_total_is_healthy():
    """The regression this exists to catch: the lane starts writing again.

    Total is well above the floor, so a size-only check would call this benign.
    """
    benign, why = fiber_shrink_verdict(_MEASURED_CURRENT, 1, 58141)
    assert benign is False
    assert "writing power lines again" in why


def test_a_second_loss_on_top_of_the_repair_refuses():
    """5,000 more rows gone than the removal explains — must stay red."""
    benign, why = fiber_shrink_verdict(58141 - 5000, 0, 58141)
    assert benign is False
    assert "larger than that removal explains" in why


def test_exactly_the_post_repair_count_is_benign():
    """The floor is inclusive: the repair leaving exactly 58,141 is the
    expected outcome, not a failure."""
    benign, _ = fiber_shrink_verdict(58141, 0, 58141)
    assert benign is True


@pytest.mark.parametrize("total,removable,floor", [
    ("err:UndefinedTable", 0, 58141),      # the table went missing
    (58183, "err:UndefinedColumn", 58141),  # the predicate stopped resolving
    (58183, 0, None),                       # canonical_stats unreadable
])
def test_unreadable_inputs_fail_closed(total, removable, floor):
    """No input may be treated as 'fine'. A guard that shrugs at an error is a
    guard that reports green during an outage."""
    benign, why = fiber_shrink_verdict(total, removable, floor)
    assert benign is False
    assert "refusing" in why


# ── the two numbers are read, not pasted ─────────────────────────────────

def test_the_floor_is_read_from_canonical_stats():
    from canonical_stats import _FALLBACK
    assert _fiber_post_repair_floor() == _FALLBACK["fiber_routes"]


def test_the_predicate_is_read_from_the_repair_script():
    from repair_fiber_routes_hifld_transmission import REMOVABLE_SQL
    assert _fiber_removable_sql() == REMOVABLE_SQL


def test_neither_value_is_re_pasted_as_a_literal_in_the_handler():
    """Two copies of a canonical number drift; one of them then goes stale
    while still being asserted. Only the explanatory comment may name them."""
    code = "\n".join(ln for ln in _SRC.splitlines() if not ln.lstrip().startswith("#"))
    assert "58141" not in code
    assert "hifld_tl_" not in code


# ── the rebaseline cannot run without the verdict ────────────────────────

def test_rebaseline_is_gated_on_the_verdict_and_refuses_with_409():
    handler = _func("fiber_drift_audit")
    src = ast.get_source_segment(_SRC, handler) or ""
    assert "fiber_shrink_verdict(" in src, "handler must consult the verdict"
    assert "409" in src, "a non-benign verdict must refuse, not fall through"
    # The UPDATE must sit inside the `if not benign: return` guard, i.e. after it.
    assert src.index("if not benign") < src.index("UPDATE sentinel_row_baselines"), \
        "the refusal must precede the write"


def test_the_rebaseline_only_touches_the_monitoring_table():
    """It must never write fiber_routes itself — it moves a baseline, not data."""
    src = ast.get_source_segment(_SRC, _func("fiber_drift_audit")) or ""
    for verb in ("DELETE", "INSERT INTO fiber_routes", "UPDATE fiber_routes"):
        assert verb not in src
