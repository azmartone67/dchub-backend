"""HIFLD owner sentinel in fiber_routes.PROVIDER (2026-08-16).

#2726 scrubbed the sentinel out of `name` and deliberately scoped itself to
display names. The sibling column was left dirty: 1,150 rows hold the literal
'NOT AVAILABLE' in `provider`, which is served publicly as `carrier` on
/api/v1/fiber/routes AND is filterable —

    GET /api/v1/fiber/routes?carrier=NOT%20AVAILABLE   ->   total: 1150

so unlike the name half this one is a wrong ANSWER, not just a wrong label.

★WHAT THESE TESTS ARE FOR. The repair is one line of behaviour and three lines
of risk, so they pin the risks:

  1. DELEGATION. The sentinel family must be read from
     infrastructure_discovery.hifld_owner, never re-listed here — a second copy
     drifts silently the day a sentinel is added to one of them.
     test_the_family_is_delegated_not_reimplemented proves it by feeding a
     sentinel that exists ONLY in a stub module; a hardcoded list fails it.
  2. SCOPE. A blank provider is not a sentinel. Mapping '' -> 'Unknown' would
     invent data for rows this backfill was never about.
  3. UNIQUE(name, provider) is live on fiber_routes (twice). Collapsing two
     spellings into one can collide, and two CANDIDATES can collide with each
     other in a way the scan's SQL snapshot cannot see.

Per repo convention, tests never import main.py; the functions under test are
pulled out of the source with ast and executed against stubs.
"""
import ast
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "routes" / "fiber_name_quality.py"


def _load(names):
    """Execute only the named module-level defs/assigns from the repair module."""
    tree = ast.parse(SRC.read_text())
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


@pytest.fixture()
def repair():
    ns = _load({"repair_provider", "_hifld_owner", "_unknown_owner"})
    return ns["repair_provider"]


# ── the repair itself ────────────────────────────────────────────────────────

def test_the_exact_live_row_shape_is_repaired():
    """1,150 rows, all source='hifld', all this literal (measured 2026-08-16)."""
    assert _load({"repair_provider", "_hifld_owner", "_unknown_owner"})[
        "repair_provider"]("NOT AVAILABLE") == "Unknown"


def test_sentinel_is_caught_case_insensitively(repair):
    # The live data carries it in caps; a case-sensitive check would miss it.
    for bad in ("NOT AVAILABLE", "not available", "Not Available", "  N/A  ",
                "no data", "-999999", "NULL"):
        assert repair(bad) == "Unknown", bad


def test_real_carriers_come_back_byte_identical(repair):
    for good in ("Zayo", "Lumen", "Dominion", "Cogent", "AT&T",
                 "Unknown"):          # already repaired -> no second write
        assert repair(good) == good


def test_a_real_carrier_is_not_whitespace_trimmed(repair):
    """hifld_owner strips; this backfill does not. Only a mapping to the
    fallback counts as a repair, so a padded REAL name is left alone rather
    than quietly rewritten by a lane that was authorised to fix sentinels."""
    assert repair("  Zayo  ") == "  Zayo  "


def test_blank_and_null_are_never_given_an_invented_value(repair):
    """★SCOPE. hifld_owner('') returns the fallback — correct at INGEST, where
    something must be written. Here it would fabricate a carrier for a row that
    was never part of this defect."""
    for blank in (None, "", "   "):
        assert repair(blank) is blank or repair(blank) == blank
        assert repair(blank) != "Unknown"


def test_repair_is_idempotent(repair):
    once = repair("NOT AVAILABLE")
    assert repair(once) == once


# ── ★ the delegation guard ───────────────────────────────────────────────────

def test_the_family_is_delegated_not_reimplemented(repair, monkeypatch):
    """Feed a sentinel that exists ONLY in a stub infrastructure_discovery.

    If repair_provider carries its own copy of the string list, 'ACME-SENTINEL'
    is unknown to it and comes back unchanged — this fails. It can only pass by
    actually calling through to the ingest's definition.
    """
    stub = types.ModuleType("infrastructure_discovery")
    stub._HIFLD_NULL_STRINGS = frozenset({"", "acme-sentinel"})

    def hifld_owner(owner, operator=None):
        for cand in (owner, operator):
            t = str(cand or "").strip()
            if t and t.lower() not in stub._HIFLD_NULL_STRINGS:
                return t
        return "NO PARTY NAMED"      # a fallback the real module never returns

    stub.hifld_owner = hifld_owner
    monkeypatch.setitem(sys.modules, "infrastructure_discovery", stub)

    assert repair("ACME-SENTINEL") == "NO PARTY NAMED"
    # ...and the real module's sentinel is NOT special-cased locally: under this
    # stub 'NOT AVAILABLE' is an ordinary party name and must survive.
    assert repair("NOT AVAILABLE") == "NOT AVAILABLE"


def test_every_upstream_sentinel_maps_to_the_upstream_fallback(repair):
    """Whatever the ingest considers a sentinel today, the backfill repairs."""
    from infrastructure_discovery import _HIFLD_NULL_STRINGS, hifld_owner
    for s in _HIFLD_NULL_STRINGS:
        if s and s.strip():
            assert repair(s) == hifld_owner(""), s


def test_the_sql_prefilter_reads_the_shared_set_and_drops_blanks():
    from infrastructure_discovery import _HIFLD_NULL_STRINGS
    vals = _load({"_sentinel_values"})["_sentinel_values"]()
    assert set(vals) == {s for s in _HIFLD_NULL_STRINGS if s and s.strip()}
    assert "" not in vals
    assert vals == sorted(vals)          # stable param for the = ANY(%s)


# ── ★ UNIQUE(name, provider) ────────────────────────────────────────────────

def _defer():
    return _load({"_defer_intra_set_collisions"})["_defer_intra_set_collisions"]


def test_two_candidates_repairing_to_the_same_key_do_not_both_get_written():
    """The scan's `EXISTS` reads the pre-UPDATE snapshot, so it sees a row that
    ALREADY holds 'Unknown' but never a sibling candidate about to become it.
    Two spellings on one name ('NOT AVAILABLE' + 'N/A') are exactly that case:
    both are legal under UNIQUE(name, provider) today, and both repair to one
    key. Writing both violates the constraint and fails the whole chunk."""
    keep, deferred = _defer()([
        {"id": 1, "name": "Unknown 500kV Line - Ashburn", "from": "NOT AVAILABLE", "to": "Unknown"},
        {"id": 2, "name": "Unknown 500kV Line - Ashburn", "from": "N/A", "to": "Unknown"},
        {"id": 3, "name": "Unknown 230kV Line - Dallas", "from": "NOT AVAILABLE", "to": "Unknown"},
    ], [], "name")
    assert [r["id"] for r in keep] == [1, 3]      # lowest id of the group wins
    assert [r["id"] for r in deferred] == [2]


def test_distinct_names_are_never_deferred():
    keep, deferred = _defer()(
        [{"id": i, "name": "Line %d" % i, "to": "Unknown"} for i in range(5)], [], "name")
    assert len(keep) == 5 and deferred == []


def test_sql_detected_collisions_are_carried_through_not_dropped():
    """A deferred row must stay COUNTABLE — 'skipped 3' is a report, silently
    losing 3 is the bug this repo keeps finding."""
    pre = [{"id": 9, "name": "Line A", "to": "Unknown"}]
    keep, deferred = _defer()([{"id": 1, "name": "Line B", "to": "Unknown"}], pre, "name")
    assert keep and [r["id"] for r in deferred] == [9]


# ── ★ what makes undo total ─────────────────────────────────────────────────

def _fn_source(name):
    tree = ast.parse(SRC.read_text())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, "%s not found in routes/fiber_name_quality.py" % name
    return ast.get_source_segment(SRC.read_text(), fn)


def test_apply_writes_the_undo_slot_under_coalesce():
    """★The property the name lane documents and this lane inherits: re-running
    apply must never launder a REPAIRED provider into the "original" slot. A
    bare `provider_orig = provider` on a second run would do exactly that and
    make undo restore the repair instead of the original."""
    src = _fn_source("fiber_providers_apply")
    assert "COALESCE(f.provider_orig, f.provider)" in src


def test_apply_is_read_only_without_confirm():
    src = _fn_source("fiber_providers_apply")
    i_guard = src.index('request.args.get("confirm")')
    i_write = src.index("UPDATE fiber_routes")
    assert i_guard < i_write, "the confirm gate must precede the write"
    assert 'dry_run=True' in src


def test_undo_restores_from_the_provider_slot_and_clears_it():
    src = _fn_source("fiber_providers_undo")
    assert "provider = provider_orig" in src
    assert "provider_orig = NULL" in src
    assert "WHERE provider_orig IS NOT NULL" in src


def test_both_undo_columns_are_created_together():
    src = _fn_source("_ensure_columns")
    for col in ("name_orig", "name_fixed_at", "provider_orig", "provider_fixed_at"):
        assert "ADD COLUMN IF NOT EXISTS %s" % col in src, col


def test_the_three_provider_routes_exist_and_are_admin_gated():
    tree = ast.parse(SRC.read_text())
    routes = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name.startswith("fiber_providers_"):
            paths = [d.args[0].value for d in n.decorator_list
                     if isinstance(d, ast.Call) and d.args
                     and isinstance(d.args[0], ast.Constant)]
            routes[n.name] = paths
            assert "_admin_ok" in ast.dump(n), "%s is not admin-gated" % n.name
    assert set(routes) == {"fiber_providers_analyze", "fiber_providers_apply",
                           "fiber_providers_undo"}
    assert routes["fiber_providers_analyze"] == ["/api/v1/admin/fiber-providers/analyze"]
