"""Guards for the 2026-07-31 depth-predicate fix on the AI Capacity Index.

Both `routes/ai_capacity_index.py` and `routes/market_intel_preview.py` carried
`COALESCE(status,'') <> 'active'`, commented "'active'=empty shells (0 MW)".
Measured on the read replica 2026-07-31 that premise was false: over the fleet
the predicate excluded 4,325 zero-MW rows while COUNTING 4,693 other zero-MW
rows — 41.4pct of everything it returned. The literal identified which ingest
path stamped the row (PeeringDB / OSM bulk loads), never whether the facility
had capacity, and util/status_taxonomy.py had already ruled 'active' ->
OPERATIONAL two days after that r-fix landed.

It was decaying too: PR #2047 routed those sources through canon_status(), so
shells written after 2026-07-31 arrive as 'Operational' and stop matching.

These pin the replacement basis — distinct OPERATIONAL fleet facilities, no
status literal anywhere — so the pending backfill (#2054) cannot move a
published figure and a later edit cannot quietly reinstate a row count.

Pure source/AST + stub-DB asserts. No DB, no network, no `import main`.
Nothing here runs at module scope.
"""
import ast
import os
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_INDEX = "routes/ai_capacity_index.py"
_PREVIEW = "routes/market_intel_preview.py"


def _read(rel_path):
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _tree(rel_path):
    tree = ast.parse(_read(rel_path))
    # Guard the guard: a degenerate parse would make every search below pass
    # vacuously (2026-07-28 lesson — assert it parsed, never just filter).
    assert isinstance(tree, ast.Module) and len(tree.body) > 5, (
        f"{rel_path} parsed to a degenerate module — this harness is not "
        "looking at the real file")
    return tree


def _fn(tree, name, rel_path):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"{rel_path} no longer defines {name}() — these depth "
                         "guards need updating, not deleting")


def _code_strings(node):
    """Every string constant in `node` EXCEPT its docstring.

    Scanning CODE only is the point. Both files document the removed predicate
    BY NAME in their docstrings, so including prose would let a comment satisfy
    the guard — and, the direction that actually bites, would let a real
    regression hide behind a mention of the old literal. Same lesson as the
    radar guard in tests/test_radar_freshness.py and the source scan in
    backfill_facility_status_canon.py.
    """
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    assert body, f"{node.name} has no body beyond its docstring"
    # An f-string must be reconstructed ATOMICALLY and in source order: the
    # SQL here is an f-string, and ast.walk would emit its interpolated names
    # and its literal chunks in tree order rather than text order, scrambling
    # any positional claim (e.g. "ORDER BY precedes LIMIT") into nonsense.
    out, consumed = [], set()

    def render_fstring(node):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                consumed.add(id(v))
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                # Keep the fragment's NAME in place of its value so the query
                # stays greppable without evaluating anything.
                inner = v.value
                parts.append(inner.id if isinstance(inner, ast.Name)
                             else "<expr>")
                for c in ast.walk(inner):
                    consumed.add(id(c))
        return "".join(parts)

    for stmt in body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.JoinedStr):
                out.append(render_fstring(n))
            elif (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in consumed):
                out.append(n.value)
    return " ".join(out)


def _module_consts(rel_path):
    """Exec the module's _UPPER_SNAKE constants + its util imports.

    Skips `x_bp = Blueprint(...)`, which would need flask — the house rule is
    that this suite never imports the web stack.
    """
    tree = _tree(rel_path)

    def is_const(n):
        return (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id.lstrip("_").replace("_", "").isupper())

    pre = [n for n in tree.body
           if is_const(n) or (isinstance(n, ast.ImportFrom)
                              and (n.module or "").startswith("util"))]
    assert pre, f"{rel_path} defines no module constants — fragments moved?"
    g = {}
    import sys
    sys.path.insert(0, ROOT)
    try:
        exec(compile(ast.Module(body=pre, type_ignores=[]),
                     f"<{rel_path}:consts>", "exec"), g)
    finally:
        sys.path.remove(ROOT)
    return g


# ── 1. no status literal may come back to either query ───────────────────────

def test_no_status_literal_in_either_depth_query():
    """status says which ingest path wrote the row, not whether it has
    capacity. Any literal here re-arms the drift the 07-31 fix removed: the
    figure moves whenever a backfill or a writer changes vocabulary."""
    for rel_path, func in ((_INDEX, "_compute_index"), (_PREVIEW, "preview")):
        sql = _code_strings(_fn(_tree(rel_path), func, rel_path))
        assert "discovered_facilities" in sql, (
            f"{rel_path}:{func} no longer queries discovered_facilities — this "
            "guard is pointed at the wrong function")
        for literal in ("'active'", "'operational'", "'Operational'",
                        "<> 'active'", "status,''"):
            assert literal not in sql, (
                f"{rel_path}:{func} is keying on the status literal {literal} "
                "again. Lifecycle belongs to util/status_taxonomy, which "
                "normalises 'active' and 'Operational' onto the same bucket — "
                "that is what makes these figures survive the #2054 backfill")


def test_lifecycle_comes_from_the_shared_taxonomy():
    """Not a hand-copied string list — the bug class util/status_taxonomy and
    util/iso_taxonomy both exist to kill."""
    for rel_path in (_INDEX, _PREVIEW):
        src = _read(rel_path)
        assert "from util.status_taxonomy import" in src, (
            f"{rel_path} no longer sources its lifecycle vocabulary from the "
            "shared taxonomy")
        g = _module_consts(rel_path)
        assert "_OPERATIONAL" in g and "operational" in g["_OPERATIONAL"], g.keys()
        # The fragment must be the case-insensitive one, or the backfill moves it.
        assert "LOWER(TRIM(COALESCE(status,'')))" in g["_OPERATIONAL"], (
            f"{rel_path}: the operational predicate is no longer case-folded — "
            "'active' and 'Operational' would stop matching the same rows and "
            "the backfill would move every count")


def test_backfill_cannot_move_these_figures_by_construction():
    """The invariance claim, proven structurally rather than only measured.

    Both spellings of the legacy cohort — 'active' today and the 'Operational'
    the backfill rewrites it to — must classify into the SAME bucket. If they
    ever diverge, every count in both files moves on backfill day.
    """
    import sys
    sys.path.insert(0, ROOT)
    try:
        from util.status_taxonomy import classify, OPERATIONAL
    finally:
        sys.path.remove(ROOT)
    assert classify("active") == classify("Operational") == OPERATIONAL, (
        "'active' and 'Operational' no longer share a bucket — the #2054 "
        "backfill would silently reorder the published AI-capacity ranking")
    assert classify("operational") == OPERATIONAL


# ── 2. what the count is over ────────────────────────────────────────────────

def test_depth_counts_distinct_fleet_facilities_not_rows():
    """Three separate defects, each pinned.

    * no fleet filter at all -> 819 duplicate rows counted as market depth;
    * COUNT(*) over a table where is_duplicate=0 still admits 727 exact-name
      collisions -> rows counted as buildings;
    * COUNT(DISTINCT provider) treats '' as an operator -> 20 markets cleared
      the ">= 2 operators" gate on a blank string.
    """
    for rel_path in (_INDEX, _PREVIEW):
        g = _module_consts(rel_path)
        # Semantics, not formatting — #2058 writes `COALESCE(is_duplicate, 0)`
        # with a space and radar writes it without; pinning either spelling
        # would make this guard fail on a cosmetic edit.
        fleet = "".join(g.get("_FLEET", "").split())
        assert fleet == "COALESCE(is_duplicate,0)=0", (
            f"{rel_path}: the #1539 fleet filter is gone (got {g.get('_FLEET')!r}). "
            "discovered_facilities aggregates must exclude is_duplicate rows — "
            "see routes/hyperscaler_brief.py, routes/radar.py and #2058")
        assert g.get("_FACILITIES") == "COUNT(DISTINCT LOWER(TRIM(name)))", (
            f"{rel_path}: depth is back to counting ROWS. is_duplicate=0 does "
            "not mean distinct — bulk ingest leaves exact-name collisions")
        assert "NULLIF(TRIM(provider),'')" in g.get("_OPERATORS", ""), (
            f"{rel_path}: '' is being counted as an operator again")


def test_index_publishes_the_metered_share_rather_than_filtering_on_power():
    """The deliberate divergence from radar's fix (#2052).

    Power is populated on roughly a third of rows, so for a COUNT-based index
    filtering on power_mw would answer a disclosure question with a depth
    field and drop two-thirds of the map. The honest move is to publish the
    share — so metered_facility_count must exist, and facility_count must NOT
    be gated on power.
    """
    src = _read(_INDEX)
    assert '"metered_facility_count"' in src, (
        "the metered share is no longer published — facility_count would then "
        "read as a capacity figure when roughly two-thirds of it is unmetered")
    g = _module_consts(_INDEX)
    assert "power_mw" not in g.get("_OP_FACILITIES", ""), (
        "facility_count is now gated on power_mw. That is radar's fix, not "
        "this one: this index counts market depth, and its own comment says "
        "power data is too sparse to filter a count on")


def test_installed_mw_excludes_pipeline():
    """total_installed_mw must not sum announced capacity.

    Before the fix it summed every non-'active' row: 68,900 of the 136,589 MW
    it published on 2026-07-31 was Announced / Planned / Under Construction —
    a total wearing the word "installed". Same bug the DCPI saturation index
    carried (r-status-taxonomy).
    """
    g = _module_consts(_INDEX)
    assert "_PIPELINE" in g and "announced" in g["_PIPELINE"], (
        "the pipeline bucket is gone from ai_capacity_index — pipeline MW has "
        "nowhere to go but back into total_installed_mw")
    sql = _code_strings(_fn(_tree(_INDEX), "_compute_index", _INDEX))
    assert "_PIPELINE" in sql and "_OPERATIONAL" in sql, (
        "the query no longer splits operational from pipeline MW")
    assert '"pipeline_mw"' in _read(_INDEX), (
        "pipeline_mw is no longer published, so the split is invisible to the "
        "reader even if the SQL still does it")


# ── 3. behavioural: the field mapping, and the preview's determinism ─────────

def _stub_run(rel_path, func, rows, extra=None):
    """Execute the real function against a stub cursor. A SELECT re-order is
    invisible to a source grep, so the mapping is asserted by RUNNING it."""
    captured = []

    class _Cur:
        def execute(self, sql, args=None):
            captured.append(sql)

        def fetchall(self):
            return list(rows)

        def fetchone(self):
            return rows[0] if rows else None

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class _Conn:
        def cursor(self, **kw):
            return _Cur()

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    from contextlib import contextmanager

    @contextmanager
    def _conn():
        yield _Conn()

    import datetime as _dt
    g = _module_consts(rel_path)
    g.update({"_conn": _conn, "_dsn": lambda: "stub://", "_pg": object(),
              "psycopg2": types.SimpleNamespace(
                  extras=types.SimpleNamespace(RealDictCursor=None)),
              "datetime": _dt, "os": os})
    g.update(extra or {})
    node = _fn(_tree(rel_path), func, rel_path)
    exec(compile(ast.Module(body=[node], type_ignores=[]),
                 f"<{rel_path}:{func}>", "exec"), g)
    return g[func], captured


def test_index_maps_each_count_to_its_own_published_field():
    """facility_count / metered_facility_count / tracked_count are three
    different populations. A silent SELECT re-order would republish the widest
    one as facility_count and re-introduce exactly the inflation this fix
    removed, with every source-level guard above still green."""
    row = {"slug": "ashburn-va", "city": "Ashburn", "state": "VA",
           "country": "US", "facility_count": 179,
           "metered_facility_count": 130, "tracked_count": 187,
           "total_mw": 6304.0, "pipeline_mw": 2500.0, "operator_count": 54,
           "max_facility_mw": 120.0}
    fn, captured = _stub_run(_INDEX, "_compute_index", [row])
    body, status = fn(90, 20)
    assert status == 200, body
    assert captured, "_compute_index issued no SQL"
    m = body["markets"][0]
    assert m["facility_count"] == 179
    assert m["metered_facility_count"] == 130
    assert m["tracked_count"] == 187
    assert m["total_installed_mw"] == 6304
    assert m["pipeline_mw"] == 2500
    assert m["operator_count"] == 54
    assert m["hyperscale_ready"] is True, "120 MW operational is hyperscale-ready"
    assert body["depth_basis"]["status_literals_in_query"] is False
    assert "taxonomy_version" in body["depth_basis"]["status_basis"]


def test_hyperscale_ready_is_not_earned_by_a_press_release():
    """max_facility_mw is OPERATIONAL-only. Under the old unfiltered MAX,
    markets whose largest facility was announced took the +50 bonus on zero
    operational MW — One TX (5,000 MW), Las Cruces NM (4,500), Mount Pleasant
    WI (2,300) all scored hyperscale_ready with nothing above 50 MW running."""
    row = {"slug": "x", "city": "X", "state": "TX", "country": "US",
           "facility_count": 5, "metered_facility_count": 0, "tracked_count": 5,
           "total_mw": 0.0, "pipeline_mw": 5000.0, "operator_count": 2,
           "max_facility_mw": 0.0}   # 5,000 MW announced, 0 operational
    fn, _ = _stub_run(_INDEX, "_compute_index", [row])
    body, _status = fn(90, 20)
    m = body["markets"][0]
    assert m["hyperscale_ready"] is False, (
        "a market with 5,000 MW announced and 0 MW operational is scoring "
        "hyperscale_ready — max_facility_mw is no longer operational-only")
    assert m["total_installed_mw"] == 0
    assert m["pipeline_mw"] == 5000


def test_preview_picks_its_market_deterministically():
    """City slugs are not unique across states, and this LIMIT 1 had no ORDER
    BY — so the free preview published whichever group the planner returned.

    Live on 2026-07-31 that meant /api/v1/market-intel-preview?market=ashburn
    served `Ashburn, ''` with 3 facilities and 0.0 MW, while the real market
    is ('Ashburn','VA', 184 rows, 7,481 MW). Verified against production, not
    inferred. Four groups match that slug; the ORDER BY is the only thing
    standing between the conversion surface and a junk one.
    """
    # Assert against the SQL the function actually EXECUTES, with `--` comments
    # stripped. Grepping the source would let the prose above satisfy the
    # guard — the comment explaining this fix contains the words "LIMIT 1" and
    # "ORDER BY" itself, which is exactly how a grep-guard rots.
    fn, captured = _stub_run(_PREVIEW, "preview", [], extra={
        "request": types.SimpleNamespace(
            args=types.SimpleNamespace(get=lambda k, d=None: "ashburn")),
        "jsonify": lambda x: x,
        "market_intel_preview_bp": types.SimpleNamespace(
            route=lambda *a, **k: (lambda f: f)),
    })
    fn()
    assert captured, "preview issued no SQL"
    sql = "\n".join(line.split("--")[0] for line in captured[0].splitlines())
    assert "LIMIT 1" in sql, "preview no longer bounds its result"
    order, limit = sql.find("ORDER BY"), sql.find("LIMIT 1")
    assert order != -1, (
        "preview's LIMIT 1 has no ORDER BY again — it will publish an "
        "arbitrary one of the several (city, state) groups a slug matches")
    assert order < limit, "ORDER BY must precede LIMIT"
    assert "GROUP BY city, state" in sql
