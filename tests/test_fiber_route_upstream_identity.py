"""Guard SH52-054 (round 2): fiber identity must come from the SOURCE, not the crawl.

WHY THIS EXISTS ON TOP OF test_fiber_route_dedup_no_collapse.py
───────────────────────────────────────────────────────────────
That guard proved the 2026-08-10 fingerprint stopped DISCARDING distinct
segments, and it does — the hifld lane went 109 rows -> 1,826 in three days.
It passed while the fix was still wrong, because every case it feeds
_save_route supplies DIFFERENT geometry per segment, and the real caller
does not.

_sync_hifld_transmission_lines queries HIFLD with return_geometry=False and
then fills start_lat/start_lng with market['lat']/market['lng'] — the market
centroid. So the fingerprint's "geometry" is a constant within a market and
differs for the SAME line seen from a neighbouring market. DC_MARKETS are
swept at 50 km and the radii overlap.

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
      (SH52-056).
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
    """Exactly the shape _sync_hifld_transmission_lines builds."""
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



def _func_or_none(name):
    """The module-level helper `name`, executed standalone. Returns None if it
    is absent, so a rename surfaces as the `assert captured` failure this file
    already explains rather than an import-time crash."""
    tree = ast.parse(open(SRC).read())
    node = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    if node is None:
        return None
    # hifld_owner closes over the module-level _HIFLD_NULL_STRINGS set, so the
    # FunctionDef alone is not executable. Carry the module-level constants it
    # depends on; without them the helper raises NameError inside the caller's
    # per-market try/except and the whole thing reads as "no routes".
    consts = [n for n in tree.body
              if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id.startswith("_HIFLD")
                      for t in n.targets)]
    ns = {"re": __import__("re")}
    exec(compile(ast.Module(body=consts + [node], type_ignores=[]), SRC, "exec"), ns)
    return ns[name]

def _drive_hifld_sync(features_by_market):
    """Run the REAL _sync_hifld_transmission_lines against a stubbed upstream.

    ★ WHY THIS EXISTS. The first cut of G2 built its own route dict and handed
    it straight to _save_route — so deleting `"uid": line_id` from the actual
    caller left G2 GREEN. Mutation-tested 2026-08-12: that mutation passed.
    That is precisely how the previous guard
    (test_fiber_route_dedup_no_collapse.py) stayed green while the market-
    centroid bug shipped: it fed _save_route inputs the real caller never
    produces. A guard must fail when the CALLER stops supplying identity.
    """
    fn = _func("_sync_hifld_transmission_lines")
    captured = []
    markets = [{"name": m, "lat": la, "lng": lo, "state": "XX"}
               for m, la, lo in features_by_market]

    def _query_hifld_nearby(api_url, lat, lng, **kw):
        for m in markets:
            if (m["lat"], m["lng"]) == (lat, lng):
                return _UPSTREAM_FEATURES
        return []

    ns = {
        "DC_MARKETS": markets,
        "HIFLD_APIS": {"transmission_lines": "stub://tl"},
        "_query_hifld_nearby": _query_hifld_nearby,
        # 2026-08-15: the caller now scrubs HIFLD's sentinels (-999999 kV,
        # 'NOT AVAILABLE' owner) through these three module-level helpers before
        # it builds a name. They are supplied here for exactly the reason
        # MARKETS_PER_RUN is: a missing name raises inside the per-market
        # try/except and reads as "no routes", which is the failure mode the
        # `assert captured` below exists to catch — and did catch, on the very
        # commit that added them.
        "hifld_voltage": _func_or_none("hifld_voltage"),
        "hifld_owner": _func_or_none("hifld_owner"),
        "hifld_line_name": _func_or_none("hifld_line_name"),
        "logger": types.SimpleNamespace(info=lambda *a, **k: None,
                                        warning=lambda *a, **k: None),
        "time": types.SimpleNamespace(sleep=lambda *a: None),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), ns)
    self = types.SimpleNamespace(
        _market_index=0, MARKETS_PER_RUN=len(markets),
        # SH52-057 raised the per-market record cap off a hardcoded 100 and onto
        # a class attribute (the old value capped the lane at 20 markets x 100 =
        # 2,000 distinct lines forever). Supplied here for the same reason
        # MARKETS_PER_RUN is: this stub self stands in for the real class, and a
        # missing attribute would raise inside the per-market try/except and
        # read as "no duplicates found" — which the `assert captured` below
        # exists to catch, and did.
        #
        # The VALUE is irrelevant to what this test proves (identity survives
        # being seen from two markets). That the shipped cap is large enough to
        # not truncate a real market is asserted in
        # tests/test_hifld_transmission_layer_canon.py, against the real class
        # attribute — so nothing is left unguarded by hardcoding it here.
        HIFLD_MAX_RECORDS=1000,
        _save_route=lambda route, source='discovery': captured.append(route))
    ns["_sync_hifld_transmission_lines"](self)
    # The real function wraps each market in try/except and only logs a warning,
    # so an exploding stub would otherwise read as "no duplicates found".
    assert captured, ("the sync produced NO routes at all — the stub or the "
                      "function signature drifted; this test proves nothing "
                      "until routes flow")
    return captured


# One physical HIFLD line, exactly as the live FeatureServer returns it
# (verified 2026-08-12: ID populated on 89,744/89,744 on the canonical layer
# SH52-057 repointed this lane onto — 0 null, 0 empty, 0 sentinel; the same
# property held on the superseded 52,244-feature layer, so the fixture is
# valid across the swap. OBJECTID is 1..N on both, which is why it is not the
# identity.)
_UPSTREAM_FEATURES = [{
    "attributes": {"ID": "141463", "OBJECTID": 541,
                   "OWNER": "VIRGINIA ELECTRIC & POWER CO", "VOLTAGE": 230,
                   "SUB_1": "BENNING (69KV)", "SUB_2": "BENNING (230KV)",
                   "TYPE": "AC; OVERHEAD", "STATUS": "IN SERVICE"},
}]


def test_g2_caller_end_to_end_same_line_two_markets_one_identity():
    """The market-centroid bug, driven through the REAL caller."""
    routes = _drive_hifld_sync([
        ("Northern Virginia", 39.0, -77.4),
        ("Baltimore", 39.29, -76.61),
    ])
    assert len(routes) == 2, f"expected the line once per market, got {len(routes)}"
    nova, balt = routes

    # The centroids really do differ — otherwise this test is trivially true.
    assert (nova["start_lat"], nova["start_lng"]) != (balt["start_lat"], balt["start_lng"])

    _, _, ns = _make_saver()
    uid_nova = ns["_route_uid"](nova)
    uid_balt = ns["_route_uid"](balt)
    assert uid_nova == uid_balt == "141463", (
        f"the caller gave the same physical line two identities "
        f"({uid_nova!r} vs {uid_balt!r}) — it is keying on the market centroid, "
        "not on the upstream HIFLD ID. Measured live 2026-08-12: 1,826 hifld "
        "rows over 1,742 distinct upstream ids.")


def test_g2c_caller_falls_back_to_the_substation_pair_not_objectid():
    """With ID absent, identity must come from SUB_1/SUB_2/VOLTAGE — never the
    export row number."""
    global _UPSTREAM_FEATURES
    saved = _UPSTREAM_FEATURES
    try:
        _UPSTREAM_FEATURES = [{"attributes": dict(saved[0]["attributes"], ID="")}]
        routes = _drive_hifld_sync([("Northern Virginia", 39.0, -77.4)])
    finally:
        _UPSTREAM_FEATURES = saved
    uid = routes[0].get("uid") or ""
    assert "541" != uid, "fell back to OBJECTID — an export row number"
    assert "BENNING (69KV)" in uid and "BENNING (230KV)" in uid, (
        f"expected the substation-pair composite, got {uid!r}")


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


# ── G5 — OBJECTID must never become identity ─────────────────────────────────
def test_g5_hifld_caller_never_keys_on_arcgis_objectid():
    fn = _func("_sync_hifld_transmission_lines")
    consts = [n.value for n in ast.walk(fn)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert "OBJECTID" not in consts, (
        "the HIFLD caller reads OBJECTID into the route key. OBJECTID is the row "
        "number of ONE ArcGIS export — re-export in a different order and it "
        "names a different line. That is exactly what substations.hifld_objectid "
        "holds for 78,356 of 79,686 rows (SH52-056).")
    assert "ID" in consts, "the HIFLD caller no longer reads the upstream ID field"


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
