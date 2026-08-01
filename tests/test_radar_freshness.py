"""Regression guards for the 2026-07-31 /radar re-stamp drift.

Between 07-16 and 07-31 the Grid Transition Radar page re-stamped daily while
its substance was pinned: the metered map-session gate (free_tier_gate,
before_request) 402'd the page's own loopback calls to
/api/v1/grid/intelligence/*, routes/radar.py::_internal swallowed every
non-200 into {}, and the "successful" rebuild published baseline constants
under a fresh retrieved_at — Ashburn LMP printed $36.94 (the hardcoded
fallback) for 15 days while the page said "the live price this hour".

These tests pin the fix's three legs:
  1. the loopback carries X-Internal-Key (clears every before_request gate);
  2. a rebuild that reaches ZERO live feeds is STALE (reason feeds_down) even
     when its stamp is minutes old — never silently re-served as fresh;
  3. since-yesterday deltas only ever compare live figures with live figures,
     and content_rev moves with content, not with the stamp.

House rules: static AST extraction, no import of main.py or routes.radar (it
imports flask at module scope), nothing executes at module scope here.
"""
import ast
import datetime as dt
import json
import os
import time

_RADAR = os.path.join(os.path.dirname(__file__), "..", "routes", "radar.py")
_GATE = os.path.join(os.path.dirname(__file__), "..", "free_tier_gate.py")


def _radar_tree() -> ast.Module:
    with open(os.path.abspath(_RADAR), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # Guard the guard: an empty/degenerate parse would vacuously pass every
    # search below (2026-07-28 lesson — assert it parsed, never just filter).
    assert isinstance(tree, ast.Module) and len(tree.body) > 20, (
        "routes/radar.py parsed to a degenerate module — extraction harness "
        "is not looking at the real file")
    return tree


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"routes/radar.py no longer defines {name}() — "
                         "these freshness guards need updating, not deleting")


def _extract(names, extra=None) -> dict:
    """Compile the named top-level functions against stub globals. Free
    variables are proven at CALL time by the tests below (a missing global is
    a NameError, not a silent pass)."""
    tree = _radar_tree()
    g = {"dt": dt, "time": time, "json": json,
         "_CORE_STALE_WARN_S": 3600.0, "_CORE_LAST_ERROR": {}}
    g.update(extra or {})
    for name in names:
        mod = ast.Module(body=[_fn(tree, name)], type_ignores=[])
        exec(compile(mod, f"<radar:{name}>", "exec"), g)
    return g


def _consts(node) -> set:
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


# ── 1. the loopback must clear the before_request gates ──────────────────────

def test_internal_sends_internal_key():
    consts = _consts(_fn(_radar_tree(), "_internal"))
    assert "X-Internal-Key" in consts, (
        "_internal() no longer sends X-Internal-Key — loopback calls to "
        "/api/v1/grid/intelligence/* will be 402'd by the metered map-session "
        "gate again and every grid field on /radar silently pins to baseline")
    assert "DCHUB_INTERNAL_KEY" in consts, (
        "_internal() must source the key from DCHUB_INTERNAL_KEY (set on the "
        "Railway web service)")


def test_gate_treats_v4_mapped_loopback_as_loopback():
    with open(os.path.abspath(_GATE), "r", encoding="utf-8") as f:
        gate_src = f.read()
    assert "::ffff:127.0.0.1" in gate_src, (
        "free_tier_gate._resolve_caller no longer recognizes the IPv4-mapped "
        "loopback — a dual-stack listener reports '::ffff:127.0.0.1' and the "
        "metered gate will session-cap the server's own localhost calls")


# ── 2. zero live feeds ⇒ STALE, even under a fresh stamp ─────────────────────

def test_staleness_reasons():
    g = _extract(["_core_date", "_staleness"])
    now = dt.datetime(2026, 7, 31, 12, 0, tzinfo=dt.timezone.utc)
    fresh = time.time()

    live = {"retrieved_at": "2026-07-31T11:55:00+00:00",
            "feeds": {"q": {"live": True}}, "live_feed_count": 1}
    dead = {"retrieved_at": "2026-07-31T11:55:00+00:00",
            "feeds": {"q": {"live": False}}, "live_feed_count": 0}
    pre_ledger = {"retrieved_at": "2026-07-31T11:55:00+00:00"}
    yesterday = {"retrieved_at": "2026-07-30T23:55:00+00:00",
                 "feeds": {"q": {"live": True}}, "live_feed_count": 1}

    assert g["_staleness"](fresh, live, now) is None

    s = g["_staleness"](fresh, dead, now)
    assert s is not None and s["reason"] == "feeds_down", (
        "an all-fallback rebuild published silently — this is the exact "
        "re-stamp drift the 07-31 fix exists to make loud")

    # A core saved before the feeds ledger existed must NOT false-alarm.
    assert g["_staleness"](fresh, pre_ledger, now) is None

    s = g["_staleness"](fresh, yesterday, now)
    assert s is not None and s["reason"] == "date_crossed"

    s = g["_staleness"](fresh - 7200, live, now)
    assert s is not None and s["reason"] == "refresh_failing"


def test_teaser_json_declares_stale_and_health():
    node = _fn(_radar_tree(), "_teaser_json")
    consts = _consts(node)
    for key in ("stale", "staleness", "data_health", "content_rev"):
        assert key in consts, (
            f"_teaser_json() no longer emits '{key}' — agents lose the "
            "machine-checkable freshness contract (payload must say STALE "
            "rather than silently re-serve)")


# ── 3. deltas and content_rev are honest ─────────────────────────────────────

def _mk_core(q, ttp, demand, live):
    return {
        "feeds": {"queue_snapshot": {"live": live},
                  "iso_time_to_power": {"live": live},
                  "ashburn_telemetry": {"live": live}},
        "scoreboard": {
            "us_interconnection_queue_gw": q,
            "grids": [{"iso": "PJM",
                       "interconnection_queue": {"queued_gw": 171.0},
                       "dcpi_detail": {"avg_queue_wait_months": ttp}}]},
        "ashburn": {"demand_mw": demand},
    }


def test_yesterday_deltas_only_live_vs_live():
    g = _extract(["_yesterday_deltas"])
    fn = g["_yesterday_deltas"]

    assert fn(_mk_core(1801, 20.4, 21400, True), None) == []

    rows = fn(_mk_core(1801.4, 20.4, 21400, True),
              _mk_core(1757.3, 20.9, 20800, True))
    labels = {r["label"] for r in rows}
    assert "U.S. interconnection queue" in labels
    assert "Ashburn time-to-power" in labels
    assert "Ashburn zone load" in labels
    for r in rows:
        assert r["delta"] != 0 and r["unit"]

    # Yesterday was baseline-only ⇒ no delta may be printed (a live-vs-baseline
    # delta would be a fabricated movement).
    assert fn(_mk_core(1801, 20.4, 21400, True),
              _mk_core(1757, 20.9, 20800, False)) == []

    # Nothing moved ⇒ nothing to show (no zero-rows padding).
    assert fn(_mk_core(1757.3, 20.9, 20800, True),
              _mk_core(1757.3, 20.9, 20800, True)) == []


def test_content_rev_tracks_content_not_stamp():
    g = _extract(["_content_rev"])
    fn = g["_content_rev"]
    a = fn({"retrieved_at": "2026-07-31T10:00:00+00:00",
            "scoreboard": {"us_interconnection_queue_gw": 1757},
            "ashburn": {}, "markets": {}})
    b = fn({"retrieved_at": "2026-07-31T11:00:00+00:00",   # stamp moved only
            "scoreboard": {"us_interconnection_queue_gw": 1757},
            "ashburn": {}, "markets": {}})
    c = fn({"retrieved_at": "2026-07-31T10:00:00+00:00",
            "scoreboard": {"us_interconnection_queue_gw": 1801},  # figure moved
            "ashburn": {}, "markets": {}})
    assert a == b, "content_rev must ignore the retrieval stamp"
    assert a != c, "content_rev must move when a figure moves"


# ── 4. the dead wires stay dead ──────────────────────────────────────────────

def test_no_per_iso_grid_intelligence_loop():
    """The old core build looped 7× GET /api/v1/grid/intelligence/<ISO> to read
    renewable_share_pct — a field that endpoint has never returned. The only
    grid/intelligence loopback left is PJM-DOM (Ashburn telemetry)."""
    node = _fn(_radar_tree(), "_build_core")
    intel_paths = sorted(c for c in _consts(node)
                         if c.startswith("/api/") and "grid/intelligence" in c)
    assert intel_paths == ["/api/v1/grid/intelligence/PJM-DOM"], (
        f"unexpected grid/intelligence fetches in _build_core: {intel_paths}")


def test_markets_read_is_canonical_db_not_seamed_endpoint():
    tree = _radar_tree()
    node = _fn(tree, "_market_rows_db")
    src_consts = " ".join(str(c) for c in _consts(node))
    assert "discovered_facilities" in src_consts, (
        "_market_rows_db must read the canonical fleet table")
    assert "is_duplicate" in src_consts, (
        "_market_rows_db lost the #1539 fleet filter (COALESCE(is_duplicate,0)=0)")
    build_consts = _consts(_fn(tree, "_build_core"))
    assert not any("/api/v1/markets" in c for c in build_consts), (
        "_build_core is back on the /api/v1/markets loopback — that endpoint "
        "ignores its query params and returns a different envelope; the seam "
        "is what froze the NoVA ledger at the 07-16 baselines")


def test_market_count_is_metered_never_a_status_literal():
    """facility_count must count the rows carrying the MW printed beside it.

    The page renders the pair in one breath — "NoVA {va_mw} MW / {va_facilities}
    facilities" — so a count over a wider population than the SUM silently
    inflates the MW's denominator.

    This guards the 2026-07-31 fix. The old predicate `status <> 'active'` read
    as an empty-shell filter and was not one: measured on the replica it
    excluded 4,325 zero-MW fleet rows while COUNTING 4,693 other zero-MW rows
    (41.4% of its own output). The literal identified which ingest path stamped
    the row, not whether the facility had capacity — and PR #2047 routed those
    same sources through canon_status(), so shells written after 07-31 land as
    'Operational' and stop matching it. Any status literal here is drift.
    """
    node = _fn(_radar_tree(), "_market_rows_db")
    # Scan CODE only. The docstring documents the removed predicate by name, so
    # including it would let prose satisfy the guard — and would equally let a
    # real regression hide behind a mention of the old literal.
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    assert body, "_market_rows_db has no body beyond its docstring"
    sql = " ".join(str(c) for stmt in body for c in _consts(stmt))
    assert "power_mw,0) > 0" in sql, (
        "_market_rows_db no longer gates its count on power_mw — facility_count "
        "must describe the same rows as total_mw")
    for literal in ("'active'", "'operational'", "'Operational'"):
        assert literal not in sql, (
            f"_market_rows_db is keying on the status literal {literal} again. "
            "That is the exact drift the 07-31 fix removed: status says which "
            "ingest path wrote the row, not whether it has capacity, so the "
            "count moves whenever a backfill or a writer changes vocabulary")


def test_market_rows_map_metered_and_tracked_to_distinct_fields():
    """The SELECT returns metered count, MW, operators, tracked count — in that
    order. Assert the mapping BEHAVIOURALLY: a silent re-order would otherwise
    publish the tracked count (which includes unmetered shells) as
    facility_count, re-introducing the inflation with the guard above still
    green."""
    import sys, types

    captured = []

    class _Cur:
        def execute(self, sql, args=None):
            captured.append(sql)
        def fetchall(self):
            return [("Ashburn", "VA", 141, 6942.0, 50, 199)]
        def close(self):
            pass

    class _Conn:
        def cursor(self):
            return _Cur()
        def close(self):
            pass

    stub_main = types.ModuleType("main")
    stub_main.get_read_db = lambda: _Conn()
    prev = sys.modules.get("main")
    sys.modules["main"] = stub_main
    try:
        g = _extract(["_market_rows_db"],
                     extra={"_VA_CLUSTER_CITIES": ("Ashburn", "Sterling",
                                                   "Manassas")})
        rows, err = g["_market_rows_db"]()
    finally:
        if prev is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = prev

    assert err is None, f"_market_rows_db errored against a stub DB: {err}"
    assert rows == [{"city": "Ashburn", "state": "VA", "facility_count": 141,
                     "total_mw": 6942, "operator_count": 50,
                     "tracked_count": 199}], rows
    assert captured, "_market_rows_db issued no SQL"

