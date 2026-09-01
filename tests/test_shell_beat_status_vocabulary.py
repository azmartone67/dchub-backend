"""A shell that RAN and found red lanes must not beat `error`.

★ THE DEFECT (measured on the live board 2026-09-01)

/api/v1/ops/deadman carried 199 feeds. Sixteen were overdue, and their statuses:

    10  lanes_failing        the ten master shells with red lanes
     4  latest-run-failed    GH Actions workflows that actually failed
     1  never-run            a feed registered yesterday, first fire pending
     1  error   <-- agentic-loop-shell-daily, ALONE

agentic-loop's note on that row was "PASS 1 FAIL 3 ? 0 | filed 0 | rate 0.677" —
the SUCCESS-path note format. The tick ran, measured four lanes and reported
findings. It was not broken. It beat `error` anyway.

★ WHY THE WORD MATTERS. routes/ingestion_integrity_master_shell runs a
producer_liveness lane whose assertion is, verbatim, "no producer is reporting
status=error". It went RED on this shell, so ingestion-integrity-tick FAILED on
2026-08-30 and 2026-08-31 — two consecutive failures caused by nothing but the
status word on a healthy sibling. One monitor's vocabulary broke another
monitor. That is worse than a false alarm: it spends the operator's attention on
a workflow that has nothing wrong with it.

★ HOW IT GOT THERE, and why the fix is not a revert. batch-3/Screen D was RIGHT:
`ok` used to be `not tick_failed`, so a run summarising "PASS 2 FAIL 2" beat
success and the board read the shell as healthy. Deriving status from the
verdict is correct. Only the word chosen for the red case was wrong. The three
sibling shells all had it already:

    routes/relay_closure_master_shell.py:699   "lanes_failing" if failing else "success"
    routes/loop_control_master_shell.py:809    "lanes_failing" if failing else "success"
    routes/seven_levers_master_shell.py:608    "lanes_failing" if failing else "success"

★ NOTHING IS SOFTENED. lanes_failing is not success, still marks the feed overdue
on the dead-man board, and this shell — which is BORN RED by design — stays red
while its lanes are red. The only thing that changes is that a working shell
stops being counted as a crashed producer.
"""
import pytest

from routes.agentic_loop_master_shell import _beat_status


def test_all_lanes_green_beats_success():
    assert _beat_status(ok=True, tick_failed=False) == "success"


def test_red_lanes_on_a_tick_that_RAN_beats_lanes_failing():
    """The exact live case: PASS 1 FAIL 3 ? 0, tick completed."""
    assert _beat_status(ok=False, tick_failed=False) == "lanes_failing"


def test_a_tick_that_FAILED_still_beats_error():
    """`error` is not retired — it is reserved for a producer that is broken,
    which is what the consumers of this ledger understand it to mean."""
    assert _beat_status(ok=False, tick_failed=True) == "error"


def test_error_is_never_used_for_a_board_that_measured():
    """The regression, stated as the invariant rather than the instance: if the
    tick ran, the status is never `error`, whatever the lanes said."""
    for ok in (True, False):
        assert _beat_status(ok=ok, tick_failed=False) != "error"


def test_a_red_board_is_never_reported_as_success():
    """The batch-3/Screen D property this fix must NOT regress: a run with
    failing lanes must not read as healthy."""
    assert _beat_status(ok=False, tick_failed=False) != "success"
    assert _beat_status(ok=False, tick_failed=True) != "success"


VOCAB = {"success", "lanes_failing", "error"}


@pytest.mark.parametrize("ok,failed", [(True, False), (False, False), (False, True)])
def test_status_is_always_a_word_the_ledger_understands(ok, failed):
    assert _beat_status(ok, failed) in VOCAB


def test_matches_the_sibling_shells_vocabulary():
    """Cross-shell consistency, checked against the siblings' own source so this
    cannot drift into a private dialect. Ten shells with red lanes report
    lanes_failing; this one must use the same word for the same situation."""
    import pathlib
    import re
    siblings = ["relay_closure_master_shell.py", "loop_control_master_shell.py",
                "seven_levers_master_shell.py"]
    root = pathlib.Path(__file__).resolve().parent.parent / "routes"
    found = 0
    for name in siblings:
        path = root / name
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        if re.search(r'"lanes_failing"\s+if\s+\w+\s+else\s+"success"', src):
            found += 1
    assert found >= 2, (
        f"only {found} sibling shell(s) still use the lanes_failing vocabulary; "
        "if the house pattern moved, this shell must move WITH it rather than "
        "diverge — a private dialect here is what broke ingestion-integrity-tick")
    assert _beat_status(ok=False, tick_failed=False) == "lanes_failing"
