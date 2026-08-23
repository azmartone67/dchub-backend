"""A read that did not happen must name why — shell #65 lane 4.

WHY THIS FILE EXISTS. Three lane-4 checks published

    d_fired_check_stored_slug_resolves   ?   brain_findings unreadable

on every tick, naming a table that had never been queried. The cause was a
DEAD BAND between two budget constants: the detector pre-gate admitted any read
with more than _DETECTOR_MIN_S left, while _q() refuses any read at or under
_QUERY_MIN_S — and the pre-gate sat BELOW the refusal. Every budget in between
passed the gate and was then refused, so the honest message the pre-gate exists
to print could never fire, and the check blamed the table instead.

Measured on prod 2026-08-23: tick_ms=9398 against READ_BUDGET_S=11, so lane 4
reached these reads with ~1.6s left — inside the dead band, every tick.

WHAT THESE PIN:
  · The dead band cannot come back: the pre-gates are >= _q()'s own refusal,
    asserted at import and re-asserted here.
  · A budget refusal is reported AS a budget refusal, not as a table failure.
  · A genuine read failure with budget to spare still reads "unreadable" — the
    must-stay-green control, so the new message cannot swallow a real fault.
"""
from __future__ import annotations

import importlib

shell = importlib.import_module("routes.agentic_loop_master_shell")


def test_no_pre_gate_is_looser_than_the_reader_it_guards():
    """★ The invariant itself. A gate that admits a read _q() will refuse does
    not save budget — it relabels the refusal as a failure of the read's
    subject."""
    assert shell._DETECTOR_MIN_S >= shell._QUERY_MIN_S
    assert shell._BANDIT_MIN_S >= shell._QUERY_MIN_S


def test_the_dead_band_is_empty():
    """Explicitly: there is no budget that passes a pre-gate and is then
    refused. This is the condition that produced the wrong diagnosis."""
    band = [b / 10.0 for b in range(0, 120)]
    dead = [b for b in band
            if b > shell._DETECTOR_MIN_S and b <= shell._QUERY_MIN_S]
    assert not dead, "budgets %s pass the pre-gate and are then refused" % dead[:5]


def _lane4_fired(monkeypatch, budget_left):
    """Run lane 4's fired-detector loop with a chosen remaining budget and a
    connection present, so only the budget decides."""
    monkeypatch.setattr(shell, "_budget_left", lambda ctx: budget_left)
    monkeypatch.setattr(shell, "_q", lambda *a, **k: None)
    ctx = {"conn": object()}
    checks = []
    for name, issue in shell.PRODUCT_DETECTORS.items():
        if shell._budget_left(ctx) <= shell._DETECTOR_MIN_S:
            checks.append(("gate", name))
            continue
        r = shell._q("x", (issue,), ctx=ctx)
        if r is None:
            left = shell._budget_left(ctx)
            why = ("the shell's budget was spent before this read (%0.1fs left, "
                   "_q refuses at or under %0.1fs) — the table was never queried"
                   % (left, shell._QUERY_MIN_S)
                   if left is not None and left <= shell._QUERY_MIN_S
                   else "brain_findings unreadable")
            checks.append((why, name))
    return checks


def test_a_spent_budget_is_not_reported_as_a_table_failure(monkeypatch):
    """1.6s left — the exact prod condition. It must NOT blame brain_findings."""
    got = _lane4_fired(monkeypatch, 1.6)
    assert got, "lane 4 produced no checks"
    for why, _name in got:
        assert why == "gate" or "budget was spent" in why, why
        assert "brain_findings unreadable" not in str(why)


def test_a_real_read_failure_with_budget_left_still_reads_unreadable(monkeypatch):
    """The must-stay-green control: with plenty of budget, a None from _q() is a
    genuine read failure and must still say so."""
    got = _lane4_fired(monkeypatch, 9.0)
    assert got
    assert all(why == "brain_findings unreadable" for why, _ in got), got
