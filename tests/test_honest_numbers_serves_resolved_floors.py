"""
routes/mcp_honest_numbers is the bridge every registry description and the
white-glove propagation job read. Until 2026-09-19 its as_dict() returned
ai_surface_canon.PINNED['public'] VERBATIM, so the module whose name promises
honest numbers published the cold-start literal no matter how well
resolve_public_floors_cached() healed. #4859 taught the sibling lesson: moving
a LABEL without moving the number is worse than staying pinned. These tests pin
the number.

Two doors, opposite consumers:
  as_dict()        publishers  -> resolved floors (overlay applied)
  as_dict_pinned() auditors    -> the hand-typed pin, unresolved

The second is not a convenience. brain_consistency_radar convicts
PINNED['public'] of standing above live reality and its finding tells a human
to lower that literal; the overlay only ever RAISES, so feeding it a resolved
floor would convict an innocent pin of an over-claim the overlay introduced.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

import ai_surface_canon as asc
import routes.mcp_honest_numbers as hn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FLOOR_KEYS = ("facilities", "deals", "markets", "countries")


def _as_int(phrase) -> int:
    digits = re.sub(r"[^\d]", "", str(phrase or ""))
    return int(digits) if digits else 0


def _risen() -> dict:
    """PINNED['public'] with every floor key raised well above its pin.

    RAISED, because the overlay rejects anything below the pin — a fixture
    that tried to lower a floor would be rejected by the code under test and
    prove nothing."""
    pin = dict(asc.PINNED.get("public") or {})
    out = dict(pin)
    for key in _FLOOR_KEYS:
        base = _as_int(pin.get(key))
        assert base > 0, f"pin {key}={pin.get(key)!r} has no digits — canon shape changed"
        out[key] = f"{base + 10_000:,}+"
    return out


@pytest.fixture
def risen(monkeypatch):
    """Stub the cached resolver with floors strictly above the pin."""
    vals = _risen()
    for key in _FLOOR_KEYS:
        assert vals[key] != (asc.PINNED["public"].get(key)), (
            f"guard-the-guard: risen {key} equals the pin — every assertion "
            f"below would pass on unmigrated code")
    monkeypatch.setattr(asc, "resolve_public_floors_cached",
                        lambda: dict(vals), raising=True)
    return vals


# ── the migration itself ────────────────────────────────────────────────────

def test_as_dict_publishes_the_resolved_floor_not_the_pin(risen):
    """THE BUG. as_dict() returning PINNED verbatim is what this fixes."""
    out = hn.as_dict()
    for key in _FLOOR_KEYS:
        assert out[key] == _as_int(risen[key]), (
            f"as_dict()[{key!r}]={out[key]} is not the resolved floor "
            f"{_as_int(risen[key])} — the bridge is reading PINNED again")
        assert out[key] != _as_int(asc.PINNED["public"][key])


def test_the_phrases_carry_the_same_floor_as_the_ints(risen):
    """One number, two painters. A phrase built off the pin beside an int
    built off the resolver publishes two different counts in one listing."""
    out = hn.as_dict()
    assert out["deals_phrase"] == f"{risen['deals']} tracked deals"
    assert out["countries_phrase"] == f"{risen['countries']} countries"
    assert str(out["deals"]) in re.sub(r"[^\d]", "", out["deals_phrase"])


def test_the_publisher_actually_receives_it(risen):
    """WIRED, not merely present. The crawler's _canonical_numbers() is the
    call site that reaches every auto-submitted registry description; a bridge
    that resolves but is read by nobody publishes nothing."""
    from routes.mcp_presence_crawler import _canonical_numbers
    assert _canonical_numbers()["facilities"] == _as_int(risen["facilities"])


# ── the auditor's door stays pinned ─────────────────────────────────────────

def test_as_dict_pinned_ignores_the_overlay(risen):
    pinned = hn.as_dict_pinned()
    for key in _FLOOR_KEYS:
        assert pinned[key] == _as_int(asc.PINNED["public"][key]), (
            f"as_dict_pinned()[{key!r}] followed the overlay — the floor fence "
            f"would convict PINNED of a number PINNED does not hold")


def test_the_floor_fence_reads_the_pinned_door():
    """AST, not grep: the comment above the import names both functions, so a
    substring check would pass on the wrong one."""
    src = open(os.path.join(ROOT, "routes", "brain_consistency_radar.py"),
               encoding="utf-8").read()
    names: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module == "routes.mcp_honest_numbers":
            names += [a.name for a in node.names]
    assert names, "brain_consistency_radar no longer imports the bridge at all"
    assert "as_dict" not in names, (
        "brain_consistency_radar imports as_dict() — that is the RESOLVED "
        "floor, and this detector's finding names PINNED['public'] and tells a "
        "human to lower it. Use as_dict_pinned().")
    assert "as_dict_pinned" in names


# ── fail-soft, and no import-time freeze ────────────────────────────────────

def test_an_empty_resolver_falls_back_to_the_pin(monkeypatch):
    monkeypatch.setattr(asc, "resolve_public_floors_cached", lambda: {}, raising=True)
    assert hn.as_dict() == hn.as_dict_pinned()


def test_a_raising_resolver_falls_back_to_the_pin(monkeypatch):
    def _boom():
        raise RuntimeError("resolver down")
    monkeypatch.setattr(asc, "resolve_public_floors_cached", _boom, raising=True)
    assert hn.as_dict() == hn.as_dict_pinned()


def test_resolver_metadata_never_reaches_copy(monkeypatch):
    """resolve_public_floors_cached() returns _source / _rejected / _cold
    alongside the floors. A bridge that copied the dict would paste them into
    a registry description."""
    vals = _risen()
    vals["_source"] = {k: "live" for k in _FLOOR_KEYS}
    vals["_rejected"] = ["facilities=400<20700"]
    vals["_cold"] = True
    monkeypatch.setattr(asc, "resolve_public_floors_cached",
                        lambda: dict(vals), raising=True)
    out = hn.as_dict()
    assert not [k for k in out if str(k).startswith("_")], out


def test_the_bridge_reads_canon_only_inside_functions():
    """★ THE IMPORT-TIME FREEZE. /.well-known/agent-card.json served 21,500+
    for weeks because AGENT_CARD ran canon_text() at import: the value looked
    live and was frozen at boot. A module-level resolve here would be strictly
    worse than the literal it replaced."""
    src = open(os.path.join(ROOT, "routes", "mcp_honest_numbers.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    nested: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                nested.add(id(child))
    offenders = [
        n.module for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and (n.module or "").startswith("ai_surface_canon")
        and id(n) not in nested
    ]
    assert not offenders, (
        f"mcp_honest_numbers imports canon at MODULE level ({offenders}) — "
        f"that freezes the cold-start floor at boot and labels it live")
