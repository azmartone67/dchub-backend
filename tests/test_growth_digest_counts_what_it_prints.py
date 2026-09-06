"""The verdict line printed five zeros under five non-zero signals.

    _vd = _act.get("verdict") or {}
    add(f"  = {_vd.get('improving', 0)} improving · ...")

read_signals() has never set a "verdict" key. By AST, the only keys it assigns
are ok / error / signals / session_upgrades_all_time / checkout_binding, so the
`or {}` was always taken and every count fell to its literal 0 default. The
operator got, on every digest, verbatim:

    · agent retention 7d: 12 (prev 9, better=up) — IMPROVING
    · ...four more...
      = 0 improving · 0 worsening · 0 flat · 0 unread · 0 withheld

The summary of the section built to answer "is anything turning" said nothing
was, immediately below the lines saying things were.

★ THE INVARIANT THAT WOULD HAVE CAUGHT IT: the five buckets partition the
signals, so they must sum to len(signals). Five zeros under five signals was
arithmetically impossible and nothing checked it. That sum is now pinned.

The second defect here is a window mismatch: agents_wk is WEEK-TO-DATE and was
differenced against the FULL prior week, so the headline manufactured a
double-digit Monday collapse that recovered by Friday with nothing underneath.
"""
import ast
import inspect
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import growth_ops_digest as god  # noqa: E402
from routes import ops_activation as oa  # noqa: E402


def _sig(label, improving=None, direction=None, better="up"):
    return {"label": label, "value": 1, "prior": 0, "better": better,
            "improving": improving, "direction": direction}


FIVE = [
    _sig("a", improving=True),
    _sig("b", improving=True),
    _sig("c", improving=False),
    _sig("d", direction="flat"),
    _sig("e", direction="withheld"),
]


def _digest(monkeypatch, signals, north=None):
    monkeypatch.setattr(god, "_activation_signals",
                        lambda: {"ok": True, "signals": signals})
    monkeypatch.setattr(god, "_shell_lanes", lambda mod: {})
    monkeypatch.setattr(god, "_north_star", lambda: north or {
        "agents_wk": 9, "agents_prev_wk": 43, "agents_prev_wtd": 8,
        "conv_30d": 2})
    d = god._build_digest()
    # _build_digest returns {subject, text, html, ...}. Read `text` by KEY --
    # falling back to str(d) renders the whole dict on one line, where
    # startswith() can never match and a substring check passes off the repr.
    assert isinstance(d, dict) and "text" in d, sorted(d) if isinstance(d, dict) else type(d)
    return d["text"]


def _verdict_line(text):
    for ln in text.splitlines():
        if "improving ·" in ln:
            return ln
    raise AssertionError("no verdict line in digest:\n" + text[:600])


# ── the five zeros ────────────────────────────────────────────────────

def test_read_signals_still_does_not_return_a_verdict_key():
    """Pins the fact the old code got wrong. If read_signals ever DOES grow a
    verdict key, this fails and the digest should be reconsidered — but until
    then, reading one is reading nothing."""
    fn = next(n for n in ast.walk(ast.parse(inspect.getsource(oa)))
              if isinstance(n, ast.FunctionDef) and n.name == "read_signals")
    keys = {n.slice.value for n in ast.walk(fn)
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
            and n.value.id == "out" and isinstance(n.slice, ast.Constant)}
    assert "verdict" not in keys, keys


def test_the_counts_match_the_marks_actually_printed(monkeypatch):
    text = _digest(monkeypatch, FIVE)
    line = _verdict_line(text)
    assert "2 improving" in line, line
    assert "1 worsening" in line, line
    assert "1 flat" in line, line
    assert "1 withheld" in line, line
    assert "0 unread" in line, line


def test_the_buckets_sum_to_the_number_of_signals(monkeypatch):
    """★ The invariant that makes five-zeros impossible. Parametrised over
    several shapes so it cannot pass by coincidence on one."""
    for signals in (FIVE, FIVE[:2], FIVE[:1],
                    [_sig("x"), _sig("y", direction="flat")],
                    [_sig(f"s{i}", improving=(i % 2 == 0)) for i in range(7)]):
        line = _verdict_line(_digest(monkeypatch, signals))
        nums = [int(n) for n in re.findall(r"(\d+) (?:improving|worsening|flat|unread|withheld)", line)]
        assert len(nums) == 5, line
        assert sum(nums) == len(signals), (
            f"{len(signals)} signal(s) but the summary accounts for "
            f"{sum(nums)}: {line}")


def test_a_non_zero_signal_set_never_summarises_to_all_zeros(monkeypatch):
    """The exact observable that shipped."""
    line = _verdict_line(_digest(monkeypatch, FIVE))
    assert "= 0 improving · 0 worsening · 0 flat · 0 unread · 0 withheld" not in line


def test_the_summary_is_not_read_from_a_separate_source(monkeypatch):
    """One ladder, one place. A second classifier is how these drifted apart."""
    src = inspect.getsource(god._build_digest)
    assert '_act.get("verdict")' not in src and "_act.get('verdict')" not in src


# ── the week-over-week window ─────────────────────────────────────────

def test_the_delta_uses_the_same_elapsed_slice_not_the_full_prior_week(monkeypatch):
    """9 week-to-date vs 8 at the same point last week is +1.
    Against the full prior week (43) it read -34, every Monday."""
    text = _digest(monkeypatch, FIVE)
    head = [l for l in text.splitlines() if l.startswith("NORTH STAR")][0]
    assert "+1" in head, head
    assert "-34" not in head, head
    assert "same point last wk" in head, head


def test_the_full_prior_week_stays_visible_as_context(monkeypatch):
    head = [l for l in _digest(monkeypatch, FIVE).splitlines()
            if l.startswith("NORTH STAR")][0]
    assert "43" in head, "the full prior week was dropped entirely: " + head
    assert "week-to-date" in head, head


def test_no_delta_is_printed_when_the_comparand_is_missing(monkeypatch):
    head = [l for l in _digest(monkeypatch, FIVE, north={
        "agents_wk": 9, "agents_prev_wk": 43, "agents_prev_wtd": None,
        "conv_30d": 2}).splitlines() if l.startswith("NORTH STAR")][0]
    assert "vs same point last wk" not in head, head


def test_the_prev_wtd_query_is_bounded_by_the_elapsed_offset():
    """Pins the SQL shape: the prior-week window must END at the same offset
    into that week, not at its end."""
    src = inspect.getsource(god._north_star)
    tree = ast.parse(src.strip())
    sql = " ".join(
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "agents" not in n.value)
    sql = " ".join(sql.split())
    assert "now() - date_trunc('week', now())" in sql, (
        "the prior-week window is not offset by the elapsed part of this week")
