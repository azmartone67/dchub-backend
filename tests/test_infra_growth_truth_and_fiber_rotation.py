"""Guards for the physical-infrastructure loaders + the board that measures them.

THE DEFECTS THESE PIN (all measured live against Neon on 2026-08-07):

  1. WRONG-TABLE x2 on the public /whats-new board.
     - transmission_lines counted `infrastructure_layers WHERE
       category='transmission'`, which returns 0 — that table's categories are
       infrastructure/fiber/power_generation/substation, there is no
       'transmission'. The real table had 95,560 rows, refreshed 2026-08-03.
     - power_plants_eia counted the `power_plants_eia` table: 13,446 rows, NO
       timestamp column at all, reporting_period/source_survey empty on every
       row. The live twin `power_plants` is 100% source='eia-860', 14,480 rows,
       refreshed 2026-07-31.

  2. THE REPOINT WOULD HAVE BEEN A SILENT NO-OP. `_count()` carried a
     hardcoded `if label == "transmission_lines"` branch that ignored the table
     named in _LAYERS. Changing the tuple alone changes nothing — registration
     is not function. Test 3 is the one that actually protects the fix.

  3. A COUNT(*) DELTA CANNOT SEE A FULL-RELOAD LAYER. gas_pipelines rewrote
     30,000 rows on 08-03, gas_compressors all 1,768 on 08-02, gem_power all
     182,428 on 08-01 — every one reported delta_7d = 0, identical to an
     abandoned table. Freshness (max ingestion timestamp) is the only signal
     that separates them, so it must travel with every layer.

  4. TWO IMPORTS IN job_fiber_sync NAMED FUNCTIONS THAT DO NOT EXIST.
     `fiber_network_discovery.sync_fiber_routes` (real name:
     run_fiber_discovery) and `infrastructure_discovery.TransmissionLineDiscovery`
     (never defined). Both raised ImportError on every run and were swallowed,
     while the handler returned {"success": true}. Test 6 is generic: it checks
     every name job_fiber_sync imports actually exists in its target module,
     so the next wrong name fails here instead of silently in production.

  5. FiberRouteDiscovery's market rotation reset on every run. `_market_index`
     is per-INSTANCE and every caller constructs a fresh object, so the window
     was always DC_MARKETS[0:2] — Northern Virginia and Dallas-Fort Worth — and
     the other 18 of 20 markets were never queried. Those two were long since
     ingested, so ON CONFLICT DO NOTHING made every run insert 0 while looking
     healthy. Measured: fiber_routes gained 0 rows in 7d despite 4 runs/day.

CI-SAFETY: the unit-tests job installs ONLY pytest, not requirements.txt.
routes/infra_growth.py imports psycopg2+flask and infrastructure_discovery
imports requests, so NEITHER may be imported here — everything is read with
`ast` and the one executable check runs the extracted function against stubs.
Nothing runs at module scope; nothing imports main.py.

EXPECTED COUNTS
  unpatched (before this change): 8 failed, 1 passed
  patched: 9 passed
"""
import ast
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse(relpath):
    """Parse a repo file to an AST, asserting the parse actually produced nodes.

    ★ An empty parse satisfies every isinstance() filter downstream and makes
    the whole suite vacuously green. Assert the tree is non-trivial FIRST.
    """
    path = os.path.join(_ROOT, relpath)
    assert os.path.exists(path), f"{relpath} missing"
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert len(src) > 500, f"{relpath} suspiciously small ({len(src)}b)"
    tree = ast.parse(src)
    assert len(tree.body) > 3, f"{relpath} parsed to {len(tree.body)} top-level nodes"
    return tree, src


def _module_level_dict(tree, name):
    """Return {key: value_node} for a module-level dict literal assignment."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    assert isinstance(node.value, ast.Dict), f"{name} is not a dict literal"
                    return {k.value: v for k, v in zip(node.value.keys, node.value.values)
                            if isinstance(k, ast.Constant)}
    raise AssertionError(f"{name} not found at module level")


def _layers():
    """[(label, table, category, stale_days)] from _LAYERS, as literals."""
    tree, _ = _parse("routes/infra_growth.py")
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "_LAYERS":
                    assert isinstance(node.value, ast.List)
                    rows = []
                    for elt in node.value.elts:
                        assert isinstance(elt, ast.Tuple), "_LAYERS entry is not a tuple"
                        vals = [e.value if isinstance(e, ast.Constant) else None
                                for e in elt.elts]
                        rows.append(tuple(vals))
                    assert rows, "_LAYERS parsed empty"
                    return rows
    raise AssertionError("_LAYERS not found")


def _func(tree, name, cls=None):
    """Find a FunctionDef by name, optionally inside a ClassDef."""
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


# ── 1. the two wrong-table mappings are gone ───────────────────────────────
def test_transmission_layer_points_at_the_real_table():
    rows = _layers()
    match = [r for r in rows if r[0] == "transmission_lines"]
    assert match, "transmission_lines layer disappeared from _LAYERS"
    label, table = match[0][0], match[0][1]
    assert table != "infrastructure_layers", (
        "transmission_lines is back on infrastructure_layers, which has no "
        "'transmission' category and returns 0 rows")
    assert table == "transmission_lines", f"unexpected transmission source table: {table}"


def test_power_plants_layer_points_at_the_live_twin():
    rows = _layers()
    match = [r for r in rows if r[0] == "power_plants_eia"]
    assert match, "power_plants_eia layer disappeared from _LAYERS"
    table = match[0][1]
    assert table != "power_plants_eia", (
        "power_plants_eia is back on the abandoned twin (13,446 rows, no "
        "timestamp column, empty reporting_period)")
    assert table == "power_plants", f"unexpected power-plant source table: {table}"


# ── 2. the hardcoded branch that made the repoint a no-op ──────────────────
def test_count_has_no_hardcoded_label_branch():
    """_count() must query the table named in _LAYERS, not a baked-in one.

    While the `if label == "transmission_lines"` branch stood, editing the
    tuple was a silent no-op. This is the test that protects the fix.
    """
    tree, _ = _parse("routes/infra_growth.py")
    fn = _func(tree, "_count")
    compares = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    for cmp_node in compares:
        if isinstance(cmp_node.left, ast.Name) and cmp_node.left.id == "label":
            literals = [c.value for c in cmp_node.comparators if isinstance(c, ast.Constant)]
            raise AssertionError(
                f"_count() branches on a hardcoded label {literals!r}; it must "
                f"COUNT(*) the table passed in from _LAYERS")
    # and it must actually interpolate the table argument
    assert any(isinstance(n, ast.JoinedStr) for n in ast.walk(fn)), \
        "_count() no longer builds its query from the `tbl` argument"


# ── 3. freshness travels with every layer ──────────────────────────────────
def test_every_layer_declares_a_freshness_column_or_is_explicitly_unmeasurable():
    tree, _ = _parse("routes/infra_growth.py")
    fresh = _module_level_dict(tree, "_FRESH_COL")
    assert len(fresh) >= 10, f"_FRESH_COL only has {len(fresh)} entries"
    labels = {r[0] for r in _layers()}
    missing = labels - set(fresh)
    # A label may be absent ONLY if the code reports it as unmeasurable rather
    # than inferring freshness from the row count. That path exists (_freshness
    # returns (None, None) -> freshness_measurable False), so absence is
    # allowed, but the dict must cover the layers that DO have a column.
    assert not (missing - {"power_plants_discovered"}), (
        f"layers with no declared freshness column and no unmeasurable "
        f"handling: {sorted(missing)}")


def test_summary_publishes_freshness_fields():
    _, src = _parse("routes/infra_growth.py")
    for field in ("last_ingest_at", "ingest_age_days", "freshness_measurable"):
        assert f'"{field}"' in src, f"/whats-new no longer publishes {field}"
    assert "_freshness(" in src, "the freshness read was removed"


def test_flatline_is_withheld_when_the_table_was_recently_reingested():
    """A full-reload layer has a flat count and a fresh timestamp — not a flatline."""
    tree, _ = _parse("routes/infra_growth.py")
    fn = _func(tree, "_summary")
    src = ast.unparse(fn)
    assert "ingest_age" in src and "flat = False" in src, (
        "_summary no longer suppresses the flatline warning for a layer whose "
        "table was re-ingested inside its staleness window — every full-reload "
        "layer will warn forever")


# ── 4. imports in job_fiber_sync must name things that exist ───────────────
@pytest.mark.parametrize("relpath,funcname,min_imports", [
    ("main.py", "job_fiber_sync", 2),
    # The regex-twin: same three dead imports, registered live at
    # crawler_scheduler.py:112. Both call sites are checked so they cannot
    # drift apart again — fixing one and not the other is how this survived.
    ("crawler_scheduler.py", "_run_infrastructure_sync", 2),
    # ★ The THIRD call site, added 2026-09-02. This is the one the two daily
    # crons actually POST — dchub-jobs.yml 01:00 and daily-infra-sync.yml
    # 04:08 — and it was on none of these fences. It imported
    # `run_permit_scan` from construction_permit_tracker, a name that has
    # never existed there, and reported the resulting ImportError as
    # {'status': 'not_available'} every day.
    #
    # Its floor is 1, not 2: the dead permits import was REMOVED rather than
    # repaired (run_permit_scan has never existed anywhere), so this handler
    # legitimately has one local import. The floor stays per-site so dropping
    # it here cannot silently weaken the other two.
    ("routes/jobs_routes.py", "job_infrastructure_sync", 1),
])
def test_fiber_sync_imports_resolve_to_real_names(relpath, funcname, min_imports):
    """Generic guard: every `from X import Y` in the fiber sync paths finds Y in X.

    This is the check that caught all THREE dead imports — including
    _ensure_peeringdb_fac_coords, which is defined nowhere in the repo. It
    reads the target modules with ast rather than importing them, so it runs
    in a CI job that installs only pytest.
    """
    tree, _ = _parse(relpath)
    fn = _func(tree, funcname)
    imports = [n for n in ast.walk(fn) if isinstance(n, ast.ImportFrom)]
    assert imports, f"{funcname} has no imports — did it get renamed?"

    checked = 0
    for imp in imports:
        rel = imp.module.replace(".", os.sep) + ".py"
        if not os.path.exists(os.path.join(_ROOT, rel)):
            continue                       # third-party / package: not ours to verify
        mod_tree, _src = _parse(rel)
        defined = set()
        for node in ast.walk(mod_tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        defined.add(tgt.id)
        for alias in imp.names:
            checked += 1
            assert alias.name in defined, (
                f"{funcname} imports {alias.name!r} from {imp.module!r}, "
                f"which does not define it — this raises ImportError on every "
                f"run and gets swallowed into a success response")
    assert checked >= min_imports, (
        f"{funcname}: only verified {checked} import(s), floor is {min_imports} — "
        f"this fence must never pass by checking nothing")


@pytest.mark.parametrize("relpath,funcname", [
    ("main.py", "job_fiber_sync"),
    # ★ 2026-09-02: the handler the crons actually call. It hardcoded
    # `'success': True` and could not fail, publishing four green reports a
    # day while fiber_routes grew by two rows in twelve days.
    ("routes/jobs_routes.py", "job_infrastructure_sync"),
])
def test_job_sync_success_is_computed_not_asserted(relpath, funcname):
    """`success` must be COMPUTED, never a literal True.

    ★ AST, not substring (hardened 2026-09-02). This test used to scan
    `ast.unparse(fn)` for the text "'success': True" — which also matches the
    DOCSTRING of a handler that quotes the defect to explain it, so documenting
    the bug made the fence fire on the fixed code. Reading the dict node looks
    at what is returned and nothing else.
    """
    tree, _ = _parse(relpath)
    fn = _func(tree, funcname)

    literal_true = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "success"
                    and isinstance(value, ast.Constant) and value.value is True):
                literal_true.append(node)
    assert not literal_true, (
        f"{funcname} returns a literal success=True; that is how a job reports "
        f"success while its steps raise on every run")

    # Anchored: the handler must actually publish a `success` key, or the check
    # above passes because there is nothing to find.
    # Either shape counts: a dict literal key, or `results['success'] = ...`.
    publishes = [k for node in ast.walk(fn) if isinstance(node, ast.Dict)
                 for k in node.keys
                 if isinstance(k, ast.Constant) and k.value == "success"]
    publishes += [n for n in ast.walk(fn)
                  if isinstance(n, ast.Subscript)
                  and isinstance(n.slice, ast.Constant) and n.slice.value == "success"]
    assert publishes, f"{funcname} no longer publishes a success field at all"
    assert "errors" in ast.unparse(fn), f"{funcname} no longer collects per-step errors"


# ── 5. the market rotation actually rotates ────────────────────────────────
def test_fiber_market_rotation_advances_across_runs():
    """_default_market_index must vary with time, not pin to 0.

    Executed against stubs: the real module imports requests. A constant
    rotation is the whole bug — 18 of 20 markets never queried.
    """
    tree, _ = _parse("infrastructure_discovery.py")
    fn = _func(tree, "_default_market_index", cls="FiberRouteDiscovery")

    import datetime as _dt

    class _Stub:
        MARKETS_PER_RUN = 2

    # Rebuild the method as a plain module-level function (drop @classmethod,
    # keep `cls` as an ordinary first parameter) so it can be called directly.
    fn = ast.parse(ast.unparse(fn)).body[0]
    fn.decorator_list = []
    mod = ast.Module(body=[fn], type_ignores=[])

    seen = set()
    for ordinal_day in range(0, 6):
        for hour in (0, 6, 12, 18):
            fixed = _dt.datetime(2026, 8, 1) + _dt.timedelta(days=ordinal_day, hours=hour)

            class _FixedDT(_dt.datetime):
                @classmethod
                def utcnow(cls_):
                    return fixed

            ns = {"datetime": _FixedDT,
                  "DC_MARKETS": [{"name": f"m{i}"} for i in range(20)]}
            exec(compile(ast.fix_missing_locations(mod), "<rot>", "exec"), ns, ns)
            seen.add(ns[fn.name](_Stub))

    assert len(seen) > 1, (
        f"_default_market_index returned the same window {seen} for every slot "
        f"across 6 days — the rotation is dead and 18 of 20 markets are unreachable")
    assert len(seen) >= 8, (
        f"rotation only reaches {len(seen)} distinct windows; a full sweep of "
        f"20 markets at 2/run needs 10")
    assert max(seen) <= 18 and min(seen) >= 0, f"window out of range: {sorted(seen)}"


def test_fiber_route_discovery_does_not_hard_reset_market_index():
    tree, _ = _parse("infrastructure_discovery.py")
    init = _func(tree, "__init__", cls="FiberRouteDiscovery")
    for node in ast.walk(init):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Attribute) and tgt.attr == "_market_index"
                        and isinstance(node.value, ast.Constant)
                        and node.value.value == 0):
                    raise AssertionError(
                        "FiberRouteDiscovery.__init__ hard-assigns _market_index = 0 "
                        "again — every run resets the window to DC_MARKETS[0:2]")


# ── 6. the loader's own counter is never the ingest proof ──────────────────
@pytest.mark.parametrize("relpath,funcname", [
    ("main.py", "job_fiber_sync"),
    ("routes/jobs_routes.py", "job_infrastructure_sync"),   # ★ 2026-09-02
])
def test_fiber_sync_reports_a_measured_row_delta_not_just_the_counter(relpath, funcname):
    """job_fiber_sync must COUNT(*) fiber_routes before/after and publish it.

    THE DEFECT, measured live 2026-08-07 against the merged #2320 fix: the job
    reported new_routes=301 / total_new=321 and fiber_routes did NOT move —
    55,064 before and after, max(created_at) still 2026-07-25, verified on the
    PRIMARY (not replica lag). The loaders increment from _safe_write()'s
    return value, which disagrees with what persists: fiber_routes has
    UNIQUE(name, provider) and _save_route synthesizes name as
    "{owner} {voltage}kV Line - {market}", so hundreds of distinct physical
    lines collapse onto a few dozen keys. That path has written 36 rows in its
    entire life.

    A loader that reports phantom inserts is worse than a dead one.
    """
    tree, _ = _parse(relpath)
    fn = _func(tree, funcname)

    # ★ AST, not substring (hardened 2026-09-02). This block used to assert
    # `"rows_persisted" in ast.unparse(fn)`. A handler that DOCUMENTS the
    # fields it publishes satisfies that from its docstring alone — verified by
    # mutation: renaming results['rows_persisted'] to results['ROWS_GONE'] left
    # all 15 tests green, because the docstring still said the word. The fence
    # now reads what the handler ASSIGNS.
    # ★ STORE context only. Collecting every Subscript counts READS too, so
    # renaming just the assignment (`results['rows_persisted'] = ...` ->
    # `results['ROWS_GONE'] = ...`) left the name visible via the later
    # `if results['rows_persisted'] == 0` and the mutation survived. Verified.
    assigned = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                and isinstance(node.ctx, ast.Store)):
            assigned.add(node.slice.value)
        elif isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant):
                    assigned.add(k.value)

    # The COUNT may live in a helper the handler calls rather than inline, so
    # this one is allowed to look at the whole module — but with the comments
    # and docstrings stripped, for the same reason as above.
    module_code = ast.unparse(ast.parse(_parse(relpath)[1]))
    module_code = "\n".join(l for l in module_code.splitlines() if "COUNT(*) FROM fiber_routes" in l)
    assert module_code, (
        f"{relpath} no longer measures fiber_routes with a real COUNT(*) — the "
        f"loader's self-reported attempt count was observed overstating "
        f"persisted rows by 321 to 0")

    for field in ("rows_persisted", "fiber_rows_before", "fiber_rows_after",
                  "counter_unreliable"):
        assert field in assigned, (
            f"{funcname} no longer ASSIGNS {field!r}. Naming it in a docstring "
            f"is not publishing it.")
