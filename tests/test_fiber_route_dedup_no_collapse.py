"""Guard SH52-054: _save_route must not collapse distinct physical route segments.

WHY
───
fiber_routes carries a live UNIQUE(name, provider) (plus UNIQUE(source_id) and
UNIQUE(source, source_id)). The discovery path synthesizes `name` from
owner/voltage/market (e.g. "Dominion 500kV Line - Northern Virginia"), so many
DISTINCT physical segments shared one (name, provider) key; ON CONFLICT DO
NOTHING then discarded all but the first, capping terrestrial discovery at ~154
rows against 55k of bulk carrier data (measured live on the Neon DB 2026-08-10:
hifld 109 / discovery 28 / osm 16 / auto 1). Even the source_id did not save it —
the HIFLD caller sets source_id="hifld_tl_{id}" and `id` is frequently empty, so
those rows collapse on source_id too.

CONTRACT
────────
  C1. Two segments sharing name+provider but with different GEOMETRY get a
      DISTINCT stored `name` AND a DISTINCT `source_id`, so neither UNIQUE
      constraint discards the second.
  C2. The same collapse cannot happen through an empty/duplicated source_id
      (the real HIFLD failure mode).
  C3. The SAME physical segment twice yields IDENTICAL keys — re-runs dedup, so
      the fix un-caps growth without a duplicate explosion.
  C4. The write is still a single INSERT ... ON CONFLICT DO NOTHING (no schema
      change, no second statement).

This guard fails against the pre-fix code: it stored route['name'] verbatim, so
C1/C2's two segments produced identical (name, provider) and the asserts trip.

Run: python3 -m pytest tests/test_fiber_route_dedup_no_collapse.py -v
"""
import ast
import hashlib
import os
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "infrastructure_discovery.py")

# Positions in the INSERT param tuple (see the VALUES column order in _save_route).
_NAME_I, _SID_I = 0, 10


def _extract_save_route():
    """AST-extract the _save_route method as a standalone function."""
    tree = ast.parse(open(SRC).read())
    assert isinstance(tree, ast.Module), "parse did not produce a Module"
    assert tree.body, "parsed module body is EMPTY"
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_save_route"), None)
    assert fn is not None, "_save_route not found in infrastructure_discovery.py"
    assert fn.body, "_save_route parsed with an EMPTY body"
    fn.decorator_list = []
    return fn


def _make_saver():
    fn = _extract_save_route()
    calls = []  # (sql, params) for every _safe_write

    def _safe_write(sql, params):
        calls.append((sql, params))
        return 1

    ns = {
        "_safe_write": _safe_write,
        "hashlib": hashlib,
        "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
    save = ns["_save_route"]
    self = types.SimpleNamespace(new_routes=0)

    def run(route, source="hifld"):
        save(self, route, source)
        assert calls, "no INSERT was issued"
        return calls[-1][1]  # the params tuple of the latest insert

    return run, calls


def _route(name, provider, sl, sg, el, eg, sid=None):
    r = {"name": name, "provider": provider,
         "start_lat": sl, "start_lng": sg, "end_lat": el, "end_lng": eg}
    if sid is not None:
        r["source_id"] = sid
    return r


def test_c1_same_name_different_geometry_gets_distinct_keys():
    run, _ = _make_saver()
    base = "Dominion 500kV Line - Northern Virginia"
    p1 = run(_route(base, "Dominion", 39.0, -77.4, 39.1, -77.5, sid="hifld_tl_1"))
    p2 = run(_route(base, "Dominion", 38.5, -77.9, 38.6, -77.8, sid="hifld_tl_2"))
    assert p1[_NAME_I] != p2[_NAME_I], \
        f"distinct-geometry segments collapsed on name: {p1[_NAME_I]!r}"
    assert p1[_SID_I] != p2[_SID_I], \
        f"distinct-geometry segments collapsed on source_id: {p1[_SID_I]!r}"


def test_c2_empty_or_dup_source_id_distinct_geometry_still_distinct():
    run, _ = _make_saver()
    base = "Unknown 0kV Line - Phoenix"
    # The real HIFLD failure mode: source_id='hifld_tl_' (empty feature id).
    p1 = run(_route(base, "Unknown", 33.4, -112.0, 33.5, -112.1, sid="hifld_tl_"))
    p2 = run(_route(base, "Unknown", 33.9, -111.5, 33.8, -111.6, sid="hifld_tl_"))
    assert p1[_NAME_I] != p2[_NAME_I], "empty-source_id segments collapsed on name"
    assert p1[_SID_I] != p2[_SID_I], "empty-source_id segments collapsed on source_id"


def test_c3_identical_segment_dedups_to_identical_keys():
    run, _ = _make_saver()
    base = "Dominion 500kV Line - Northern Virginia"
    args = ("Dominion", 39.0, -77.4, 39.1, -77.5)
    p1 = run(_route(base, *args, sid="hifld_tl_42"))
    p2 = run(_route(base, *args, sid="hifld_tl_42"))
    assert p1[_NAME_I] == p2[_NAME_I], "same segment produced different names -> would duplicate"
    assert p1[_SID_I] == p2[_SID_I], "same segment produced different source_id -> would duplicate"


def test_c4_single_insert_on_conflict_do_nothing():
    run, calls = _make_saver()
    run(_route("X 1kV Line - Y", "X", 1.0, 2.0, 3.0, 4.0))
    assert len(calls) == 1, f"expected exactly one write, got {len(calls)}"
    sql = " ".join(calls[-1][0].split()).lower()
    assert "insert into fiber_routes" in sql
    assert "on conflict do nothing" in sql


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="control: proves this file actually runs")
def test_zzz_must_fail_control():
    assert False, "control"
