"""The name lane's chunked apply + its missing UNIQUE(name, provider) guard
(2026-08-16).

#2726 shipped the name lane as a per-row UPDATE loop: 53.9s for 1,674 rows.
/api/v1/admin/fiber-names/ has no ROUTE_TIMEOUTS entry in worker.js, so it fell
to the Cloudflare worker's 15s DEFAULT and came back as a 200-wrapped 503
envelope — the lane only worked if whoever ran it remembered to aim at the
Railway-direct origin. #2736 gave the provider lane one chunked set-based
UPDATE per 500 ids (1,150 rows in 4.9s); this brings the name lane to the same
form so the footnote is not load-bearing.

★THE PART THAT IS NOT A PERFORMANCE CHANGE. `name` is half of the live
UNIQUE(name, provider) — carried TWICE, by fiber_routes_name_provider_key and
fiber_routes_name_provider_unique. The provider lane guards that key from both
sides; the name lane guarded it from NEITHER. That was not a latent nicety:
verified 2026-08-16 on a `CREATE TEMP TABLE (LIKE public.fiber_routes INCLUDING
ALL)` mirror, the per-row loop wrote 2 rows and then raised UniqueViolation,
and because `_conn()` sets autocommit those 2 rows stayed written — a 500
`apply_failed` on top of a half-applied repair.

repair_name is many-to-one in three independent ways, so two rows that are
legal TODAY under one provider can repair onto one key:

    'NOT AVAILABLE -999999kV X' \\
                                 >-- both -> 'Unknown X'   (owner spellings)
    'UNKNOWN -999999kV X'       /
    '... -999998kV ...' / '... -999999kV ...'               (voltage spellings)
    'A -  B' / 'A - B'                                      (whitespace squeeze)

and the target can equally be held by a row that is already clean. These tests
pin both halves of the guard and the shape of the write. Per repo convention
they never import main.py; the functions are pulled out of the source with ast.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "fiber_name_quality.py"


def _source():
    return SRC.read_text()


def _load(names):
    """Execute only the named module-level defs/assigns from the repair module."""
    tree = ast.parse(_source())
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            keep.append(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    keep.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            keep.append(node)
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<extract>", "exec"), ns)
    return ns


def _fn(name):
    tree = ast.parse(_source())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, "%s not found in routes/fiber_name_quality.py" % name
    return fn


def _fn_source(name):
    return ast.get_source_segment(_source(), _fn(name))


@pytest.fixture()
def repair():
    return _load({"repair_name", "_VOLT_RE", "_OWNER_RE"})["repair_name"]


@pytest.fixture()
def defer():
    return _load({"_defer_intra_set_collisions"})["_defer_intra_set_collisions"]


# ── ★ the collision is reachable: repair_name is many-to-one ────────────────
# Pure proof that the guard has something to guard. Each pair below is two rows
# that can coexist under UNIQUE(name, provider) today and cannot after repair.

def test_two_owner_spellings_collapse_to_one_name(repair):
    a = repair("NOT AVAILABLE -999999kV Line - Columbus [c1]")
    b = repair("UNKNOWN -999999kV Line - Columbus [c1]")
    assert a == b == "Unknown Line - Columbus [c1]"


def test_the_two_voltage_spellings_collapse_to_one_name(repair):
    """_VOLT_RE is -99999[89]kV, so both sentinels land on the same string."""
    a = repair("NOT AVAILABLE -999999kV Line - Dallas [d1]")
    b = repair("NOT AVAILABLE -999998kV Line - Dallas [d1]")
    assert a == b == "Unknown Line - Dallas [d1]"


def test_internal_whitespace_collapses_to_one_name(repair):
    a = repair("NOT AVAILABLE -999999kV Line -  Reno [r1]")
    b = repair("NOT AVAILABLE -999999kV Line - Reno [r1]")
    assert a == b == "Unknown Line - Reno [r1]"


def test_a_repair_can_land_on_a_row_that_is_already_clean(repair):
    """The other half: nothing about the target says 'was repaired'. A clean row
    can already hold it, which is what the SQL NOT EXISTS is for."""
    assert repair("NOT AVAILABLE -999999kV Line - Reston [x1]") == \
        "Unknown Line - Reston [x1]"


# ── ★ intra-set deferral, keyed on the half the lane does NOT change ────────

def test_candidates_colliding_with_each_other_are_deferred_not_both_written(defer):
    """The scan's EXISTS reads the pre-UPDATE snapshot: it sees a row that
    ALREADY holds the target but never a sibling about to take it. Writing both
    violates the constraint and — measured on the mirror — fails mid-chunk."""
    keep, deferred = defer([
        {"id": 1, "provider": "Lumen", "to": "Unknown Line - Columbus [c1]"},
        {"id": 2, "provider": "Lumen", "to": "Unknown Line - Columbus [c1]"},
        {"id": 3, "provider": "Lumen", "to": "Unknown Line - Dallas [d1]"},
    ], [], "provider")
    assert [r["id"] for r in keep] == [1, 3]      # lowest id of the group wins
    assert [r["id"] for r in deferred] == [2]


def test_the_same_target_under_a_different_provider_is_not_a_collision(defer):
    """★The key is the PAIR. Deferring on the repaired name alone would refuse
    a repair the constraint has no objection to."""
    keep, deferred = defer([
        {"id": 1, "provider": "Zayo", "to": "Unknown Line - Ashburn [a1]"},
        {"id": 2, "provider": "Crown", "to": "Unknown Line - Ashburn [a1]"},
    ], [], "provider")
    assert [r["id"] for r in keep] == [1, 2]
    assert deferred == []


def test_sql_detected_collisions_are_carried_through_not_dropped(defer):
    """A deferred row must stay COUNTABLE — 'skipped 3' is a report, silently
    losing 3 is the bug this repo keeps finding."""
    pre = [{"id": 9, "provider": "AT&T", "to": "Unknown Line - Reston [x1]"}]
    keep, deferred = defer(
        [{"id": 1, "provider": "Zayo", "to": "Unknown A"}], pre, "provider")
    assert [r["id"] for r in keep] == [1]
    assert [r["id"] for r in deferred] == [9]


def test_each_lane_groups_on_the_half_it_holds_fixed():
    """★The one argument that is different between the two lanes, and the one
    that is silently wrong rather than loud: passing 'name' here would group the
    name lane on a column it is REWRITING, so every candidate looks unique and
    nothing is ever deferred."""
    held = {}
    for lane, fn in (("provider", "_scan"), ("name", "_scan_providers")):
        calls = [n for n in ast.walk(_fn(fn))
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "_defer_intra_set_collisions"]
        assert len(calls) == 1, "%s must defer intra-set collisions exactly once" % fn
        assert len(calls[0].args) == 3, "%s must name the held column" % fn
        assert isinstance(calls[0].args[2], ast.Constant)
        held[fn] = calls[0].args[2].value
    assert held == {"_scan": "provider", "_scan_providers": "name"}


# ── ★ one predicate, two callers — the drift this file exists to prevent ────

def test_the_scan_and_the_write_share_one_collision_predicate():
    """If the report and the skip are two copies of the predicate, they drift and
    analyze starts lying about what apply will do."""
    assert "_NAME_KEY_TAKEN" in _fn_source("_scan")
    assert "_NAME_KEY_TAKEN" in _fn_source("fiber_names_apply")
    ns = _load({"_NAME_KEY_TAKEN", "_NAME_TARGETS_SQL"})
    pred = ns["_NAME_KEY_TAKEN"]
    assert pred.startswith("EXISTS (")
    assert "g.id <> f.id" in pred
    assert "g.name = v.new_name" in pred
    # ★the HELD half compared as a value: `= NULL` is never true, so a plain `=`
    # would silently call every NULL-provider key free.
    assert "g.provider IS NOT DISTINCT FROM f.provider" in pred


def test_the_scan_reports_collisions_instead_of_hiding_them():
    """★Counted, not merely present. apply has TWO responses — the dry run and
    the confirmed write — and a substring check is satisfied by either, so
    dropping it from the branch that actually wrote rows reads as green."""
    for fn, want in (("fiber_names_analyze", 1), ("fiber_names_apply", 2)):
        src = _fn_source(fn)
        assert "rows, collisions = scanned" in src, fn
        assert src.count("collisions=len(collisions)") == want, fn
        # every jsonify that reports a count must report the collision count too
        for n in ast.walk(_fn(fn)):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "jsonify":
                kw = {k.arg for k in n.keywords}
                if kw & {"fixable", "would_fix", "fixed"}:
                    assert "collisions" in kw, "%s: %s omits collisions" % (fn, kw)


def test_apply_reports_what_it_did_not_write():
    src = _fn_source("fiber_names_apply")
    assert "skipped=len(rows) - fixed" in src


# ── ★ the shape of the write ────────────────────────────────────────────────

def test_apply_is_chunked_not_per_row():
    """The regression this PR is named for. A `for r in rows:` around an execute
    is 1,674 round trips and 53.9s; the edge default is 15s."""
    fn = _fn("fiber_names_apply")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert len(loops) == 1, "apply should have exactly one loop — over chunks"
    call = loops[0].iter
    assert isinstance(call, ast.Call) and getattr(call.func, "id", "") == "range", \
        "apply must iterate chunk offsets, not rows"
    assert ast.unparse(call) == "range(0, len(rows), _APPLY_CHUNK)"
    execs = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "execute"]
    assert len(execs) == 1, "one statement per chunk, not one per row"


def test_both_lanes_share_one_chunk_size():
    ns = _load({"_APPLY_CHUNK"})
    assert ns["_APPLY_CHUNK"] == 500
    for fn in ("fiber_names_apply", "fiber_providers_apply"):
        assert "_APPLY_CHUNK" in _fn_source(fn), fn


def test_the_per_row_target_is_passed_as_parallel_arrays():
    """★Unlike the provider lane, `to` differs per row, so the target cannot be
    one scalar parameter. int[] — fiber_routes.id is integer, and a bigint[]
    unnest would force a cast that drops the pkey index."""
    ns = _load({"_NAME_TARGETS_SQL"})
    assert ns["_NAME_TARGETS_SQL"] == "unnest(%s::int[], %s::text[]) AS v(id, new_name)"
    src = _fn_source("fiber_names_apply")
    assert "_NAME_TARGETS_SQL" in src
    assert '[r["id"] for r in chunk], [r["to"] for r in chunk]' in src


def test_apply_skips_a_collision_atomically_rather_than_raising():
    """`NOT EXISTS` inside the UPDATE, not a try/except around it: a row that
    became conflicting between the scan and the write must fall out with
    rowcount 0 and leave the rest of the chunk written."""
    src = _fn_source("fiber_names_apply")
    assert "AND NOT " in src and "_NAME_KEY_TAKEN" in src
    i_where = src.index("WHERE f.id = v.id")
    i_guard = src.index("_NAME_KEY_TAKEN", i_where)
    assert i_guard > i_where


# ── ★ what makes undo total — unchanged by the rewrite ──────────────────────

def test_apply_writes_the_undo_slot_under_coalesce():
    """Re-running apply must never launder a REPAIRED name into the "original"
    slot. A bare `name_orig = name` on a second run would make undo restore the
    repair instead of the original."""
    assert "COALESCE(f.name_orig, f.name)" in _fn_source("fiber_names_apply")


def test_apply_is_read_only_without_confirm():
    src = _fn_source("fiber_names_apply")
    i_guard = src.index('request.args.get("confirm")')
    i_write = src.index("UPDATE fiber_routes")
    assert i_guard < i_write, "the confirm gate must precede the write"
    assert "dry_run=True" in src


def test_undo_restores_from_the_name_slot_and_clears_it():
    src = _fn_source("fiber_names_undo")
    assert "name = name_orig" in src
    assert "name_orig = NULL" in src
    assert "WHERE name_orig IS NOT NULL" in src


def test_the_three_name_routes_exist_and_are_admin_gated():
    tree = ast.parse(_source())
    routes = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name.startswith("fiber_names_"):
            routes[n.name] = [d.args[0].value for d in n.decorator_list
                              if isinstance(d, ast.Call) and d.args
                              and isinstance(d.args[0], ast.Constant)]
            assert "_admin_ok" in ast.dump(n), "%s is not admin-gated" % n.name
    assert set(routes) == {"fiber_names_analyze", "fiber_names_apply",
                           "fiber_names_undo"}
    assert routes["fiber_names_analyze"] == ["/api/v1/admin/fiber-names/analyze"]
