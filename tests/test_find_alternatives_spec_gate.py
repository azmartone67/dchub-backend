"""find_alternatives must not hand operator + capacity to unpaid callers.

Source-level, no DB and no network: the route's leak channels are decided by
`_specs_visible(tier)` and by the tier the request resolves to, and both are
checkable without standing up Postgres. What this file exists to stop:

  1. the row fields coming back for an anonymous caller;
  2. `target_facility` keeping them (the corpus, one caller-named row at a time);
  3. match_reasons / key_differences republishing them in PROSE;
  4. ★ the gate resolving through X-Internal-Key, which dchub-mcp-server sends
     on EVERY call — a gate that reads the stock resolver masks REST and leaves
     POST /mcp wide open, and every REST-only test still passes.
"""
import ast
import io
import os
import pathlib

import pytest

SRC_PATH = pathlib.Path(__file__).resolve().parents[1] / "routes" / "mcp_tier1_tools.py"
SRC = io.open(SRC_PATH, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _func(name):
    for n in ast.walk(TREE):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name}() not found in {SRC_PATH.name}")


# ── 1. the gate exists and is not vacuous ───────────────────────────────────

def _load_gate():
    """Exec the REAL gate source — the constants and the function, verbatim.

    Not a re-typed copy: a mirror of the rule would agree with itself while the
    shipped rule drifted.
    """
    wanted = ("_SPECS_MIN_TIER", "_SPEC_TIER_RANK")
    parts = []
    for n in TREE.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in wanted for t in n.targets):
            parts.append(ast.get_source_segment(SRC, n))
        if isinstance(n, ast.FunctionDef) and n.name == "_specs_visible":
            parts.append(ast.get_source_segment(SRC, n))
    assert len(parts) == 3, f"expected 2 constants + 1 function, got {len(parts)}"
    ns = {"os": os}
    exec(compile("\n".join(parts), "<gate>", "exec"), ns)
    return ns["_specs_visible"]


def test_specs_visible_actually_discriminates():
    """A gate that answers the same for everyone is not a gate."""
    vis = _load_gate()
    assert vis("anonymous") is False
    assert vis("free") is False
    assert vis("identified") is False
    assert vis("developer") is True
    assert vis("pro") is True
    assert vis("enterprise") is True
    # An unknown plan string must not fall through to visible.
    assert vis("some-plan-nobody-has-heard-of") is False
    assert vis(None) is False


# ── 2. every leak channel is behind the gate ────────────────────────────────

def _specs_guard_ancestry(fn):
    """Map every AST node in `fn` -> True if some ancestor gates it on _specs.

    Built by walking DOWN from the function with the guard state carried along,
    so the answer is "is THIS occurrence protected", not "is SOME occurrence
    somewhere protected". An earlier version of this file asked the second
    question and passed while the row fields were fully ungated — mutation A
    and B both survived it.
    """
    guarded = {}

    def walk(node, under):
        guarded[id(node)] = under
        for child in ast.iter_child_nodes(node):
            child_under = under
            if isinstance(node, ast.If) and "_specs" in ast.dump(node.test):
                child_under = under or (child in node.body)
            if isinstance(node, ast.IfExp) and "_specs" in ast.dump(node.test):
                child_under = under or (child is node.body)
            walk(child, child_under)

    walk(fn, False)
    return guarded


def _every_occurrence_guarded(fn, predicate):
    """(all_guarded, n_seen) over nodes matching `predicate`."""
    guarded = _specs_guard_ancestry(fn)
    hits = [n for n in ast.walk(fn) if predicate(n)]
    return all(guarded.get(id(n), False) for n in hits), len(hits)


@pytest.mark.parametrize("field", ["provider", "power_mw"])
def test_every_spec_field_emission_is_gated(field):
    """EVERY place the field reaches a response dict sits under the gate.

    Covers the row AND target_facility: both were separate leaks and a test
    that accepted "one of them is guarded" catches neither.
    """
    fn = _func("find_alternatives")

    # Scope to the two RESPONSE dicts. `target["provider"]` is a read off the
    # DB row and the SQL SELECT names both columns — neither reaches a caller,
    # and counting them made this test fail on correct code.
    RESPONSE_DICTS = {"_row", "_target_block"}

    def is_response_emission(n):
        return (isinstance(n, ast.Subscript)
                and isinstance(n.value, ast.Name)
                and n.value.id in RESPONSE_DICTS
                and isinstance(n.slice, ast.Constant)
                and n.slice.value == field)

    ok, seen = _every_occurrence_guarded(fn, is_response_emission)
    assert seen >= 2, (
        f"expected at least 2 emissions of {field} (row + target_facility), "
        f"saw {seen} — the scan rotted or the shape changed")
    assert ok, (
        f"at least one emission of {field} is NOT behind the _specs gate — "
        "an unpaid caller receives it")


def test_derived_strings_do_not_republish_the_specs():
    """match_reasons / key_differences must not leak the specs in prose.

    Masking the fields while emitting "different operator (Amazon Web
    Services)" or "larger (+1000 MW)" suppresses the value where it is READ and
    keeps publishing it where it is SCRAPED. The MW *delta* counts too: the
    caller chose the target and can look its capacity up.
    """
    fn = _func("find_alternatives")
    guarded = _specs_guard_ancestry(fn)

    # An f-string that interpolates provider or a MW figure is a leak unless
    # it is on the guarded side of a _specs branch.
    # Scope to the strings that actually SHIP inside match_reasons / diffs.
    # The SQL SELECT is an f-string naming `provider` and `power_mw` as columns
    # and is not a response — counting it failed this test on correct code.
    shipped_strings = []
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "append"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id in ("match_reasons", "diffs")):
            shipped_strings.extend(n.args)
    assert shipped_strings, (
        "found no match_reasons/diffs appends — the scan rotted and this test "
        "would pass on anything")

    leaks = []
    for arg in shipped_strings:
        for n in ast.walk(arg):
            if not isinstance(n, ast.JoinedStr):
                continue
            dumped = ast.dump(n)
            interpolates_spec = any(t in dumped for t in
                                    ("provider", "cand_mw", "target_mw", "mw_diff"))
            if interpolates_spec and not guarded.get(id(n), False):
                flat = "".join(v.value for v in n.values
                               if isinstance(v, ast.Constant) and isinstance(v.value, str))
                leaks.append(flat.strip() or ast.dump(n)[:60])

    assert not leaks, (
        "these f-strings republish a gated spec outside the _specs gate: "
        f"{leaks}")


# ── 3. ★ the transport must not grant the tier ──────────────────────────────

def test_gate_does_not_trust_the_internal_key():
    """_end_user_tier must neutralise X-Internal-Key.

    map_tier_gating._detect_caller_tier maps that header to 'pro' at step 1,
    and dchub-mcp-server's callAPI sends it on every call. If this route read
    the stock resolver, POST /mcp would be ungated while REST looked fixed.
    """
    fn = _func("_end_user_tier")
    src = ast.get_source_segment(SRC, fn)
    assert "HTTP_X_INTERNAL_KEY" in src, (
        "_end_user_tier does not neutralise the internal key — every MCP "
        "caller, including anonymous ones, would resolve to 'pro'")
    assert ".pop(" in src, "the internal key must be REMOVED, not merely inspected"
    # and it must be restored, or the rest of the request loses its provenance
    assert "finally" in src and "HTTP_X_INTERNAL_KEY\"] = _stashed" in src.replace("'", '"'), (
        "the internal key must be restored in a finally: block")
    # fail closed
    assert "return \"anonymous\"" in src or "return 'anonymous'" in src, (
        "_end_user_tier must fail CLOSED to anonymous on error")


def test_gate_uses_the_data_gate_resolver_not_the_failopen_one():
    """detect_tier_failopen treats `X-API-Key: x` as paid. Never use it here."""
    fn_src = ast.get_source_segment(SRC, _func("_end_user_tier"))
    assert "detect_tier_for_data_gate" in fn_src
    assert "detect_tier_failopen" not in fn_src, (
        "the credential-PRESENCE fail-open resolver serves the full paid view "
        "to anyone who invents a header — see reference 2026-08-31")


# ── 4. the caller is told what is missing ───────────────────────────────────

def test_gated_response_discloses_and_does_not_advertise_a_bypass():
    """The gated payload must SAY it is gated, and must not hint at a way out."""
    fn = _func("find_alternatives")

    # ★ Read the assignment TARGETS, not the source text. Checking
    # `"_gated" in source` passed while the assignment was deleted, because the
    # token also appears in this route's docstring — a vacuous substring
    # assertion of exactly the kind this file exists to prevent.
    assigned_keys = {
        n.targets[0].slice.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and isinstance(n.targets[0], ast.Subscript)
        and isinstance(n.targets[0].slice, ast.Constant)
        and isinstance(n.targets[0].slice.value, str)
    }
    for required in ("_gated", "_withheld_fields", "_upgrade_cta"):
        assert required in assigned_keys, (
            f"the gated response never assigns {required!r} — a silently "
            "thinner payload is indistinguishable from an empty database. "
            f"assigned: {sorted(assigned_keys)}")

    # DOCSTRINGS ARE NOT SHIPPED. A docstring is an ast.Constant like any other
    # string, so a naive walk pulls in this route's own explanation of the
    # bypass it closes and the test fails on its own prose.
    docstrings = {
        id(n.body[0].value) for n in ast.walk(fn)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
        and n.body and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
        and isinstance(n.body[0].value.value, str)
    }
    shipped = " ".join(
        n.value.lower() for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and id(n) not in docstrings
    )
    assert shipped, "scanned zero shipped strings — the scan rotted"
    # #2096 had to strip a note that told gated callers how to get around it.
    for bypass_hint in ("pass bbox", "x-internal-key", "post /mcp",
                        "raise the limit", "internal key"):
        assert bypass_hint not in shipped, (
            f"a shipped string hints at a bypass ({bypass_hint!r})")


# ── 5. behavioural matrix: who actually gets the specs ──────────────────────

def test_tier_resolution_matrix():
    """The five callers that matter, resolved through the REAL _end_user_tier.

    Source assertions above prove the gate is WIRED; this proves it ANSWERS
    correctly — in particular row 3, the anonymous agent arriving through
    dchub-mcp-server, which is the case a gate built on the stock resolver gets
    wrong while every direct-REST test still passes.
    """
    import importlib.util
    import sys

    import flask

    os.environ.setdefault("INTERNAL_API_KEY", "test-internal-secret-abc123")
    INTERNAL = os.environ["INTERNAL_API_KEY"]

    spec = importlib.util.spec_from_file_location("_t1_gate", SRC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_t1_gate"] = mod
    spec.loader.exec_module(mod)

    import map_tier_gating as mtg

    def fake_data_gate(decode_jwt_func=None, req=None):
        """Stands in for the DB lookup, and REPRODUCES the step-1 shortcut.

        Faithful on the one behaviour under test: a valid X-Internal-Key
        resolves to 'pro' before any user credential is read. If
        _end_user_tier stops hiding that header, this stub returns 'pro' and
        the anonymous-MCP row below goes red — which is the regression.
        """
        from flask import request
        if request.headers.get("X-Internal-Key"):
            return "pro", {"source": "internal"}
        if request.headers.get("X-API-Key") == "dchub_real_paid":
            return "developer", {}
        return "anonymous", {}

    original = mtg.detect_tier_for_data_gate
    mtg.detect_tier_for_data_gate = fake_data_gate
    try:
        app = flask.Flask(__name__)

        def specs_for(headers):
            with app.test_request_context("/api/v1/mcp/tools/find_alternatives",
                                          headers=headers):
                return mod._specs_visible(mod._end_user_tier())

        assert specs_for({}) is False, "anonymous REST caller got the specs"
        assert specs_for({"X-API-Key": "x"}) is False, (
            "a one-character key that no account has ever held got the specs — "
            "the credential-PRESENCE fail-open is back")
        assert specs_for({"X-Internal-Key": INTERNAL}) is False, (
            "★ an ANONYMOUS agent arriving through dchub-mcp-server got the "
            "specs. The MCP server sends X-Internal-Key on every call and the "
            "stock resolver maps it to 'pro' at step 1 — POST /mcp is ungated "
            "while direct REST looks fixed.")
        assert specs_for({"X-Internal-Key": INTERNAL,
                          "X-API-Key": "dchub_real_paid"}) is True, (
            "a PAYING agent on the MCP path lost access — the gate must read "
            "the forwarded end-user credential, not merely refuse the transport")
        assert specs_for({"X-API-Key": "dchub_real_paid"}) is True, (
            "a paying REST caller lost access")
    finally:
        mtg.detect_tier_for_data_gate = original
