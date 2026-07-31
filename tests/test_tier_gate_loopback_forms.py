"""Loopback trust tuples accept all FOUR loopback forms.

'::ffff:127.0.0.1' is what a dual-stack ([::]) listener reports for an IPv4
loopback connect. The METERED gate (free_tier_gate._resolve_caller) learned
that form in #2018 — its absence 402'd /radar's own loopback calls and pinned
every grid field to baseline for 15 days. The teaser gate in the same class
(routes/tier_gate.py::caller_is_privileged — iso snapshot teaser via
gate_intl_snapshot, /api/v1/deals $-masking + 3-row cap) and the rate_limit
internal exemption next to it never learned it: a keyless loopback self-call
silently got ANONYMOUS-tier data (teaser rows, masked $) or lost its
rate-limit exemption depending on socket family.

These tests pin each tuple to EXACTLY the four forms: a missing form re-opens
the socket-family split; an extra entry is silent trust expansion. Loopback
trust stays defense-in-depth — self-calls still send X-Internal-Key per
#2018/#2025/#2039.

House rules: static AST extraction, no import of main.py or routes.tier_gate
(it imports flask at module scope), nothing executes at module scope here.
"""
import ast
import os

_HERE = os.path.dirname(__file__)
_TIER_GATE = os.path.join(_HERE, "..", "routes", "tier_gate.py")
_METERED_GATE = os.path.join(_HERE, "..", "free_tier_gate.py")
_CONTEXT_PACKS = os.path.join(_HERE, "..", "routes", "context_packs.py")

_LOOPBACK_FORMS = frozenset(
    {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})


def _tree(path: str) -> ast.Module:
    with open(os.path.abspath(path), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # Guard the guard: a degenerate parse would vacuously pass every search
    # below (2026-07-28 lesson — assert it parsed, never just filter).
    assert isinstance(tree, ast.Module) and len(tree.body) > 10, (
        f"{os.path.basename(path)} parsed to a degenerate module — "
        "extraction harness is not looking at the real file")
    return tree


def _remote_addr_membership_tuples(path: str, fn_name: str) -> list:
    """Every `request.remote_addr in (<literal>)` inside fn_name, nested
    functions included (rate_limit's check lives in deco/wrapper)."""
    fns = [n for n in ast.walk(_tree(path))
           if isinstance(n, ast.FunctionDef) and n.name == fn_name]
    assert fns, (f"{os.path.basename(path)} no longer defines {fn_name}() — "
                 "these loopback guards need updating, not deleting")
    found = []
    for fn in fns:
        for node in ast.walk(fn):
            if (isinstance(node, ast.Compare)
                    and len(node.ops) == 1
                    and isinstance(node.ops[0], ast.In)
                    and isinstance(node.left, ast.Attribute)
                    and node.left.attr == "remote_addr"
                    and isinstance(node.comparators[0],
                                   (ast.Tuple, ast.List, ast.Set))):
                found.append({e.value for e in node.comparators[0].elts
                              if isinstance(e, ast.Constant)})
    assert found, (f"{fn_name}() no longer compares request.remote_addr "
                   "against a literal tuple — if the loopback check moved, "
                   "point this test at its new home")
    return found


def test_caller_is_privileged_accepts_all_four_loopback_forms():
    for forms in _remote_addr_membership_tuples(
            _TIER_GATE, "caller_is_privileged"):
        assert forms == _LOOPBACK_FORMS, (
            f"caller_is_privileged trusts {sorted(forms)} — a dual-stack "
            "([::]) listener reports '::ffff:127.0.0.1' for IPv4 loopback "
            "connects, so a keyless self-call to a teaser-gated surface "
            "(iso snapshot, /api/v1/deals) gets anonymous-tier data "
            "depending on socket family (the METERED gate's #2018 bug, "
            "teaser twin)")


def test_rate_limit_exemption_accepts_all_four_loopback_forms():
    for forms in _remote_addr_membership_tuples(_TIER_GATE, "rate_limit"):
        assert forms == _LOOPBACK_FORMS, (
            f"rate_limit's internal exemption trusts {sorted(forms)} — "
            "without '::ffff:127.0.0.1' a dual-stack loopback self-probe "
            "loses the r58b exemption and the brain-radar 429s come back")


def test_context_packs_full_access_accepts_all_four_loopback_forms():
    for forms in _remote_addr_membership_tuples(
            _CONTEXT_PACKS, "_full_access"):
        assert forms == _LOOPBACK_FORMS, (
            f"context_packs._full_access trusts {sorted(forms)} — a "
            "dual-stack loopback self-call gets the truncated anonymous "
            "context pack depending on socket family (third member of the "
            "#2018/#2041 defect class)")


def test_metered_gate_accepts_all_four_loopback_forms():
    # The #2018 original. test_radar_freshness pins it by substring, which a
    # comment would satisfy — this pins the actual comparison tuple.
    for forms in _remote_addr_membership_tuples(
            _METERED_GATE, "_resolve_caller"):
        assert forms == _LOOPBACK_FORMS, (
            f"free_tier_gate._resolve_caller trusts {sorted(forms)} — "
            "regressing #2018 re-meters the server's own loopback calls "
            "and re-pins /radar to baseline")
