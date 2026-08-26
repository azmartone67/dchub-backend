"""Guards for `capacity_mw` being load-bearing on /api/site-score.

THE DEFECT (measured live 2026-08-25, Dallas 32.7767,-96.7970):

    analyze_site{lat,lon}                    -> composite_score 81.2
    analyze_site{lat,lon,capacity_mw:1}      -> composite_score 81.2
    analyze_site{lat,lon,capacity_mw:5000}   -> composite_score 81.2

Identical after subtracting per-call noise. The argument was DECLARED, reached
the handler, and was used exactly once — echoed as `capacity_requested_mw`, and
stripped entirely on the free tier — while the tool description told agents to
set it. These tests exist so it cannot go silently inert again.
"""
import ast
import pathlib

import pytest

from util.site_capacity import site_capacity_context as scc

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_GEN = 12_345.6          # nearby installed generation, MW


@pytest.mark.parametrize("nothing", [None, 0, 0.0, -1, -5000, "", "abc", [], {}])
def test_no_requested_load_yields_no_block(nothing):
    """ABSENT, not zero-filled. The block's presence is the proof the argument
    was received; a zero-filled block is indistinguishable from the silent drop
    this exists to end."""
    assert scc(nothing, _GEN) is None


def test_the_block_actually_moves_with_the_requested_load():
    """THE anti-inert guard. If two different loads produce the same block, the
    parameter is inert again and this whole change bought nothing."""
    a, b = scc(1, _GEN), scc(5000, _GEN)
    assert a is not None and b is not None
    assert a != b
    assert a["requested_mw"] != b["requested_mw"]
    assert a["requested_pct_of_nearby_generation"] != b["requested_pct_of_nearby_generation"]
    assert a["note"] != b["note"]


def test_a_small_load_keeps_its_precision():
    """1 MW against 12 GW is 0.008%. Rendering that as '0.0%' erases the signal
    the block exists to carry."""
    note = scc(1, _GEN)["note"]
    assert "0.0%" not in note
    assert "0.0081%" in note


def test_it_declares_that_it_does_not_move_the_composite():
    """The composite is published with a methodology_version and a citable free
    headline. This block must say it is not folded in, and where the load IS
    applied — otherwise it reads as a score input."""
    got = scc(500, _GEN)
    assert got["affects_overall_score"] is False
    assert got["why_not"].strip()
    assert "get_power_availability_timeline" in got["instead"]


def test_nameplate_is_never_presented_as_headroom():
    """An interconnection study grants headroom; nameplate within 80 km is not
    that, and a reader who conflates them over-claims a site."""
    basis = scc(500, _GEN)["basis"]
    assert "NOT AVAILABLE HEADROOM" in basis.upper()


def test_zero_nearby_generation_is_a_signal_not_a_ratio():
    """Never divide by zero, and never publish a fabricated 0%."""
    got = scc(500, 0)
    assert got["requested_pct_of_nearby_generation"] is None
    assert "NO installed generation" in got["note"]


@pytest.mark.parametrize("bad", [None, "n/a", object(), float("nan")])
def test_unresolvable_generation_reads_unmeasured_never_fabricated(bad):
    got = scc(500, bad)
    assert got is not None, "a requested load must still be acknowledged"
    assert got["requested_mw"] == 500
    assert got.get("requested_pct_of_nearby_generation") is None


def test_it_never_raises_on_hostile_input():
    for args in [(5000, _GEN, "x", "y"), ("5000", _GEN), (float("inf"), _GEN), (1, -5)]:
        scc(*args)


# ── wiring: the handler must actually call it ─────────────────────────────
def _site_score_fn_source() -> str:
    """Source of api_site_score ONLY.

    Scoped deliberately: a whole-file grep for 'capacity_context' would pass on
    a main.py where the call had been moved into a dead branch, or where only
    the comment survived. A silently-empty extraction would pass everything, so
    the body is asserted non-empty first."""
    src = (_ROOT / "main.py").read_text()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "api_site_score"), None)
    assert fn is not None and fn.body, "api_site_score parsed with an EMPTY body"
    seg = ast.get_source_segment(src, fn)
    assert seg and len(seg) > 2000, "extraction collapsed — the guard would be vacuous"
    return seg


def test_the_handler_passes_the_requested_capacity_in():
    """Asserted on the CALL EXPRESSION: first positional arg must be `capacity`,
    the variable the request's capacity_mw was parsed into. Passing anything
    else would produce a well-formed block about the wrong number."""
    tree = ast.parse(_site_score_fn_source())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in ("_scc", "site_capacity_context")]
    assert len(calls) == 1, f"expected exactly 1 capacity-context call, found {len(calls)}"
    first = calls[0].args[0]
    assert isinstance(first, ast.Name) and first.id == "capacity", (
        "the requested load must be what gets framed")


def test_the_response_key_is_conditional_not_always_present():
    """`capacity_context` must be absent when no load was requested. An
    unconditional key would republish the zero-filled shape the None return
    exists to prevent."""
    seg = _site_score_fn_source()
    tree = ast.parse(seg)
    ifexps = [n for n in ast.walk(tree) if isinstance(n, ast.IfExp)]
    guarded = [n for n in ifexps
               if "capacity_context" in (ast.get_source_segment(seg, n) or "")]
    assert guarded, "capacity_context is emitted unconditionally — it must be gated on a real load"
