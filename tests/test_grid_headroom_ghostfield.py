"""Regression guards: /grid pages DERIVE headroom — never read the ghost field.

routes/grid_public_routes.py's hub cards read `live.get('headroom_pct')` off
the /api/v1/grid/intelligence payload — a field that has NEVER existed in that
payload (grep main.py: 0 hits) — so every hub card rendered '0% headroom'
24/7. Same ghost-field class as radar's renewable_share_pct (PR #2018).
`total_capacity_mw` was the sibling ghost feeding the per-ISO page's fallback.

The fix derives headroom from the REAL payload fields (demand_mw + the
demand_24h series' peak) in ONE shared helper, _demand_capacity_headroom,
used by BOTH the hub-card path (grid_hub) and the per-ISO page
(render_grid_iso_html). An absent series yields None → rendered '—', so a
structural 0% (NGESO/AEMO flattened snapshots carry no demand_24h) can't
impersonate a genuine at-peak 0%.

These guards pin:
  1. no code path .get()s or subscripts the ghost fields anywhere in the
     module — any receiver name, so a rename can't smuggle the read back;
  2. grid_hub and render_grid_iso_html both call the shared helper;
  3. the helper derives from the real fields and names no ghost in its BODY;
  4. must-fail control: the detector fires on a literal reintroduction, so
     guard 1 can never be vacuously green.

House rules: static AST extraction, no import of routes.grid_public_routes
(it imports flask at module scope), nothing executes at module scope here.
"""
import ast
import os

_GRID = os.path.join(os.path.dirname(__file__), "..", "routes",
                     "grid_public_routes.py")

# Fields the /api/v1/grid/intelligence payload has never shipped. Reading
# them "works" (dict.get → None → or-0) which is exactly why the 0% rendered
# silently for months.
_GHOST_FIELDS = ("headroom_pct", "total_capacity_mw")


def _tree(path: str, min_body: int) -> ast.Module:
    with open(os.path.abspath(path), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # Guard the guard: an empty/degenerate parse would vacuously pass every
    # search below (2026-07-28 lesson — assert it parsed, never just filter).
    assert isinstance(tree, ast.Module) and len(tree.body) > min_body, (
        f"{path} parsed to a degenerate module — extraction harness is not "
        "looking at the real file")
    return tree


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"routes/grid_public_routes.py no longer defines {name}() — these "
        "ghost-field guards need updating, not deleting")


def _code_consts(fn: ast.FunctionDef) -> set:
    """String constants in the function BODY, docstring excluded — the
    helper's docstring NAMES the ghost fields it exists to kill, so counting
    it would make the negative asserts below self-defeating (the 07-19
    'comments satisfy grep' lesson, applied to AST)."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return {n.value for stmt in body for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _ghost_payload_reads(tree: ast.Module) -> list:
    """Every read of a ghost field in the tree, as (lineno, shape) pairs.

    Two shapes count, on ANY receiver (a renamed payload variable must not
    escape the guard): `<recv>.get('<ghost>')` calls and `<recv>['<ghost>']`
    subscripts. Docstrings/comments mentioning the names don't trip this —
    only Call/Subscript nodes are inspected.
    """
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in _GHOST_FIELDS):
            hits.append((node.lineno, f".get({node.args[0].value!r})"))
        elif isinstance(node, ast.Subscript):
            for c in ast.walk(node.slice):
                if isinstance(c, ast.Constant) and c.value in _GHOST_FIELDS:
                    hits.append((node.lineno, f"[{c.value!r}]"))
    return hits


# ── 1. the ghost reads must never come back, under any variable name ────────

def test_no_ghost_field_reads_in_grid_public():
    hits = _ghost_payload_reads(_tree(_GRID, 10))
    assert not hits, (
        f"routes/grid_public_routes.py reads ghost payload fields again at "
        f"{hits} — /api/v1/grid/intelligence has NEVER shipped "
        f"{_GHOST_FIELDS}; dict.get makes the read silently None → '0% "
        "headroom' on every card 24/7 (the radar renewable_share_pct class, "
        "PR #2018). Derive via _demand_capacity_headroom instead")


# ── 2. hub cards and the per-ISO page share ONE derivation ──────────────────

def test_hub_and_iso_pages_share_headroom_derivation():
    tree = _tree(_GRID, 10)
    for renderer in ("grid_hub", "render_grid_iso_html"):
        calls = [n for n in ast.walk(_fn(tree, renderer))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_demand_capacity_headroom"]
        assert calls, (
            f"{renderer}() no longer calls _demand_capacity_headroom() — the "
            "hub-card and per-ISO headroom math must stay ONE shared helper, "
            "or the two paths drift apart again (the hub side is how the "
            "ghost headroom_pct read shipped in the first place)")


# ── 3. the helper computes from fields the payload actually ships ───────────

def test_helper_derives_from_real_payload_fields():
    consts = _code_consts(_fn(_tree(_GRID, 10), "_demand_capacity_headroom"))
    for real in ("demand_mw", "demand_24h"):
        assert real in consts, (
            f"_demand_capacity_headroom() no longer reads `{real}` — those "
            "are the only real fields in the intelligence payload; without "
            "them headroom cannot be derived, only faked")
    for ghost in _GHOST_FIELDS:
        assert ghost not in consts, (
            f"_demand_capacity_headroom() BODY references ghost field "
            f"`{ghost}` — the helper exists to end those reads, not to "
            "relocate them")


# ── 4. must-fail control: prove the detector actually detects ───────────────

def test_control_detector_fires_on_literal_reintroduction():
    bad = ast.parse(
        "def grid_hub():\n"
        "    a = live.get('headroom_pct')\n"
        "    b = payload['total_capacity_mw']\n")
    hits = _ghost_payload_reads(bad)
    assert len(hits) == 2, (
        f"must-fail control: the ghost-read detector caught {hits!r} from a "
        "snippet containing both banned shapes (a .get call and a "
        "subscript) — test_no_ghost_field_reads_in_grid_public is vacuously "
        "green and guards nothing")
