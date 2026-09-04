"""Guards for r-twin-bleed (2026-08-03).

A state-suffixed twin's display name comma-strips back to the bare city
("Portland, ME" -> "Portland"), so _gather_market_facts' NAME-match facility
join re-merged exactly what the r-portland-canon / r-aurora-canon slug
disambiguation had just separated. Measured live 2026-08-03, AFTER both slug
fixes had landed and the briefs had regenerated:

    aurora      29 fac / 209 MW  |  aurora-co  29 fac / 209 MW   <- IDENTICAL
    portland-me 72 fac / 578 MW  (Maine is really 6 fac / 19 MW)

So the slug canon alone cannot fix its own briefs — regenerating produced the
same wrong numbers. Fix: qualify the facility join by state, but ONLY for the
slugs on either side of a known collision.

The scoping is the whole point, and both halves are pinned below:
  * a BLANKET state qualifier re-opens the NoVA zero (#1546 / r-nova-zero) —
    metro-keyed markets match by `market`, not city, with mixed member states;
  * it would also silently delete the 17 Columbus rows whose state is spelled
    'OHIO' rather than 'OH' (real Amazon New Albany / AWS CMH facilities).

Source-level + pure imports only; never imports main or routes/dcpi (which
builds MARKETS at import time and needs a DB — house rule).
"""

import ast
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(rel):
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _fac_union_sql():
    # r-latam-twin (2026-09-03): the literal moved from a local inside
    # _gather_market_facts to the module constant _FAC_UNION_SQL when the
    # .json twin became a second call site. Read it where it lives — the
    # invariants below are about the SQL, not about which scope holds it.
    m = re.search(r'_FAC_UNION_SQL = """(.*?)"""',
                  _src("routes/market_deep_dive.py"), re.S)
    assert m, "_FAC_UNION_SQL literal not found"
    return m.group(1)


def test_both_union_arms_carry_the_gated_state_predicate():
    """BOTH arms must gate, or the un-patched arm keeps merging the twin.
    `NOT %(qualify)s OR ...` keeps it a no-op for every normal market."""
    sql = _fac_union_sql()
    assert sql.count("NOT %(qualify)s") == 2, (
        "expected the gated state predicate on BOTH the facilities and "
        "discovered_facilities arms of _fac_union"
    )
    assert sql.count("UPPER(TRIM(COALESCE(state,''))) = %(state)s") == 2


def test_the_union_is_one_string_shared_by_every_call_site():
    """Two divergent copies is how one call site silently keeps the bleed."""
    src = _src("routes/market_deep_dive.py")
    # Exactly ONE literal, and it is the module constant every call site
    # names. A second `= """..."""` union anywhere in this file is a copy.
    assert src.count('_FAC_UNION_SQL = """') == 1
    assert src.count('_fac_union = """') == 0, (
        "the union was re-inlined as a local — two copies is how one call "
        "site silently keeps the bleed"
    )
    # every execute() of the union passes a qualify/state-bearing bundle
    _sites = src.count("_fac_union +") + src.count("_FAC_UNION_SQL +")
    _bundles = src.count("_fac_args)") + src.count("_args)")
    assert _sites <= _bundles, (
        "a union call site does not pass its param bundle — it will raise on "
        "the missing %(qualify)s/%(state)s keys or silently skip qualifying"
    )


def test_qualify_is_scoped_to_collision_slugs_only():
    """The predicate must be gated on membership in _collision_slugs(), NOT
    applied unconditionally. Unconditional = the NoVA/#1546 regression."""
    fn = _func(ast.parse(_src("routes/market_deep_dive.py")),
               "_gather_market_facts")
    seg = ast.get_source_segment(_src("routes/market_deep_dive.py"), fn)
    assert "_collision_slugs()" in seg
    m = re.search(r'"qualify":\s*(.+)', seg)
    assert m, "no 'qualify' key built in _gather_market_facts"
    expr = m.group(1)
    assert "_collision_slugs()" in expr, (
        "qualify is no longer gated on the collision set — a blanket state "
        "qualifier zeroes metro-keyed markets (Northern Virginia) and drops "
        "the Columbus rows spelled 'OHIO'"
    )
    assert "_state" in expr, (
        "qualify must also require a known state — a blank mps state must "
        "fail OPEN to the old behaviour, never match state = ''"
    )


def test_collision_set_covers_both_sides_and_the_alias_target():
    """The disambiguation table names only the state-suffixed side. The other
    side may be a bare slug (aurora=IL) or a hardcoded alias target
    (portland -> portland-or); miss the latter and Oregon keeps counting
    Maine's facilities."""
    fn = _func(ast.parse(_src("routes/market_deep_dive.py")),
               "_collision_slugs")
    seg = ast.get_source_segment(_src("routes/market_deep_dive.py"), fn)
    assert "_CITY_MARKET_DISAMBIGUATION" in seg, (
        "collision set no longer derives from the disambiguation table — a "
        "copied literal drifts silently (regex-twin class)"
    )
    assert "canonical_slug" in seg, (
        "collision set no longer resolves the alias target — bare 'portland' "
        "is retired onto 'portland-or', which would stay un-qualified"
    )
    assert "out.add(_bare)" in seg and "out.add(_slug)" in seg


def test_state_is_selected_and_exposed_on_the_facts_dict():
    """qualify needs the market's own state; it comes from the mps row."""
    src = _src("routes/market_deep_dive.py")
    fn = _func(ast.parse(src), "_gather_market_facts")
    seg = ast.get_source_segment(src, fn)
    assert "computed_at, time_to_power_months, state" in seg, (
        "market_power_scores SELECT no longer returns `state`"
    )
    assert '"state":' in seg, "facts dict no longer exposes state"
