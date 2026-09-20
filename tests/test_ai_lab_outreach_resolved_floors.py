"""
routes/ai_lab_outreach emails partnerships@ at NVIDIA, Google DeepMind,
Perplexity, Mistral, Groq, CoreWeave, Lambda, TensorWave and Core42 on a daily
autopilot. Two functions in it read canon, and until 2026-09-19 both read
ai_surface_canon.PINNED['public'] verbatim:

  _canon_public()  writes the figures INTO the draft body
  _claim_gate()    refuses to send any figure ABOVE canon

★ THEY MUST MOVE TOGETHER. Migrate only the copy and every draft is built
above the gate's pinned ceiling -> status='blocked_claims' -> the lane goes
dark refusing its own correct numbers. Migrate only the gate and the copy
still publishes the cold-start literal. The first test below is the one that
fails in BOTH of those worlds.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

import ai_surface_canon as asc
import routes.ai_lab_outreach as alo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_COPY_KEYS = ("facilities", "deals", "markets")


def _as_int(phrase) -> int:
    digits = re.sub(r"[^\d]", "", str(phrase or ""))
    return int(digits) if digits else 0


@pytest.fixture
def risen(monkeypatch):
    """Floors strictly ABOVE the pin — the only direction the overlay allows."""
    pin = dict(asc.PINNED.get("public") or {})
    vals = dict(pin)
    for key in _COPY_KEYS:
        base = _as_int(pin.get(key))
        assert base > 0, f"pin {key}={pin.get(key)!r} has no digits — canon shape changed"
        vals[key] = f"{base + 10_000:,}+"
        assert vals[key] != pin.get(key), (
            f"guard-the-guard: risen {key} equals the pin — every assertion "
            f"below would pass on unmigrated code")
    monkeypatch.setattr(asc, "resolve_public_floors_cached",
                        lambda: dict(vals), raising=True)
    return vals


# ── the deadlock guard ──────────────────────────────────────────────────────

def test_the_gate_passes_the_copy_this_lane_itself_writes(risen):
    """★ THE LOAD-BEARING TEST. Real draft, real gate, one resolver.

    Fails if only _canon_public() migrated  -> the gate over-claims on its own
    copy and every draft is blocked.
    Fails if only _claim_gate() migrated    -> the body still carries the pin.
    """
    _subject, body = alo._draft_pitch(alo._TARGETS[0])
    assert risen["facilities"] in body, (
        f"the draft body does not carry the resolved floor "
        f"{risen['facilities']!r} — _canon_public() is still reading PINNED")
    overs = [v for v in alo._claim_gate(body) if v.get("kind") == "over_claim"]
    assert overs == [], (
        f"the claim gate convicts the lane's OWN copy: {overs}. The body is "
        f"built from the resolved floor and the gate is checking it against a "
        f"pinned ceiling — every draft would be marked 'blocked_claims'.")


def test_both_readers_resolve_through_the_same_door():
    """AST, not grep: both docstrings name the other function, so a substring
    check would pass on prose alone."""
    src = open(os.path.join(ROOT, "routes", "ai_lab_outreach.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    want = {"_canon_public", "_claim_gate"}
    seen: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in want:
            seen[node.name] = any(
                (n.func.attr if isinstance(n.func, ast.Attribute)
                 else getattr(n.func, "id", "")) == "resolve_public_floors_cached"
                for n in ast.walk(node) if isinstance(n, ast.Call))
    assert set(seen) == want, f"functions missing or renamed: {sorted(seen)}"
    assert all(seen.values()), (
        f"these no longer resolve: {[k for k, v in seen.items() if not v]} — "
        f"the copy and the gate that checks it must read the same floors")


# ── the copy ────────────────────────────────────────────────────────────────

def test_canon_public_publishes_the_resolved_floor(risen):
    pub = alo._canon_public()
    for key in _COPY_KEYS:
        assert pub[key] == risen[key], (
            f"_canon_public()[{key!r}]={pub[key]!r} is not the resolved floor")
        assert pub[key] != asc.PINNED["public"][key]


def test_an_empty_resolver_leaves_the_pin(monkeypatch):
    monkeypatch.setattr(asc, "resolve_public_floors_cached", lambda: {}, raising=True)
    pub = alo._canon_public()
    for key in _COPY_KEYS:
        assert pub[key] == asc.PINNED["public"][key]


def test_a_raising_resolver_leaves_the_pin(monkeypatch):
    def _boom():
        raise RuntimeError("resolver down")
    monkeypatch.setattr(asc, "resolve_public_floors_cached", _boom, raising=True)
    pub = alo._canon_public()
    for key in _COPY_KEYS:
        assert pub[key] == asc.PINNED["public"][key]


# ── the gate is not loosened ────────────────────────────────────────────────

def test_a_figure_above_the_resolved_floor_is_still_blocked(risen):
    """The ceiling moved; it did not disappear."""
    over = _as_int(risen["facilities"]) + 50_000
    body = f"DC Hub tracks {over:,}+ global facilities."
    overs = [v for v in alo._claim_gate(body) if v.get("kind") == "over_claim"]
    assert len(overs) == 1, f"a real over-claim stopped firing: {overs}"
    assert overs[0]["noun"] == "facilities"


def test_a_dead_resolver_keeps_the_STRICTER_pinned_ceiling(monkeypatch):
    """Fail-closed direction: no resolver means the LOWER pin, not silence."""
    monkeypatch.setattr(asc, "resolve_public_floors_cached", lambda: {}, raising=True)
    over = _as_int(asc.PINNED["public"]["facilities"]) + 10_000
    body = f"DC Hub tracks {over:,}+ global facilities."
    overs = [v for v in alo._claim_gate(body) if v.get("kind") == "over_claim"]
    assert len(overs) == 1, (
        "with no live overlay the gate must fall back to the PINNED ceiling "
        f"and still convict {over:,}+ — got {overs}")


def test_unreadable_canon_still_refuses_the_send(monkeypatch):
    """★ FAILS CLOSED. Unverifiable is not permission."""
    import builtins
    real_import = builtins.__import__

    def _no_canon(name, *a, **k):
        if name == "ai_surface_canon":
            raise ImportError("canon unreadable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_canon)
    out = alo._claim_gate("DC Hub tracks 999,999+ global facilities.")
    monkeypatch.undo()
    assert out and out[0]["kind"] == "unverifiable", out


def test_no_canon_VALUE_is_computed_at_import_time():
    """★ THE IMPORT-TIME FREEZE. /.well-known/agent-card.json served 21,500+
    for weeks because AGENT_CARD ran canon_text() at import — frozen at boot
    and looking live.

    The hazard is binding a canon VALUE, not importing a canon NAME: a
    module-level `from ai_surface_canon import canon_text` is a function
    reference resolved per call and is safe."""
    src = open(os.path.join(ROOT, "routes", "ai_lab_outreach.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    nested: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for child in ast.walk(node):
                nested.add(id(child))
    canon_calls = {
        "canon_text", "canon_nums", "resolve_canon", "resolve_public_floors",
        "resolve_public_floors_cached", "_canon_public",
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
        "a canon value is frozen at import: " + "; ".join(problems))
