"""Stability master shell (#55) — lane logic and the fail-open directions.

The shell is the scoreboard for the 2026-08-20 drift audit. These tests pin the
properties that make a scoreboard trustworthy, which are mostly about what it
must REFUSE to do:

  - never render green on a failed read ("?" is not a soft pass)
  - never let one lane's exception 5xx the tick
  - never return 5xx from the kill switch (the CF worker fails the whole site
    over to stale Render on any 5xx from Railway)

Extraction is asserted non-empty before anything is asserted about it — an
empty extraction satisfies every "the bad thing is absent" check for free.
"""
from __future__ import annotations

import ast
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "routes", "stability_master_shell.py")


def _load():
    src = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    ns: dict = {}
    want_fn = {"_lane_verdict", "_check"}
    want_val = {"_MUST_BE_REQUIRED", "_MERGES_PER_DAY_CEILING"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want_fn:
            exec(compile(ast.Module([node], []), _SRC, "exec"), ns)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in want_val:
                    exec(compile(ast.Module([node], []), _SRC, "exec"), ns)
    missing = (want_fn | want_val) - set(ns)
    assert not missing, (
        f"EXTRACTION EMPTY: {sorted(missing)} not found in {_SRC} — every "
        f"assertion below would pass vacuously."
    )
    return ns


NS = _load()
_verdict = NS["_lane_verdict"]
_check = NS["_check"]


# ── "?" is a real verdict, never a soft pass ─────────────────────────────

def test_a_lane_whose_reads_all_failed_is_not_green():
    """The single most important property. Lanes A/C/E depend on a GitHub
    token or the DB, and GH_TOKEN was deleted from Railway — if an unreadable
    lane rendered PASS, the board would report health it never observed."""
    checks = [_check("x", "readable", None, "no token", critical=True)]
    assert _verdict(checks) == "?"


def test_a_failed_check_beats_everything():
    checks = [_check("a", "read", True, "ok", critical=True),
              _check("b", "invariant", False, "violated")]
    assert _verdict(checks) == "FAIL"


def test_an_unmeasured_critical_read_blocks_a_pass():
    """A lane that could not do its critical read must not pass on the
    strength of its cheap non-critical ones."""
    checks = [_check("a", "read", None, "unreadable", critical=True),
              _check("b", "something else", True, "fine")]
    assert _verdict(checks) == "?"


def test_a_fully_verified_lane_passes():
    checks = [_check("a", "read", True, "ok", critical=True),
              _check("b", "invariant", True, "held")]
    assert _verdict(checks) == "PASS"


# ── the shell must not become a hazard itself ────────────────────────────

def test_kill_switch_never_returns_5xx():
    """Repo-wide guard in tests/test_shell_killswitch_never_5xx.py covers all
    shells; this pins it for THIS one with the reason attached, because the
    consequence is specific and severe: the CF worker's proxyWithRetry reads
    any 5xx from Railway as a dead origin and fails the site over to the stale
    Render backend."""
    src = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    codes = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and isinstance(node.test.func, ast.Name)
                and node.test.func.id == "_disabled"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Tuple):
                    for el in sub.value.elts:
                        if isinstance(el, ast.Constant) and isinstance(el.value, int):
                            codes.append(el.value)
    assert codes, "EXTRACTION EMPTY: no kill-switch return codes found"
    assert all(c < 500 for c in codes), (
        f"kill switch returns {codes}; a 5xx here fails the whole site over to "
        f"stale Render"
    )


def test_every_lane_is_wrapped_so_one_failure_cannot_5xx_the_tick():
    """_tick must catch per-lane exceptions. An unhandled raise in one lane
    would 500 the endpoint, which is the same site-failover hazard as the kill
    switch returning 503."""
    src = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    tick = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_tick"]
    assert tick, "EXTRACTION EMPTY: _tick not found"
    handlers = [n for n in ast.walk(tick[0]) if isinstance(n, ast.Try)]
    assert handlers, (
        "_tick calls every lane with no try/except — one raising lane would "
        "500 the tick and trip the CF failover"
    )


# ── the invariants are invariants, not values ────────────────────────────

def test_the_ceiling_is_declared_not_discovered():
    """Lane C's bound must be a stated target. If it were set to the measured
    rate it would pass by construction — the failure mode contract healer #44
    names as 'invariants≠values'."""
    ceiling = NS["_MERGES_PER_DAY_CEILING"]
    assert isinstance(ceiling, int) and ceiling > 0
    assert ceiling < 36, (
        "the audit measured ~36 backend merges/day and called that rate the "
        "disease; a ceiling at or above it would render the lane green on day "
        "one and measure nothing"
    )


def test_the_gate_list_is_not_empty():
    """Lane A checks that gates are REQUIRED. An empty list would make the
    lane vacuously green — it would verify nothing while looking healthy."""
    gates = NS["_MUST_BE_REQUIRED"]
    assert gates, "no gates listed; lane A would pass vacuously"
    assert "app-contract-gate" in gates


def test_the_shell_is_report_only():
    """Lanes C and D are working-practice and autonomy-scope decisions, and
    lane A mutates branch protection. None may be auto-actioned by a
    diagnostic, so the shell must not carry write verbs."""
    src = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name in ("post", "put", "patch", "delete"):
                # bp.route decorators register handlers; that is not a write.
                bad.append(name)
    assert not bad, (
        f"the shell performs write calls {sorted(set(bad))}; it is REPORT-ONLY "
        f"by design — lane A's remedy mutates branch protection and must stay "
        f"a human action"
    )
