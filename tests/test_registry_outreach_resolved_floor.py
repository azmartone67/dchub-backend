"""
routes/mcp_registry_outreach._canonical_short_desc() is the `description` field
POSTed into third-party MCP registry listings (_submit_target, the payload at
the network POST). Until 2026-09-19 it read ai_surface_canon.PINNED['public']
verbatim, so every listing we submitted carried the cold-start literal no
matter how well resolve_public_floors_cached() healed.

A registry listing is re-crawled on the REGISTRY's schedule, not ours: a stale
number pasted into one sits where no drift detector of ours can reach it. That
is why this surface in particular has to publish the resolved floor.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

import ai_surface_canon as asc
import routes.mcp_registry_outreach as mro

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _as_int(phrase) -> int:
    digits = re.sub(r"[^\d]", "", str(phrase or ""))
    return int(digits) if digits else 0


@pytest.fixture
def risen(monkeypatch):
    """Floors strictly ABOVE the pin — the only direction the overlay allows.

    A fixture that lowered a floor would be rejected by resolve_public_floors()
    itself and would prove nothing about this module."""
    pin = dict(asc.PINNED.get("public") or {})
    base = _as_int(pin.get("facilities"))
    assert base > 0, f"pin facilities={pin.get('facilities')!r} — canon shape changed"
    vals = dict(pin)
    vals["facilities"] = f"{base + 10_000:,}+"
    assert vals["facilities"] != pin["facilities"], (
        "guard-the-guard: risen floor equals the pin, every assertion below "
        "would pass on unmigrated code")
    monkeypatch.setattr(asc, "resolve_public_floors_cached",
                        lambda: dict(vals), raising=True)
    return vals


def test_the_listing_description_carries_the_resolved_floor(risen):
    """THE BUG: the submitted description said the pin."""
    desc = mro._canonical_short_desc()
    assert risen["facilities"] in desc, (
        f"description {desc!r} does not carry the resolved floor "
        f"{risen['facilities']!r} — still reading PINNED")
    assert asc.PINNED["public"]["facilities"] not in desc


def test_the_tool_count_stays_pinned(risen):
    """tools_advertised is NOT a floor: no spec, no query, no resolver. It is
    hand-walked by design and must not be inferred from the floors overlay."""
    desc = mro._canonical_short_desc()
    assert f"{asc.PINNED['tools_advertised']} tools" in desc


def test_an_empty_resolver_falls_back_to_the_pin(monkeypatch):
    monkeypatch.setattr(asc, "resolve_public_floors_cached", lambda: {}, raising=True)
    assert asc.PINNED["public"]["facilities"] in mro._canonical_short_desc()


def test_a_raising_resolver_falls_back_to_the_pin(monkeypatch):
    def _boom():
        raise RuntimeError("resolver down")
    monkeypatch.setattr(asc, "resolve_public_floors_cached", _boom, raising=True)
    assert asc.PINNED["public"]["facilities"] in mro._canonical_short_desc()


def test_resolver_metadata_never_reaches_the_listing(monkeypatch):
    """resolve_public_floors_cached() returns _source / _rejected / _cold
    beside the floors. They must not be pasted into a registry description."""
    vals = dict(asc.PINNED.get("public") or {})
    vals["_source"] = {"facilities": "live"}
    vals["_rejected"] = ["facilities=400<20700"]
    vals["_cold"] = True
    monkeypatch.setattr(asc, "resolve_public_floors_cached",
                        lambda: dict(vals), raising=True)
    desc = mro._canonical_short_desc()
    for leak in ("_source", "_rejected", "_cold", "live", "pinned"):
        assert leak not in desc, f"{leak!r} leaked into {desc!r}"


def test_the_submitter_payload_is_what_calls_it():
    """WIRED, not merely present: a resolved description read by nobody
    publishes nothing. The call must sit inside the function that POSTs."""
    src = open(os.path.join(ROOT, "routes", "mcp_registry_outreach.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_submit_target"), None)
    assert fn is not None, "_submit_target is gone — re-point this guard"
    called = {
        n.func.id for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_canonical_short_desc" in called, (
        "_submit_target no longer builds its description from "
        "_canonical_short_desc() — the resolved floor reaches no listing")


def test_no_canon_VALUE_is_computed_at_import_time():
    """★ THE IMPORT-TIME FREEZE. /.well-known/agent-card.json served 21,500+
    for weeks because AGENT_CARD ran canon_text() at import: the value was
    frozen at boot and looked live. A module-level resolve here would be
    strictly worse than the literal it replaced.

    ★ The hazard is binding a canon VALUE, not importing a canon NAME.
    `from ai_surface_canon import canon_text` at line 45 is a function
    reference resolved per call and is SAFE; this module has had it for
    months. What must never happen at module level is CALLING one of them, or
    subscripting PINNED into a constant."""
    src = open(os.path.join(ROOT, "routes", "mcp_registry_outreach.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    nested: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for child in ast.walk(node):
                nested.add(id(child))
    canon_calls = {
        "canon_text", "canon_nums", "resolve_canon", "resolve_public_floors",
        "resolve_public_floors_cached", "as_dict", "as_dict_pinned",
    }
    problems: list[str] = []
    for n in ast.walk(tree):
        if id(n) in nested:
            continue
        if isinstance(n, ast.Call):
            name = (n.func.id if isinstance(n.func, ast.Name)
                    else getattr(n.func, "attr", ""))
            if name in canon_calls:
                problems.append(f"line {n.lineno}: calls {name}() at module level")
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                and n.value.id == "PINNED":
            problems.append(f"line {n.lineno}: subscripts PINNED at module level")
    assert not problems, (
        "a canon value is frozen at import: " + "; ".join(problems) +
        " — resolve per submission instead, or the listing publishes the "
        "cold-start floor while claiming to be live")
