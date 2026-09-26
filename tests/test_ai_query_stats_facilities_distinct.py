"""/api/ai/query?type=stats quotes the canon facility population.

2026-09-26: the free citation sentence said "there are 23,172 data center
facilities" (the keeper count, is_duplicate = 0) while /api/v1/canon/phrases,
mcp.json, the MCP server, GitHub About and the homepage all published
"24,600+" (facilities_distinct, 24,687 live) — canon since #4924. The QA
super-user probe takes canon from this sentence, so it would have convicted
every correct "24,600+" registry listing as a CRITICAL over-claim.

The helper and the SQL constant are extracted from main.py by AST and RUN —
importing main.py boots the whole app (tests/ must not).
"""
import ast
import pathlib
import sqlite3

MAIN = pathlib.Path(__file__).resolve().parent.parent / "main.py"
_TREE = ast.parse(MAIN.read_text())


def _ns():
    wanted = {"_stats_citation_facilities", "_STATS_FACILITIES_SQL"}
    body = [n for n in _TREE.body
            if (isinstance(n, ast.FunctionDef) and n.name in wanted)
            or (isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) in wanted for t in n.targets))]
    assert len(body) == 2, "helper or SQL constant missing from main.py"
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(MAIN), "exec"), ns)
    return ns


def _ai_query():
    return next(n for n in _TREE.body
                if isinstance(n, ast.FunctionDef) and n.name == "ai_query")


# Snapshot shaped like canonical_stats: both populations present, as live.
SNAP = {"facilities_distinct": 24687, "facilities_verified": 23172,
        "facilities_with_keeper_distinct": 23172, "deals": 1659}


def test_a_measured_snapshot_quotes_facilities_distinct():
    f = _ns()["_stats_citation_facilities"]
    assert f(SNAP, lambda k: True) == 24687


def test_an_unmeasured_value_is_never_quoted():
    # The snapshot is seeded with citation floors (facilities_distinct = 400).
    # Quoting the seed would publish a ~60x under-claim; 0 sends the caller to
    # the SQL fallback instead.
    f = _ns()["_stats_citation_facilities"]
    seeded = {"facilities_distinct": 400, "facilities_verified": 400}
    assert f(seeded, lambda k: False) == 0
    live_other = lambda k: k == "facilities_verified"
    assert f(SNAP, live_other) == 0, "must ask about facilities_distinct itself"
    assert f(None, lambda k: True) == 0


def test_the_sql_fallback_counts_distinct_buildings_duplicates_included():
    sql = _ns()["_STATS_FACILITIES_SQL"]
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE discovered_facilities "
               "(canonical_slug TEXT, is_duplicate INTEGER)")
    db.executemany("INSERT INTO discovered_facilities VALUES (?, ?)", [
        ("a", 0), ("a", 1),      # one building, two rows
        ("b", 1),                # a real building with no keeper row
        ("c", 0),
        (None, 0),               # unslugged row is not a building
    ])
    assert db.execute(sql).fetchone()[0] == 3
    keeper = db.execute(
        "SELECT COUNT(DISTINCT canonical_slug) FROM discovered_facilities "
        "WHERE COALESCE(is_duplicate,0)=0 AND canonical_slug IS NOT NULL"
    ).fetchone()[0]
    assert keeper == 2, "fixture must separate the two populations"


def test_the_route_uses_the_helper_and_the_constant():
    fn = _ai_query()
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "_stats_citation_facilities" in names
    assert "_STATS_FACILITIES_SQL" in names
    consts = [n.value for n in ast.walk(fn)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "facilities_verified" not in consts, (
        "the stats sentence must not read the keeper population again")
    # The search branch's row listings rightly hide duplicates; only the
    # building COUNT behind the citation sentence must not.
    assert not any("COUNT(DISTINCT canonical_slug)" in c and "is_duplicate" in c
                   for c in consts)


def test_the_helper_did_not_steal_the_routes_decorator():
    fn = _ai_query()
    assert [ast.unparse(d) for d in fn.decorator_list] == ["app.route('/api/ai/query')"]
