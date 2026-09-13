"""Guard SH52-054 (round 2): fiber identity must come from the SOURCE, not the crawl.

WHY THIS EXISTS ON TOP OF test_fiber_route_dedup_no_collapse.py
───────────────────────────────────────────────────────────────
That guard proved the 2026-08-10 fingerprint stopped DISCARDING distinct
segments, and it does — the hifld lane went 109 rows -> 1,826 in three days.
It passed while the fix was still wrong, because every case it feeds
_save_route supplies DIFFERENT geometry per segment, and the real caller
does not.

_sync_hifld_transmission_lines queried HIFLD with return_geometry=False and
then filled start_lat/start_lng with market['lat']/market['lng'] — the market
centroid. So the fingerprint's "geometry" is a constant within a market and
differs for the SAME line seen from a neighbouring market. DC_MARKETS are
swept at 50 km and the radii overlap. (That lane was removed 2026-09-13: it
wrote power transmission lines into fiber_routes. G1-G3 and G6 still guard
_save_route for the lanes that remain.)

Measured on the live Neon table 2026-08-12:

    source='hifld':  1,826 rows  /  1,742 distinct upstream HIFLD line ids
                     -> 84 physical lines held twice, climbing daily

That is identity derived from where WE were standing. This file pins identity
to what UPSTREAM says.

CONTRACT
────────
  G1. _route_uid passes an upstream id through and rejects sentinels/None,
      because a fabricated id asserts distinctness that was never established.
  G2. THE MARKET-CENTROID BUG. The same physical line seen from two different
      markets — identical upstream ID, DIFFERENT injected start_lat/start_lng —
      yields the SAME upstream_uid, so the second sighting is discarded by the
      partial unique index. This is the case the previous guard could not see.
  G3. Two genuinely different lines still get different upstream_uid.
  G4. NO PER-PROCESS IDENTITY. _sync_from_learned_apis must not call the
      builtin hash(): CPython salts str hashing per process, so it minted a new
      identity for the same row on every worker restart.
  G5. NO ArcGIS OBJECTID IN IDENTITY. OBJECTID is an export row number; keying
      on it is the fault that holds substations.hifld_objectid hostage
      (SH52-056). Retired 2026-09-13 with the HIFLD lane it guarded.
  G6. upstream_uid is actually written by the INSERT, and the write is still a
      single INSERT ... ON CONFLICT DO NOTHING (bare, so it arbitrates EVERY
      unique index including the new partial one).

Run: python3 -m pytest tests/test_fiber_route_upstream_identity.py -v
"""
import ast
import hashlib
import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "infrastructure_discovery.py")

# Positions in the INSERT param tuple (see the VALUES column order in _save_route).
_NAME_I, _SID_I, _UID_I = 0, 10, 11


def _tree():
    tree = ast.parse(open(SRC).read())
    assert isinstance(tree, ast.Module), "parse did not produce a Module"
    assert tree.body, "parsed module body is EMPTY — an empty parse must not pass"
    return tree


def _func(name):
    fn = next((n for n in ast.walk(_tree())
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"{name} not found in infrastructure_discovery.py"
    assert fn.body, f"{name} parsed with an EMPTY body"
    return fn


def _make_saver():
    """Exec the real _save_route + _route_uid against stubs."""
    save_fn = _func("_save_route")
    uid_fn = _func("_route_uid")
    sentinels = next((n for n in ast.walk(_tree())
                      if isinstance(n, ast.Assign)
                      and any(getattr(t, "id", "") == "_UID_SENTINELS"
                              for t in n.targets)), None)
    assert sentinels is not None, "_UID_SENTINELS not found"

    calls = []

    def _safe_write(sql, params):
        calls.append((sql, params))
        return 1

    ns = {"_safe_write": _safe_write, "hashlib": hashlib, "frozenset": frozenset,
          "logger": types.SimpleNamespace(warning=lambda *a, **k: None)}
    save_fn.decorator_list = []
    uid_fn.decorator_list = []
    exec(compile(ast.Module(body=[sentinels, uid_fn, save_fn], type_ignores=[]),
                 SRC, "exec"), ns)
    save = ns["_save_route"]
    self = types.SimpleNamespace(new_routes=0)

    def run(route, source="hifld"):
        save(self, route, source)
        assert calls, "no INSERT was issued"
        return calls[-1][1]

    return run, calls, ns


def _hifld_route(uid, market_lat, market_lng, owner="VIRGINIA ELECTRIC & POWER CO",
                 voltage=230, market="Northern Virginia"):
    """Exactly the shape _sync_hifld_transmission_lines built (removed 2026-09-13)."""
    return {
        "name": f"{owner} {voltage}kV Line - {market}"[:200],
        "provider": owner[:100],
        "type": "transmission",
        "start": market, "end": market,
        "start_lat": market_lat, "start_lng": market_lng,
        "voltage_kv": voltage,
        "uid": uid,
        "source_id": f"hifld_tl_{uid}",
    }


# ── G1 ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("141463", "141463"),
    (141463, "141463"),
    ("  141463  ", "141463"),
    (None, None), ("", None), ("0", None), ("UNKNOWN", None),
    ("NOT AVAILABLE", None), ("n/a", None), ("None", None),
])
def test_g1_route_uid_passes_real_ids_and_rejects_sentinels(raw, expected):
    _, _, ns = _make_saver()
    assert ns["_route_uid"]({"uid": raw}) == expected


# ── G2 — the bug the previous guard could not see ────────────────────────────
def test_g2_same_line_from_two_markets_shares_one_upstream_uid():
    run, _, _ = _make_saver()
    # HIFLD line 141463, found once while sweeping Northern Virginia and again
    # while sweeping Baltimore. return_geometry=False, so the ONLY coordinates
    # on the route are the two market centroids — different every time.
    p_nova = run(_hifld_route("141463", 39.0, -77.4, market="Northern Virginia"))
    p_balt = run(_hifld_route("141463", 39.29, -76.61, market="Baltimore"))

    assert p_nova[_UID_I] == p_balt[_UID_I] == "141463", (
        "the same physical line got different upstream_uid depending on which "
        f"market we saw it from: {p_nova[_UID_I]!r} vs {p_balt[_UID_I]!r} — "
        "identity is being derived from the crawl, not the asset")


def test_g3_different_lines_keep_different_upstream_uid():
    run, _, _ = _make_saver()
    a = run(_hifld_route("141463", 39.0, -77.4))
    b = run(_hifld_route("141464", 39.0, -77.4))
    assert a[_UID_I] != b[_UID_I], "two distinct HIFLD lines collapsed onto one uid"
    assert a[_NAME_I] != b[_NAME_I], "distinct lines still collapse on (name, provider)"
    assert a[_SID_I] != b[_SID_I], "distinct lines still collapse on source_id"


def test_g2b_unidentified_row_gets_null_not_a_fabricated_uid():
    run, _, _ = _make_saver()
    p = run({"name": "Telecom line near Phoenix", "provider": "Unknown",
             "start_lat": 33.4, "start_lng": -112.0})
    assert p[_UID_I] is None, (
        f"a row upstream could not identify was given uid {p[_UID_I]!r} — NULL "
        "means UNIDENTIFIED and must never be filled in with a fabricated value")


# ── G4 — no per-process identity ─────────────────────────────────────────────
def test_g4_learned_lane_does_not_use_the_salted_builtin_hash():
    fn = _func("_sync_from_learned_apis")
    bad = [n for n in ast.walk(fn)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id == "hash"]
    assert not bad, (
        "_sync_from_learned_apis calls the builtin hash() to mint a source_id. "
        "CPython salts str hashing per process and PYTHONHASHSEED is not set in "
        "this repo, so the same learned row mints a NEW identity on every worker "
        "restart — measured live 2026-08-12 as 12 rows of 'Unknown [<random>]' "
        "added in 3 days, unbounded.")
    used = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "md5" in used, "expected a stable hashlib digest for the learned uid"


def test_g4b_the_hazard_g4_guards_against_is_real():
    """Prove hash() really is per-process salted — a guard whose premise is
    assumed rather than demonstrated is how a vacuous check ships."""
    seen = set()
    for seed in ("1", "2", "3"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run(
            [sys.executable, "-c", "print(hash('Dominion Fiber Route') % 10**8)"],
            capture_output=True, text=True, env=env, timeout=30)
        assert out.returncode == 0, out.stderr
        seen.add(out.stdout.strip())
    assert len(seen) > 1, (
        "hash() returned the same value under different PYTHONHASHSEED — this "
        "guard's premise no longer holds and G4 needs re-deriving, not deleting")


# ── G6 — the write itself ────────────────────────────────────────────────────
def test_g6_upstream_uid_is_written_by_a_single_bare_on_conflict_insert():
    run, calls, _ = _make_saver()
    run(_hifld_route("141463", 39.0, -77.4))
    assert len(calls) == 1, f"expected exactly one write, got {len(calls)}"
    sql = " ".join(calls[-1][0].split()).lower()
    assert "insert into fiber_routes" in sql
    assert "upstream_uid" in sql, "upstream_uid is not in the INSERT column list"
    # BARE on-conflict on purpose: it arbitrates EVERY unique index on the
    # table, so the new partial index catches the cross-market duplicate without
    # naming a conflict target here.
    assert "on conflict do nothing" in sql
    assert "on conflict (" not in sql, (
        "a targeted ON CONFLICT arbitrates ONE index; fiber_routes carries five "
        "unique indexes and a violation of any other still raises")


def test_g6b_migration_ships_with_the_column():
    path = os.path.join(ROOT, "migrations",
                        "2026-08-12_fiber_route_upstream_uid.sql")
    assert os.path.exists(path), "identity migration missing"
    sql = open(path).read().lower()
    assert "add column if not exists upstream_uid" in sql
    assert "create unique index" in sql and "where upstream_uid is not null" in sql, (
        "the index must be PARTIAL — 84 rows are already duplicate twins of one "
        "upstream id and are frozen, so a full unique index cannot be built")
    for forbidden in ("drop column", "delete from fiber_routes", "update fiber_routes\n   set name",
                      "drop index", "truncate"):
        assert forbidden not in sql, f"migration must not {forbidden!r} — slugs are frozen"


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="control: proves this file actually runs")
def test_zzz_must_fail_control():
    assert False, "control"
