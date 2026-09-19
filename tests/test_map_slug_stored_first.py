#!/usr/bin/env python3
"""tests/test_map_slug_stored_first.py — /api/v1/map must emit the FROZEN
canonical_slug, not a recomputed one.

NO NETWORK, NO DB, and it does NOT import main (house rule). The slug-assignment
loop is lifted out of main.py's AST and EXECUTED against fakes, so this fences
the real source rather than a restatement of it.

THE BUG (measured live 2026-09-18, /api/v1/map):
    109 of 150 sampled emitted slugs returned 301.
    switch-las-vegas-4-68d0ff15 -> switch-switch-las-vegas-4-68d0ff15
    The hash8 tail is IDENTICAL; only the slug BODY moved.
The freeze ran 2026-07-03 and stored the DOUBLED pre-dedupe body. The
provider-prefix dedupe landed 2026-07-28 and changed what build_canonical_slug
returns. Every row frozen before 07-28 therefore has a stored slug the builder
no longer reproduces, and this endpoint was emitting the builder's answer.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MAIN = os.path.join(ROOT, "main.py")
_SRC = open(MAIN, encoding="utf-8", errors="replace").read()
_TREE = ast.parse(_SRC)


def _map_fn():
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == "api_v1_map":
            return node
    raise AssertionError("api_v1_map not found in main.py")


def _slug_loops():
    """(composition loop, collapse loop) — both assign f['slug']."""
    fn = _map_fn()
    comp, coll = [], []
    for node in ast.walk(fn):
        if isinstance(node, ast.For):
            seg = ast.get_source_segment(_SRC, node)
            if not seg or "'slug'" not in seg:
                continue
            (comp if "build_canonical_slug(" in seg else coll).append(seg)
    assert len(comp) == 1, f"expected 1 composition loop, got {len(comp)}"
    assert len(coll) == 1, f"expected 1 served-slug collapse loop, got {len(coll)}"
    return comp[0], coll[0]


def _slug_loop_src():
    return _slug_loops()[0]


def _run_loop(facilities, canon_by_id, builder_returns):
    """Execute main.py's OWN loop against injected fakes."""
    calls = []

    def _builder(provider, name):
        calls.append((provider, name))
        return builder_returns

    ns = {
        "facilities": facilities,
        "_canon_by_id": canon_by_id,
        "build_canonical_slug": _builder,
    }
    src = _slug_loop_src()
    exec(compile(ast.parse(src), "<map-slug-loop>", "exec"), ns)  # noqa: S102
    return facilities, calls


FROZEN = "switch-switch-las-vegas-4-68d0ff15"     # what /facilities/<slug> serves
REBUILT = "switch-las-vegas-4-68d0ff15"           # what the builder now returns


def test_stored_canonical_slug_wins_over_the_rebuilt_one():
    """THE regression. Before the fix this returned REBUILT and 301'd."""
    facs = [{"id": 7, "provider": "Switch", "name": "Switch Las Vegas 4"}]
    facs, _ = _run_loop(facs, {7: FROZEN}, REBUILT)
    assert facs[0]["slug"] == FROZEN, (
        f"map emitted {facs[0]['slug']!r}; /facilities/{FROZEN} is canonical, so "
        "this is a 301 on every map link")


def test_builder_is_the_fallback_when_the_row_is_not_frozen():
    """A row the freeze has not reached must still get a usable slug."""
    facs = [{"id": 9, "provider": "Switch", "name": "Switch Las Vegas 4"}]
    facs, calls = _run_loop(facs, {}, REBUILT)
    assert facs[0]["slug"] == REBUILT
    assert calls, "builder must be consulted when there is no stored slug"


def test_empty_stored_value_does_not_shadow_the_builder():
    """'' must not win — it would emit /facilities/ and soft-404."""
    facs = [{"id": 3, "provider": "Switch", "name": "Switch Las Vegas 4"}]
    facs, _ = _run_loop(facs, {3: ""}, REBUILT)
    assert facs[0]["slug"] == REBUILT


def test_unsluggable_row_still_yields_empty_string_not_none():
    """The '' guard downstream depends on this never being None."""
    facs = [{"id": 5, "provider": None, "name": None}]
    facs, _ = _run_loop(facs, {}, None)
    assert facs[0]["slug"] == ""


def test_map_delegates_to_the_stored_slug_helper():
    fn_src = ast.get_source_segment(_SRC, _map_fn()) or ""
    assert "stored_slugs_by_id(" in fn_src, (
        "api_v1_map must read the frozen slug via stored_slugs_by_id()")


def test_the_column_is_probed_before_it_is_selected():
    """Live DDL lags the code; a bare SELECT canonical_slug would 500 the map."""
    src = open(os.path.join(ROOT, "routes", "facility_slug_freeze.py"),
               encoding="utf-8", errors="replace").read()
    body = src[src.index("def stored_slugs_by_id"):][:3000]
    assert "information_schema.columns" in body, "helper must probe for the column"
    assert body.index("information_schema.columns") < body.index("SELECT id, canonical_slug"), \
        "probe must precede the canonical_slug SELECT"


# ── the helper itself, against a cursor that READS THE SQL IT IS GIVEN ──────

class _Cur:
    """Fake cursor that branches on the actual SQL, not on call order."""
    def __init__(self, has_col=True, rows=(), boom=False):
        self.has_col, self.rows, self.boom = has_col, rows, boom
        self._last = None; self.seen = []
    def execute(self, sql, args=None):
        if self.boom:
            raise RuntimeError("relation does not exist")
        self.seen.append(sql); self._last = sql
        if "information_schema.columns" in sql:
            assert "canonical_slug" in sql, "probe must name the column it checks"
        elif "SELECT id, canonical_slug" in sql:
            assert args and isinstance(args[0], list), "ids must be bound as a list"
            self._ids = args[0]
    def fetchone(self):
        assert "information_schema.columns" in (self._last or "")
        return (1,) if self.has_col else None
    def fetchall(self):
        assert "SELECT id, canonical_slug" in (self._last or "")
        return [r for r in self.rows if r[0] in self._ids]


class _Conn:
    def __init__(self): self.rolled = False
    def rollback(self): self.rolled = True


def _helper():
    import importlib
    return importlib.import_module("routes.facility_slug_freeze").stored_slugs_by_id


def test_helper_returns_the_frozen_slug_for_frozen_rows():
    got = _helper()(_Cur(rows=[(7, FROZEN), (8, "other-slug")]), _Conn(), [7])
    assert got == {7: FROZEN}


def test_helper_returns_empty_when_the_column_does_not_exist_yet():
    """Live DDL can lag; callers must fall back to the builder, not 500."""
    assert _helper()(_Cur(has_col=False, rows=[(7, FROZEN)]), _Conn(), [7]) == {}


def test_helper_swallows_db_errors_and_rolls_back():
    conn = _Conn()
    assert _helper()(_Cur(boom=True), conn, [7]) == {}
    assert conn.rolled, "a failed probe must roll back or the txn stays poisoned"


def test_helper_does_not_query_at_all_for_an_empty_id_list():
    cur = _Cur()
    assert _helper()(cur, _Conn(), []) == {}
    assert cur.seen == [], "no ids means no SQL"
    assert _helper()(cur, _Conn(), [None, None]) == {}
    assert cur.seen == [], "all-None ids means no SQL"


# ── stage 2: collapse to the slug the URL is actually SERVED at ────────────

KEEPER = "kaynarca-santrali-kaynarca-santrali-c95ef7d8"
DUP = "unknown-kaynarca-santrali-d787fb4b"


def _run_collapse(facilities, served):
    ns = {"facilities": facilities, "_served": served}
    exec(compile(ast.parse(_slug_loops()[1]), "<collapse>", "exec"), ns)  # noqa: S102
    return facilities


def test_a_duplicate_row_slug_collapses_to_its_keeper():
    """THE regression: the map linked the duplicate, costing a 301 hop."""
    facs = [{"id": 1, "slug": DUP}]
    assert _run_collapse(facs, {DUP: KEEPER})[0]["slug"] == KEEPER


def test_a_terminal_slug_is_left_alone():
    facs = [{"id": 2, "slug": KEEPER}]
    assert _run_collapse(facs, {KEEPER: KEEPER})[0]["slug"] == KEEPER


def test_a_slug_the_resolver_never_answered_for_is_kept():
    """served_slugs failing closed must not blank a link."""
    facs = [{"id": 3, "slug": DUP}]
    assert _run_collapse(facs, {})[0]["slug"] == DUP


def test_an_empty_slug_is_not_looked_up():
    facs = [{"id": 4, "slug": ""}]
    assert _run_collapse(facs, {"": "boom"})[0]["slug"] == ""


def test_map_does_not_reimplement_the_twin_redirect_predicate():
    """The four-condition same-physical-site rule has exactly ONE owner.

    Re-deriving it here would risk pointing a marker at the wrong BUILDING:
    two rows with different street addresses deliberately serve 200 +
    cross-canonical rather than 301.
    """
    fn_src = ast.get_source_segment(_SRC, _map_fn()) or ""
    assert "served_slugs(" in fn_src, "map must delegate to the route's resolver"
    code = "\n".join(l for l in fn_src.splitlines() if not l.strip().startswith("#"))
    for banned in ("_same_physical_site", "_SAME_SITE_METRES", "slug_rows",
                   "duplicate_of_id"):
        assert banned not in code, \
            f"map re-derives the twin-redirect predicate ({banned}); delegate instead"


if __name__ == "__main__":
    import traceback
    failed = 0
    for nm, fn in sorted(globals().items()):
        if nm.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {nm}")
            except Exception:
                failed += 1; print(f"FAIL {nm}"); traceback.print_exc()
    sys.exit(1 if failed else 0)
