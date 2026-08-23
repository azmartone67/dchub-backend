"""tests/test_graph_spine_shell.py — Graph-Spine master shell (#36, 2026-07-26).

Guards routes/graph_spine_master_shell.py. The shell exists to answer "can we
replace RAG with graph engineering?" with measurements instead of vibes, so the
ways it can go wrong are all ways a MEASUREMENT can lie:

  (1) ORPHAN — the blueprint is written but never registered, so /admin/
      graph-spine 404s forever while the module looks wired. This is the smell
      that hid network_ix_ingestion.py for four months.
  (2) COUNT-DECOY — someone "simplifies" lane 1c's semantic (coordinate) decoy
      control into a row-count comparison. That version PASSES on a
      deliberately-broken join: `df.id + 1` matches 13,815 rows where the real
      join matches 13,870 (99.6%). Counts cannot separate colliding integer
      id-spaces; only coordinates can.
  (3) SILENT-WRITE — the snapshot INSERT runs on the replica, raises "read-only
      transaction", is swallowed by the fail-soft handler, and the trend table
      stays empty while every tick reports success.
  (4) CONFIDENT-GREEN — a lane whose load-bearing (critical) check could not be
      evaluated renders green anyway.
  (5) 5xx KILL — the kill switch returns 503, which the CF worker reads as a
      dead origin and fails over site-wide to the stale Render backend.
  (6) PERCENT-TRAP — a literal % in a no-params SQL string makes psycopg2
      %-substitute against an empty tuple and 500.

House rules (reference_dchub_green_main_0709): pre-merge pytest has NO DB and
must NEVER import main — so the wiring guards read main.py as TEXT, and the
live-data assertions skip without a DB URL.

Run:  python3 -m pytest tests/test_graph_spine_shell.py -v
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "main.py"
_SHELL = _ROOT / "routes" / "graph_spine_master_shell.py"

sys.path.insert(0, str(_ROOT))


def _shell_src() -> str:
    return _SHELL.read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    """Slice `def name(` out of the shell by ast (parsed, not regex-guessed)."""
    tree = ast.parse(_shell_src())
    src = _shell_src().splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "\n".join(src[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name}() not found in {_SHELL.name}")


# ── (1) orphan check ──────────────────────────────────────────────────

def test_blueprint_is_registered_in_main():
    """The check that would have caught network_ix_ingestion.py in March."""
    src = _MAIN.read_text(encoding="utf-8")
    assert "from routes.graph_spine_master_shell import graph_spine_master_shell_bp" in src
    assert "app.register_blueprint(graph_spine_master_shell_bp)" in src


def test_shell_declares_its_routes():
    src = _shell_src()
    for route in ("/api/v1/admin/graph-spine/master-tick",
                  "/admin/graph-spine"):
        assert route in src, f"route {route} missing"


# ── (2) the decoy control must stay SEMANTIC ──────────────────────────

def test_decoy_control_is_coordinate_based_not_count_based():
    """★ The load-bearing guard of this whole shell.

    A count-based decoy passes on a known-wrong join. Lane 1c must compare
    COORDINATES of matched pairs — real join ~0 deg apart, decoy ~60 deg. If
    this test fails because someone rewrote 1c in terms of row counts, the
    rewrite is wrong even though it will look green in production.
    """
    src = _func_src("_lane_carrier_idspaces")
    assert "df.id + 1" in src, "decoy join (df.id + 1) is gone"
    # the decoy must be scored on geography, not on how many rows it matched
    for token in ("facility_lat", "facility_lng", "latitude", "longitude"):
        assert token in src, f"decoy control lost its {token} comparison"
    assert "cf_decoy" in src


def _check_calls(check_id: str) -> list[ast.Call]:
    """EVERY _check(...) call carrying this id. A check usually appears twice —
    once in the 'query failed' branch and once in the real branch — so a guard
    that inspects only the first occurrence can be defeated by breaking the
    second (this test file shipped that bug once; the mutation run caught it)."""
    found = []
    for node in ast.walk(ast.parse(_shell_src())):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_check"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == check_id):
            found.append(node)
    return found


def _is_critical(call: ast.Call) -> bool:
    return any(kw.arg == "critical"
               and isinstance(kw.value, ast.Constant)
               and kw.value.value is True
               for kw in call.keywords)


@pytest.mark.parametrize("check_id", [
    "cf_decoy",        # lane 1 — the semantic decoy control
    "pdb_disjoint",    # lane 2 — the third id-space stays disjoint
    "deal_dupes",      # lane 3 — exact duplicates among SERVED rows
    "rag_serves_excluded",  # lane 4 — chunks we refuse to serve
    "es_blindspot",    # lane 5 — resolver gap we can actually close
    "es_no_reject_real",  # lane 5 — purge-noise landmine guard
    "gv_precondition",  # lane 6 — the verdict itself
])
def test_load_bearing_checks_are_critical_at_every_call_site(check_id):
    """An undetermined load-bearing check must force its lane to '?', never to
    green — at EVERY branch that can emit it."""
    calls = _check_calls(check_id)
    assert calls, f"{check_id} no longer exists"
    non_critical = [c.lineno for c in calls if not _is_critical(c)]
    assert not non_critical, (
        f"{check_id} is emitted without critical=True at line(s) {non_critical} "
        "— that branch can render the lane green while proving nothing")


def test_decoy_threshold_separates_real_from_decoy():
    """The thresholds must actually discriminate the measured values
    (real max 0.0000 deg, decoy avg 60.6 deg). A threshold that both sides
    satisfy is decoration."""
    src = _func_src("_lane_carrier_idspaces")
    m = re.search(r"real_max\s*<=\s*([0-9.]+)\s*and\s*decoy_avg\s*>=\s*([0-9.]+)", src)
    assert m, "decoy comparison shape changed — re-verify it still discriminates"
    real_ceiling, decoy_floor = float(m.group(1)), float(m.group(2))
    assert real_ceiling < decoy_floor, "thresholds overlap — cannot discriminate"
    # measured 2026-07-26 on the Neon replica
    assert real_ceiling >= 0.0 and real_ceiling <= 1.0, "real ceiling too loose"
    assert decoy_floor >= 1.0, "decoy floor too loose to catch a colliding join"


# ── (2b) every deals count must honour the quarantine ─────────────────

def test_every_deals_query_filters_quarantine():
    """★ The bug this lane shipped once: `deals` is de-duped by data_flag, not
    by DELETE, so a bare COUNT(*) over it reports 2,078 duplicates (46.4%)
    where the served table holds 40 of 1,611 (2.5%). Any SQL here that reads
    `deals` without the data_flag predicate is measuring history."""
    # Checked per FUNCTION, not per string literal: the lane assembles its SQL
    # as "… FROM deals WHERE " + _LIVE_DEALS, and ast does not fold a literal
    # concatenated with a NAME, so a literal-level scan sees only fragments and
    # passes vacuously. (It did, on the first attempt.)
    tree = ast.parse(_shell_src())
    lines = _shell_src().splitlines()
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if not re.search(r"\b(FROM|JOIN)\s+deals\b", body, re.I):
            continue
        if "_LIVE_DEALS" not in body and "data_flag" not in body:
            offenders.append(node.name)
    assert not offenders, (
        "these functions read `deals` with no data_flag filter and will count "
        f"quarantined rows as live: {offenders}")


def test_live_deals_predicate_exists_and_is_percent_free():
    """The predicate this module applies, now imported rather than copied.

    Was a module-local `_LIVE_DEALS = "coalesce(left(data_flag,11),'') <>
    'quarantine_'"` — one of seven hand-copies across the codebase, which is
    the drift util/capacity_pipeline.py was created to end and which its
    docstring names this very file for. The alias is gone on purpose: an
    alias hides the guard from the per-file census in
    tests/test_deals_guard.py, so the call sites spell DEALS_OK out.
    """
    import routes.graph_spine_master_shell as gs
    from util.deals import DEALS_OK

    assert not hasattr(gs, "_LIVE_DEALS"), (
        "_LIVE_DEALS is back. Import DEALS_OK from util.deals instead — a "
        "module-local alias is how this predicate ended up hand-copied into "
        "seven files, two of them as function-locals nothing could import.")
    assert gs.DEALS_OK is DEALS_OK, \
        "graph_spine no longer uses the canonical predicate"
    assert "data_flag" in DEALS_OK
    assert "%" not in DEALS_OK, "literal % in the quarantine predicate"
    assert "left(" in DEALS_OK.lower(), \
        "use LEFT(data_flag,11) — the LIKE form carries a literal %"


def test_blindspot_lane_mirrors_the_resolver_guards():
    """★ A lane must not measure a target the code it audits is forbidden to
    hit. Lane 5a's first cut omitted the resolver's _GENERIC_PREFIX_STOP and
    counted 17 blind-spot entities where the resolver can only ever resolve 16
    — "Power" prefix-matches a garbage facility name and is stoplisted ON
    PURPOSE — so the check would have sat permanently RED at 1.

    The stoplist must be IMPORTED from the resolver, never restated here, or
    the two drift and the lane silently starts lying again.

    ★ 2026-08-23 — the property is unchanged, its home moved. The whole
    measurement (stoplist AND query) now lives in news_entity_extraction
    beside _GENERIC_PREFIX_STOP, because restating it here was not enough:
    the query was ALSO restated in brain-autonomy and both copies stated it
    in a form that could not be indexed and timed out at 15s. Resident in the
    resolver's own module, the stoplist cannot drift from the resolver at
    all. So this guard now checks the same property one level down."""
    body = _func_src("_lane_entity_spine")
    assert "from routes.news_entity_extraction import entity_blindspot_count" \
        in body, ("lane 5a must import the resident measurement — restating "
                  "the query here is what let it rot in two places at once")
    assert "entity_blindspot_count(" in body, \
        "lane 5a imports the measurement but does not call it"

    resolver = (_ROOT / "routes" / "news_entity_extraction.py").read_text(
        encoding="utf-8")
    assert "_GENERIC_PREFIX_STOP" in resolver and \
        "def entity_blindspot_count" in resolver, (
            "the measurement must live in the SAME module as the stoplist it "
            "applies, or the two can drift again")
    measure = resolver.split("def _entity_blindspot_count")[1]
    assert "_GENERIC_PREFIX_STOP" in measure, \
        "the measurement does not apply the resolver's stoplist — "\
        "unreachable target"
    assert "NOT IN (" in measure


def test_brain_rag_deals_corpus_has_quarantine_gate():
    """The registry fix itself: without it, 2,811 embedded chunks point at
    rows /api/deals refuses to serve, and `deals` is in PUBLIC_CORPORA so they
    are retrievable on the unauthenticated /api/v1/rag/search."""
    rag = (_ROOT / "routes" / "brain_rag.py").read_text(encoding="utf-8")
    m = re.search(r'"deals":\s*\{(.*?)\n    \}', rag, re.S)
    assert m, "CORPORA['deals'] not found in brain_rag.py"
    spec = m.group(1)
    assert "data_flag" in spec, \
        "CORPORA['deals'] where-clause lost its quarantine filter"
    where = re.search(r'"where":\s*\((.*?)\),\s*\n', spec, re.S)
    assert where, "could not isolate the deals where-clause"
    assert "%" not in where.group(1), (
        "literal % in the deals where-clause — spec['where'] is interpolated "
        "into queries that pass a params tuple (_corpus_total, _count_orphans, "
        "_sweep_orphans); use LEFT(...,11) not LIKE 'quarantine_%'")


# ── (3) the snapshot must not be written on the replica ───────────────

def test_snapshot_uses_a_separate_write_connection():
    """The replica rejects writes. If the snapshot INSERT reuses the read
    connection it raises, gets swallowed, and the trend table stays empty
    while the tick keeps reporting success."""
    src = _shell_src()
    assert "def _write_conn" in src, "_write_conn() is gone"
    write_src = _func_src("_write_conn")
    assert "NEON_REPLICA_URL" not in write_src, \
        "_write_conn must NEVER resolve to the read replica"
    tick = _func_src("_run_tick")
    assert "_write_conn()" in tick, "snapshot no longer uses the write connection"
    assert "graph_spine_snapshots" in tick


def test_read_conn_prefers_replica():
    read_src = _func_src("_conn")
    assert "NEON_REPLICA_URL" in read_src


def test_tick_reports_whether_it_persisted():
    """'trend unavailable' must be distinguishable from 'trend flat'."""
    assert '"persisted"' in _func_src("_run_tick")


# ── (4) confident-green ───────────────────────────────────────────────

def test_lane_verdict_never_greens_an_undetermined_critical_check():
    from routes.graph_spine_master_shell import _check, _lane_verdict
    ok = _check("a", "fine", True, "")
    undetermined_critical = _check("b", "load-bearing", None, "", critical=True)
    undetermined_gauge = _check("c", "gauge", None, "")

    assert _lane_verdict([ok]) is True
    assert _lane_verdict([ok, undetermined_gauge]) is True
    assert _lane_verdict([ok, undetermined_critical]) is None, \
        "a lane with an unevaluated critical check must render '?', not green"
    assert _lane_verdict([ok, _check("d", "broken", False, "")]) is False
    assert _lane_verdict([undetermined_gauge]) is None


def test_every_lane_has_a_critical_check():
    """A lane with no critical check can never render '?' and will therefore
    claim green on the strength of gauges alone."""
    from routes.graph_spine_master_shell import _LANES
    # drive off _LANES, not a name prefix — `_lane_verdict` is a helper, not a
    # lane, and a prefix match silently swept it in.
    assert len(_LANES) == 6, f"expected 6 lanes, found {len(_LANES)}"
    for key, _label, fn, _actuator in _LANES:
        assert "critical=True" in _func_src(fn.__name__), \
            f"lane {key} ({fn.__name__}) has no critical check"


def test_lane_table_names_an_actuator_for_every_lane():
    from routes.graph_spine_master_shell import _LANES
    assert len(_LANES) == 6
    for key, label, fn, actuator in _LANES:
        assert callable(fn)
        assert actuator and len(actuator) > 20, f"{key} has no named actuator"


# ── (5) kill switch must not 5xx ──────────────────────────────────────

def test_kill_switch_returns_404_not_5xx():
    """A 5xx from Railway trips the CF worker's failover to the stale Render
    origin; two within 10s break the site for 30s.

    Walks the AST rather than slicing text: a regex bounded by a blank line
    silently skipped the JSON endpoint's guard (whose block has no blank line
    inside it), so flipping its 404 to a 503 went undetected.
    """
    tree = ast.parse(_shell_src())
    guards = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If)
                and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            continue
        guards += 1
        codes = [c.value for c in ast.walk(node)
                 if isinstance(c, ast.Constant) and isinstance(c.value, int)
                 and 100 <= c.value < 600]
        assert codes, f"_disabled() guard at line {node.lineno} returns no status code"
        for code in codes:
            assert code == 404, (
                f"_disabled() guard at line {node.lineno} returns {code} — a kill "
                "switch must never emit 5xx (CF reads it as a dead origin)")
    assert guards >= 2, f"expected a kill guard on each endpoint, found {guards}"


def test_shell_is_admin_gated():
    src = _shell_src()
    assert src.count("_admin_ok()") >= 2, "an endpoint is missing the admin gate"
    assert "DCHUB_ADMIN_KEY" in src


def test_kill_switch_env_var_is_documented():
    assert "GRAPH_SPINE_SHELL_DISABLE" in _shell_src()
    assert "GRAPH_SPINE_SHELL_DISABLE" in _MAIN.read_text(encoding="utf-8")


# ── (6) percent trap + read-only discipline ───────────────────────────

def test_no_literal_percent_in_no_params_sql():
    """_row()/_scalar() call cur.execute(sql) with NO params tuple. A literal
    % in those strings is fine there, but the module deliberately writes every
    pattern match as a POSIX regex so a future refactor that adds a params
    tuple cannot detonate (reference_psycopg2_empty_tuple_percent_trap)."""
    # Inspect SQL STRING LITERALS only — prose, display strings and logging
    # format strings all legitimately contain '%'. Concatenated SQL is folded
    # by ast into a single constant, so this sees whole statements.
    sql_kw = re.compile(r"\b(SELECT|INSERT INTO|UPDATE|DELETE FROM|CREATE TABLE)\b", re.I)
    offenders = []
    for node in ast.walk(ast.parse(_shell_src())):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        s = node.value
        if not sql_kw.search(s) or "%" not in s:
            continue
        if "INSERT INTO graph_spine_snapshots" in s:
            continue  # the one deliberately parameterised statement
        offenders.append(s[:120])
    assert not offenders, (
        "literal % inside a SQL string — psycopg2 will %-substitute against an "
        f"empty tuple and 500: {offenders}")


def test_pattern_matches_use_regex_not_like():
    """Every LIKE is written as a POSIX regex (~) so no SQL string in this
    module carries a literal % wildcard. Scoped to SQL literals — the module
    prose deliberately names the LIKE form it is telling you not to use."""
    sql_kw = re.compile(r"\b(SELECT|INSERT INTO|CREATE TABLE)\b")
    for node in ast.walk(ast.parse(_shell_src())):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        s = node.value
        if sql_kw.search(s) and re.search(r"\bLIKE\s+'", s, re.I):
            assert False, (
                f"use a POSIX regex (~ '^AUTO-') instead of LIKE: {s[:120]}")


def test_shell_writes_nothing_but_its_own_snapshot():
    """READ-ONLY / names an actuator and fires nothing."""
    src = _shell_src()
    mutations = re.findall(r"\b(INSERT INTO|UPDATE |DELETE FROM|CREATE TABLE)\s+(\w+)",
                           src, re.I)
    for verb, table in mutations:
        assert table == "graph_spine_snapshots" or "IF NOT EXISTS" in src, \
            f"shell mutates {table} via {verb} — it must be read-only"
    assert "graph_spine_snapshots" in src


# ── live assertions (skipped without a DB) ────────────────────────────

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))
_live = pytest.mark.skipif(not _DB, reason="no DB URL — live checks skipped")


@_live
def test_live_tick_runs_and_returns_a_verdict():
    from routes.graph_spine_master_shell import _run_tick
    p = _run_tick()
    assert p["ok"] is True
    assert p["lanes_total"] == 6
    assert p["verdict"] in ("READY", "NOT_READY", "UNKNOWN")
    assert len(p["lanes"]) == 6


@_live
def test_live_decoy_control_actually_discriminates():
    """The empirical claim the shell rests on, asserted against live data:
    the real join's matched pairs must be far tighter than the decoy's."""
    import psycopg2
    c = psycopg2.connect(_DB, connect_timeout=10)
    c.autocommit = True
    q = ("SELECT round(avg(d)::numeric,4) FROM ("
         " SELECT abs(df.latitude - cfp.facility_lat)"
         "      + abs(df.longitude - cfp.facility_lng) AS d"
         " FROM discovered_facilities df"
         " JOIN carrier_facility_presence cfp ON cfp.dchub_facility_id = {}"
         " WHERE df.latitude IS NOT NULL AND cfp.facility_lat IS NOT NULL"
         " LIMIT 20000) x")
    with c.cursor() as cur:
        cur.execute(q.format("df.id::text"))
        real = float(cur.fetchone()[0])
        cur.execute(q.format("(df.id + 1)::text"))
        decoy = float(cur.fetchone()[0])
    c.close()
    assert real < 0.01, f"real join no longer coordinate-tight ({real} deg)"
    assert decoy > 5.0, f"decoy join is no longer far ({decoy} deg)"
