"""SH52-057 — one canonical HIFLD transmission layer, read from one place.

THE DEFECT THIS PINS (measured live 2026-08-12):

  Two modules in this repo disagreed about which ArcGIS service is the real
  Electric_Power_Transmission_Lines, and the fiber-discovery lane was on the
  loser:

    infrastructure_discovery.HIFLD_APIS['transmission_lines']
        -> services1/Hp6G80Pky0om7QvQ    52,244 features
    land_power_crawler._ARCGIS_LAYERS['hifld-transmission']
        -> services5/HDRa0B57OVrv2E1q    89,744 features

  land_power_crawler's min_rows floor of 70,000 exists SPECIFICALLY to reject
  the 52,244 layer as a different population — and infrastructure_discovery
  fed fiber_routes from exactly that rejected layer. Both numbers re-verified
  against the live FeatureServers on 2026-08-12.

  Second defect, same lane: _sync_hifld_transmission_lines swept 20 markets at
  max_records=100, a hard ceiling of 2,000 distinct lines FOREVER. It held
  1,826 rows / 1,742 distinct upstream ids — 87% saturated. 19 of the 20
  markets came back with ArcGIS's exceededTransferLimit flag set, which the
  code discarded, so the ceiling was invisible.

★ WHY THESE TESTS ARE AST-ONLY. The unit-tests job (.github/workflows/
pre-merge.yml) installs a deliberately light dep set — pytest, requests, flask,
pyyaml, psycopg2, Unidecode, Pillow — not requirements.txt. Importing
infrastructure_discovery would drag in db_utils and pull the assertions into
whatever that set happens to be on a given morning; these checks are about what
the SOURCE says, so they read it with `ast` and import nothing from the repo.
Nothing runs at module scope. That also means these guards keep working if the
CI dep list is trimmed again.

★★ MUST-FAIL CHECK — ACTUALLY RUN, NOT ASSERTED. This file was copied onto a
pristine worktree at the parent commit (b90a7f9d) and executed there; a guard
that has never been observed red is not a guard. Measured:

    unpatched (b90a7f9d): 7 failed, 1 passed
    patched:              8 passed

The one test that passes UNPATCHED is the control:
test_no_hardcoded_superseded_transmission_url_in_code[land_power_crawler.py].
land_power_crawler was the module that was already correct — it never carried
the superseded URL. If that case ever goes red, the fix has been applied
backwards.
"""
import ast
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The superseded org. Any transmission URL on it, in either module, is the bug.
_SUPERSEDED_ORG = "Hp6G80Pky0om7QvQ"
_CANON_ORG = "HDRa0B57OVrv2E1q"

# Largest single DC_MARKETS market measured at radius_m=50000 against the
# canonical layer on 2026-08-12 (Dallas-Fort Worth, 792 lines). A record cap
# at or below this truncates a real market, which is the ceiling being fixed.
_LARGEST_MARKET_LINES = 792


def _parse(relpath, min_nodes=3):
    """Parse a repo file to an AST, asserting the parse actually produced nodes.

    ★ An empty parse satisfies every isinstance() filter downstream and makes
    the whole suite vacuously green. Assert the tree is non-trivial FIRST.

    min_nodes is explicit because util/hifld_layers.py is legitimately three
    top-level nodes (docstring, registry, accessor) — the point of the check is
    "this parsed to something real", not "this file is big", and padding a
    module to satisfy a threshold would defeat both.
    """
    path = os.path.join(_ROOT, relpath)
    assert os.path.exists(path), f"{relpath} missing"
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert len(src) > 500, f"{relpath} suspiciously small ({len(src)}b)"
    tree = ast.parse(src)
    assert len(tree.body) >= min_nodes, \
        f"{relpath} parsed to {len(tree.body)} top-level nodes (want >= {min_nodes})"
    return tree, src


def _strip_comments(src):
    """Source with comments and docstrings removed.

    The modules DISCUSS the superseded URL at length on purpose — the whole
    point of the comments is to name what went wrong. A naive substring scan
    would therefore fail forever on prose. Unparsing the AST drops comments,
    and docstrings are cleared explicitly, so what remains is code only.
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)


def _module_dict_entry(tree, dict_name, key):
    """The value node for one key of a module-level dict literal."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == dict_name:
                    assert isinstance(node.value, ast.Dict), \
                        f"{dict_name} is not a dict literal"
                    for k, v in zip(node.value.keys, node.value.values):
                        if isinstance(k, ast.Constant) and k.value == key:
                            return v
                    raise AssertionError(f"{dict_name}[{key!r}] not found")
    raise AssertionError(f"{dict_name} not found at module level")


def _func(tree, name, cls=None):
    scope = tree.body
    if cls:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls:
                scope = node.body
                break
        else:
            raise AssertionError(f"class {cls} not found")
    for node in scope:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found" + (f" in {cls}" if cls else ""))


# ── 1. the canonical registry exists and names the maintained layer ─────────
def test_canonical_registry_declares_the_maintained_transmission_layer():
    tree, _ = _parse("util/hifld_layers.py")
    entry = _module_dict_entry(tree, "HIFLD_LAYERS", "hifld-transmission")
    spec = ast.literal_eval(entry)

    cands = spec["candidates"]
    assert cands, "hifld-transmission declares no candidates"
    assert _CANON_ORG in cands[0], (
        f"preferred transmission candidate is not the maintained layer: {cands[0]}")
    assert not any(_SUPERSEDED_ORG in c for c in cands), (
        "the superseded 52,244-feature layer is listed as a candidate — falling "
        "back to it is the silent population swap this registry prevents")
    assert spec["min_rows"] >= 70000, (
        f"min_rows={spec['min_rows']} no longer excludes the 52,244-feature "
        "layer, which is the floor's entire purpose")
    # The fields _sync_hifld_transmission_lines actually reads, plus the two
    # the crawler's parser needs. Losing any of them silently degrades identity.
    for f in ("ID", "OWNER", "VOLTAGE", "SUB_1", "SUB_2", "TYPE", "STATUS"):
        assert f in spec["required_fields"], f"required field {f} dropped"


# ── 2. both modules read that one definition ───────────────────────────────
@pytest.mark.parametrize("relpath", [
    "land_power_crawler.py",
    "infrastructure_discovery.py",
])
def test_module_imports_the_shared_registry(relpath):
    tree, _ = _parse(relpath)
    imported = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.module or "") == "util.hifld_layers"
    ]
    assert imported, (
        f"{relpath} does not import from util.hifld_layers — if it defines its "
        "own transmission URL again, the two modules can disagree again, which "
        "is exactly how the fiber lane ended up on the superseded layer")


# ── 3. no module keeps its own hardcoded transmission URL ──────────────────
@pytest.mark.parametrize("relpath", [
    "land_power_crawler.py",
    "infrastructure_discovery.py",
])
def test_no_hardcoded_superseded_transmission_url_in_code(relpath):
    _, src = _parse(relpath)
    code = _strip_comments(src)
    assert _SUPERSEDED_ORG not in code, (
        f"{relpath} still carries a live reference to the superseded ArcGIS org "
        f"{_SUPERSEDED_ORG} in CODE (comments are exempt and were stripped). "
        "That org serves 52,244 transmission features against the maintained "
        "layer's 89,744.")


def test_infra_discovery_transmission_url_is_resolved_not_literal():
    """HIFLD_APIS['transmission_lines'] must be a lookup, not a string."""
    tree, _ = _parse("infrastructure_discovery.py")
    value = _module_dict_entry(tree, "HIFLD_APIS", "transmission_lines")
    assert not isinstance(value, ast.Constant), (
        "HIFLD_APIS['transmission_lines'] is a hardcoded literal again — that "
        "is the drift this change removed; it must resolve from "
        "util.hifld_layers so there is one definition, not two")
    assert isinstance(value, ast.Call) and getattr(value.func, "id", "") == "layer_url", (
        "expected layer_url('hifld-transmission') from util.hifld_layers")


# ── 4. the lane's record cap clears the largest real market ────────────────
def test_hifld_sweep_record_cap_does_not_truncate_a_real_market():
    """max_records must exceed the biggest market, or the lane is capped.

    20 markets x 100 records was a permanent ceiling of 2,000 distinct lines
    and the table sat at 1,742. This asserts the cap is sized to the measured
    population rather than to a round number someone typed once.
    """
    tree, _ = _parse("infrastructure_discovery.py")
    fn = _func(tree, "_sync_hifld_transmission_lines", cls="FiberRouteDiscovery")

    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "_query_hifld_nearby"]
    assert len(calls) == 1, f"expected 1 _query_hifld_nearby call, found {len(calls)}"

    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "max_records" in kw, "_query_hifld_nearby called without max_records"

    node = kw["max_records"]
    if isinstance(node, ast.Constant):
        cap = node.value
    else:
        # e.g. self.HIFLD_MAX_RECORDS — resolve the class attribute literal.
        attr = getattr(node, "attr", None)
        assert attr, f"max_records is neither a literal nor an attribute: {ast.dump(node)}"
        cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "FiberRouteDiscovery")
        consts = [a.value.value for a in cls.body
                  if isinstance(a, ast.Assign)
                  and any(getattr(t, "id", "") == attr for t in a.targets)
                  and isinstance(a.value, ast.Constant)]
        assert consts, f"{attr} not found as a literal class attribute"
        cap = consts[0]

    assert isinstance(cap, int), f"max_records resolved to non-int {cap!r}"
    assert cap > _LARGEST_MARKET_LINES, (
        f"max_records={cap} truncates the largest DC market "
        f"({_LARGEST_MARKET_LINES} lines within 50km, measured 2026-08-12). "
        "The lane's ceiling is len(DC_MARKETS) * max_records — at 100 that was "
        "2,000 distinct lines forever, against 1,742 already held.")


# ── 5. the truncation signal is no longer discarded ────────────────────────
def test_query_helper_surfaces_exceeded_transfer_limit():
    """ArcGIS says when it truncated; the lane must not throw that away.

    This is what made the ceiling invisible: at max_records=100, 19 of 20
    markets returned exceededTransferLimit=true and nothing logged it, so a
    saturated lane and a complete lane looked identical.
    """
    tree, _ = _parse("infrastructure_discovery.py")
    fn = _func(tree, "_query_hifld_nearby")
    body = ast.unparse(fn)
    assert "exceededTransferLimit" in body, (
        "_query_hifld_nearby ignores exceededTransferLimit — a truncated "
        "response is indistinguishable from a complete one, which is why a "
        "2,000-line ceiling went unnoticed until the table was 87% full")
