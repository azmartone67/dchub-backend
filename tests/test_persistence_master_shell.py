"""Persistence Master Shell #41 (2026-07-29) — lane guards.

The shell's whole claim is that the shortlist chain stores nothing for real
agents because the ROOT write lands in a shared bucket and the arg contract is
unsatisfiable. These tests guard the two properties that must not silently
regress, and they guard them the way this repo has repeatedly learned to:

  * derived from the shipped source, never transcribed — the recurring defect
    here is a test that agrees with its own copy of a contract;
  * INDETERMINATE is not a PASS — a lane that cannot read its evidence must not
    report green, because a check that ran against nothing looks identical to a
    check that passed.

Run:  python3 -m pytest tests/test_persistence_master_shell.py -v
"""
from __future__ import annotations

import inspect

import routes.shortlists as sl
from routes.persistence_master_shell import (
    CHAIN, CHAIN_ROOT, CHAIN_DEPENDENTS, SHARED_OWNER, LANES,
    _check, _lane_verdict,
)


# ── lane 1: the actuator ─────────────────────────────────────────────
def test_owner_never_returns_the_shared_bucket():
    """THE BUG. A keyless caller must not receive a shared owner string."""
    src = inspect.getsource(sl._owner)
    assert f'return "{SHARED_OWNER}"' not in src, (
        "_owner() still hands keyless callers the shared bucket — every anonymous "
        "shortlist is addressable across tenants by name")
    assert "OWNER_REQUIRED" in src, "_owner() lost its keyless sentinel"


def test_every_owner_call_site_handles_the_keyless_case():
    """The trap in this fix: _owner() now returns None, so any call site that
    forgets to check writes owner=NULL — the same cross-tenant bug wearing a
    different value, and harder to spot because NULL looks like an accident
    rather than a policy. Counted from source so a new endpoint cannot be added
    without a guard."""
    src = inspect.getsource(sl)
    calls = src.count("owner = _owner()")
    guards = src.count("if owner is OWNER_REQUIRED")
    assert calls > 0, "no _owner() call sites found — test is blind"
    assert guards == calls, (
        f"{calls} call sites but only {guards} keyless guards — an unguarded site "
        f"writes owner=NULL for anonymous callers")


def test_keyless_refusal_names_the_tool_that_fixes_it():
    """A refusal that doesn't say how to proceed just loses the caller. The
    conversion path IS the fix, so it has to be in the response."""
    src = inspect.getsource(sl._owner_required_response)
    assert "claim_free_key" in src, "refusal doesn't name the one call that resolves it"
    assert "401" in src, "refusal must be an explicit auth failure, not a silent no-op"


def test_session_scoping_was_not_used_as_the_fix():
    """Guards against the plausible wrong remedy. X-MCP-Session is forwarded by
    the gateway and would make an isolation test pass, but session ids rotate per
    connection — so it converts a cross-tenant bug into a shortlist that silently
    vanishes before the next conversation, defeating the subsystem's purpose."""
    src = inspect.getsource(sl)
    owner_region = src[src.index("def _owner("):src.index("def _owner_required_response")]
    assert "X-MCP-Session" not in owner_region, (
        "_owner() scopes on the rotating session id — shortlists would not survive "
        "to the next conversation, which is the entire feature")


# ── lane wiring ──────────────────────────────────────────────────────
def test_chain_root_gates_its_dependents():
    """The dependents read state the root creates. If the root is ever dropped
    from the chain the shell would report four 'independently unpopular' tools
    and hide the single cause."""
    assert CHAIN_ROOT == "save_to_shortlist"
    assert CHAIN_ROOT in CHAIN
    assert set(CHAIN_DEPENDENTS).issubset(set(CHAIN))
    assert CHAIN_ROOT not in CHAIN_DEPENDENTS, "the root must not be its own dependent"


def test_indeterminate_never_reports_as_pass():
    """A lane that could not read its evidence must not be green — the exact
    failure mode that let a stale listing and a no-op submitter both report clean
    earlier this week."""
    assert _lane_verdict([_check("x", "n", None, "unreadable")]) == "INDETERMINATE"
    assert _lane_verdict([]) == "INDETERMINATE"
    # An INDETERMINATE alongside a PASS must still sink the lane.
    assert _lane_verdict([_check("a", "n", True, "ok"),
                          _check("b", "n", None, "unreadable")]) == "INDETERMINATE"
    # A critical FAIL outranks a non-critical one.
    assert _lane_verdict([_check("a", "n", False, "x", critical=True)]) == "FAILED"
    assert _lane_verdict([_check("a", "n", False, "x")]) == "DEGRADED"
    assert _lane_verdict([_check("a", "n", True, "x")]) == "PASSED"


def test_all_three_lanes_are_registered():
    keys = [k for k, _, _ in LANES]
    assert keys == ["ownership", "entry_barrier", "reachability"], (
        f"lane set drifted: {keys} — the shell covers items 1-3 in dependency order")
    for _, _, fn in LANES:
        assert callable(fn)
