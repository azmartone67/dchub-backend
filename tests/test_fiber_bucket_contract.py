"""tests/test_fiber_bucket_contract.py — the backend half of a CROSS-REPO pin.

_bucket() in routes/connectivity_score.py is the source of truth for the
near_net_bucket enum that dchub-mcp-server's get_fiber_readiness description
publishes to every MCP client. When #2731 added the "unknown" bucket, the
served tool description kept advertising the old four-value enum for a day
(fixed in dchub-mcp-server #195) — an agent reading an undocumented "unknown"
will most likely read it as the bad end of the scale, reinstating exactly the
false greenfield claim #2731 removed. sync-tools-manifest guards
server.mjs <-> mcp-server.json, but nothing tied either to what _bucket()
actually returns. This pin is that tie.

★ COUNTERPART: dchub-mcp-server/test/fiber-bucket-contract.test.mjs pins the
same list against the get_fiber_readiness description. If THIS test fails
because the buckets legitimately changed, the change is not done until:
  1. server.mjs's get_fiber_readiness description documents the new enum
     (and, for a new "not measured"-style value, says what it does NOT mean),
  2. `node scripts/sync-tools-manifest.mjs --fix` regenerates mcp-server.json,
  3. BOTH pinned lists are updated.
A cross-repo change cannot be forced from one repo's CI — this pin exists so
whichever side changes first fails loudly and names the other side.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "connectivity_score.py")

# The published enum. Keep sorted-set semantics: source order of returns is
# incidental; the CONTRACT is the value set.
PINNED_BUCKETS = {"on-net", "near-net", "acceptable", "build-required", "unknown"}


def _bucket_return_strings():
    """AST-extract every string constant returned by _bucket().

    Returns (values, error). Collection stays inside functions and never
    imports the route module (which would pull DB plumbing); if _bucket is
    ever refactored to return through variables instead of literals, extend
    this extractor — the equality test below will fail loudly rather than
    silently passing on an empty set.
    """
    try:
        tree = ast.parse(open(SRC, encoding="utf-8").read())
    except OSError as e:
        return None, f"cannot read {SRC}: {e}"
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_bucket"), None)
    if fn is None:
        return None, "def _bucket not found in routes/connectivity_score.py"
    vals, returns = set(), 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            returns += 1
            for c in ast.walk(node.value):
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    vals.add(c.value)
    if returns == 0:
        return None, "_bucket has no return statements — extraction is vacuous"
    return vals, None


def test_extraction_finds_a_real_bucket_function():
    """Positive control: a silently-empty extraction must FAIL, not pass.
    (House rule: assert the parse found a real FunctionDef body.)"""
    vals, err = _bucket_return_strings()
    assert err is None, err
    assert vals, "_bucket returned no string literals — extractor or refactor drift"


def test_bucket_returns_exactly_the_published_enum():
    vals, err = _bucket_return_strings()
    assert err is None, err
    assert vals == PINNED_BUCKETS, (
        f"_bucket() returns {sorted(vals)} but the published near_net_bucket "
        f"enum is {sorted(PINNED_BUCKETS)}. If this change is intentional, the "
        "MCP surface must move WITH it: update the get_fiber_readiness "
        "description in dchub-mcp-server/server.mjs, regenerate mcp-server.json "
        "via `node scripts/sync-tools-manifest.mjs --fix`, and update BOTH "
        "pinned lists (this file + "
        "dchub-mcp-server/test/fiber-bucket-contract.test.mjs). An undocumented "
        "bucket value reads as 'the bad end of the scale' to agents — that is "
        "the #2731/'unknown' incident this pin exists to prevent."
    )
