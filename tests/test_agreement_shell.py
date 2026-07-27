"""tests/test_agreement_shell.py — Agreement master shell (#37, 2026-07-27).

Guards routes/agreement_master_shell.py. This shell watches for "two things
that should agree, silently not agreeing", so its own failure modes are the
same ones it hunts — a lane that drifts from the code it audits, or one that
asserts a target nothing can reach.

Regressions guarded, each one a mistake actually made while building it:

  (1) LANE 3 FALSE ALARM — matching the substring "published" flagged
      news_articles and announcements, whose `published_at` / `published_date`
      are event TIMES, not publication gates. Filtering by data_type does not
      save you: both are TEXT on live (LIVE ≠ repo DDL), so the discriminator
      must be the NAME SUFFIX. A bare `status` is excluded too — on
      construction_permits it holds announced/under_construction, which
      describes the world, not our editorial state.
  (2) LANE 1 BUILT ON A BROKEN INSTRUMENT — the original design reported
      "endpoints never invoked" from api_endpoint_log, which captures ~0 admin
      calls (56 rows in 548k; 0 of ~50 reindex calls). That would have branded
      ~290 endpoints dead with total confidence. The lane must NOT claim
      invocation, and must keep reporting the observability gap.
  (3) 5xx KILL SWITCH — a disabled shell that returns 5xx trips the CF worker's
      failover to the STALE Render origin. This shell asserts that about other
      shells, so it had better hold itself to it.
  (4) UNCACHED SOURCE SCAN — the AST pass over ~670 route files costs ~2s;
      running it per tick puts that on an admin page refresh.

House rules (reference_dchub_green_main_0709): no `import main`; live
assertions skip without a DB URL.

Run:  python3 -m pytest tests/test_agreement_shell.py -v
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
_SHELL = _ROOT / "routes" / "agreement_master_shell.py"
sys.path.insert(0, str(_ROOT))


def _src() -> str:
    return _SHELL.read_text(encoding="utf-8")


def _func_src(name: str) -> str:
    tree = ast.parse(_src())
    lines = _src().splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{name}() not found")


# ── wiring ────────────────────────────────────────────────────────────

def test_blueprint_is_registered_in_main():
    src = _MAIN.read_text(encoding="utf-8")
    assert "from routes.agreement_master_shell import agreement_master_shell_bp" in src
    assert "app.register_blueprint(agreement_master_shell_bp)" in src


def test_kill_switch_env_documented():
    assert "AGREEMENT_SHELL_DISABLE" in _src()
    assert "AGREEMENT_SHELL_DISABLE" in _MAIN.read_text(encoding="utf-8")


# ── (3) this shell obeys the rule it enforces ─────────────────────────

def test_kill_switch_returns_404_not_5xx():
    """Lane 5 asserts other shells never 5xx on their kill switch. This one
    must hold itself to it — walked via AST because a regex bounded by a blank
    line silently skipped a guard once already (shell #36)."""
    guards = 0
    for node in ast.walk(ast.parse(_src())):
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            continue
        guards += 1
        codes = [c.value for c in ast.walk(node)
                 if isinstance(c, ast.Constant) and isinstance(c.value, int)
                 and 100 <= c.value < 600]
        assert codes and all(c == 404 for c in codes), \
            f"_disabled() guard at line {node.lineno} returns {codes}, must be 404"
    assert guards >= 2, f"expected a kill guard per endpoint, found {guards}"


# ── (1) lane 3 must not cry wolf, and must still catch the real thing ──

def test_lane3_excludes_event_time_columns():
    body = _func_src("_lane_public_gates")
    assert '"_at"' in body and '"_date"' in body, (
        "lane 3 lost its _at/_date suffix filter — it will flag "
        "news_articles.published_at as a leak")
    assert "_is_gate_col" in body


def test_lane3_does_not_assert_on_bare_status():
    """`status` on construction_permits is announced/under_construction — the
    world's state, not ours. Asserting on it is a permanent false alarm."""
    from routes.agreement_master_shell import _lane_public_gates  # noqa: F401
    body = _func_src("_lane_public_gates")
    m = re.search(r"PUB_WORDS\s*=\s*\(([^)]*)\)", body, re.S)
    assert m, "PUB_WORDS not found"
    words = [w.strip().strip("\"'") for w in m.group(1).split(",") if w.strip()]
    assert "status" not in words, \
        "bare 'status' in PUB_WORDS — construction_permits/capacity_pipeline " \
        "will be flagged forever"
    assert "row_status" in words and "data_flag" in words, \
        "unambiguous editorial columns must still be matched"


def test_lane3_imports_the_registry_rather_than_restating_it():
    body = _func_src("_lane_public_gates")
    assert "from routes.brain_rag import" in body, (
        "the corpus registry is restated instead of imported — it will drift "
        "from the thing it audits, which is this shell's entire subject")


def test_gate_col_classifier_matrix():
    """The discriminator, exercised directly."""
    import routes.agreement_master_shell as m
    src = _func_src("_lane_public_gates")
    ns: dict = {}
    # lift the nested helper out for a direct unit test
    body = re.search(r"(    PUB_WORDS = .*?\n    def _is_gate_col.*?\n        return any.*?\n)",
                     src, re.S)
    assert body, "could not isolate _is_gate_col"
    exec(re.sub(r"^    ", "", body.group(1), flags=re.M), ns)
    f = ns["_is_gate_col"]
    # real gates
    assert f("published") and f("data_flag") and f("row_status")
    assert f("is_duplicate") and f("hidden")
    # event times — NOT gates
    assert not f("published_at"), "published_at is an event time"
    assert not f("published_date"), "published_date is an event time"
    assert not f("created_at")
    # ambiguous / unrelated
    assert not f("status"), "bare status is world-state, not editorial"
    assert not f("title") and not f("buyer")


# ── (2) lane 1 must not claim invocation ──────────────────────────────

def test_lane1_observability_is_a_liveness_check_on_credential_classes():
    """Was a gauge while admin traffic was invisible. api_usage_tracker now
    stamps a credential CLASS, so the check asserts — and is satisfied by this
    shell's own admin tick, which makes it a can-we-see-ourselves probe."""
    body = _func_src("_lane_destructive")
    assert "ds_observability" in body
    assert "'admin','cron','internal'" in body, \
        "the lane no longer looks for the credential-class markers"
    assert "critical=True" in body.split("ds_observability")[-1][:400], \
        "observability must be critical — an unmeasurable surface is not a pass"


def test_tracker_records_admin_credentials_without_storing_the_secret():
    """★ The fix, exercised FUNCTIONALLY rather than by grepping the source.

    A string-presence assertion passed even with the admin branch deleted,
    because "X-Admin-Key" still appeared in the explanatory comment — the same
    weak-guard failure this shell exists to catch. So: build a real Flask app,
    install the tracker, issue requests, and read the buffer.

    Two properties, both load-bearing:
      · an admin-authenticated request IS recorded (it was invisible before)
      · what lands is the credential CLASS, never the secret, and never a
        `dchub_`-prefixed value that could collide with a partner in
        partner-usage reporting
    """
    from flask import Flask
    import routes.api_usage_tracker as t

    app = Flask(__name__)

    @app.route("/api/v1/admin/probe", methods=["POST"])
    def _probe():
        return "ok"

    @app.route("/api/v1/open")
    def _open():
        return "ok"

    # install_tracker() touches the DB for schema; the hooks are what we want
    try:
        t.install_tracker(app)
    except Exception:
        pytest.skip("tracker install unavailable in this environment")

    secret = "SUPERSECRETADMINKEY_do_not_log_me_0123456789"
    with t._BUFFER_LOCK:
        t._BUFFER.clear()
    client = app.test_client()
    client.post("/api/v1/admin/probe", headers={"X-Admin-Key": secret})
    client.get("/api/v1/open")           # unauthenticated — must stay untracked
    with t._BUFFER_LOCK:
        entries = list(t._BUFFER)
        t._BUFFER.clear()

    admin = [e for e in entries if e["path"] == "/api/v1/admin/probe"]
    assert admin, "admin-authenticated request was NOT recorded — the gap is back"
    prefix = admin[0]["key_prefix"]
    assert prefix == "admin", f"expected credential class 'admin', got {prefix!r}"
    assert secret not in str(entries), "the admin secret reached the buffer"
    assert not str(prefix).startswith("dchub_"), \
        "marker collides with the partner key-prefix namespace"
    assert not [e for e in entries if e["path"] == "/api/v1/open"], \
        "unauthenticated traffic is now being tracked — scope creep"

def test_lane1_baseline_is_a_baseline_not_zero():
    """18 set-wide statements exist and several are legitimate (TTL cleanups,
    admin purges). Asserting zero would be unreachable — the #36 mistake."""
    body = _func_src("_lane_destructive")
    assert "baseline = 18" in body
    assert "len(setwide) <= baseline" in body, \
        "the check must allow the reviewed baseline, not demand zero"


# ── (4) cost discipline ───────────────────────────────────────────────

def test_source_scan_is_cached_per_process():
    src = _src()
    assert "_SCAN is None" in src and "_SCAN_LOCK" in src, \
        "the ~2s AST scan must be cached, never re-run per tick"
    tick = _func_src("_run_tick")
    assert "_scan_routes()" not in tick, "tick must not call the raw scanner"


def test_write_conn_never_resolves_to_the_replica():
    assert "NEON_REPLICA_URL" not in _func_src("_write_conn")
    assert "NEON_REPLICA_URL" in _func_src("_conn")


def test_every_lane_has_a_critical_check():
    from routes.agreement_master_shell import _LANES
    assert len(_LANES) == 5
    for key, _label, fn, actuator in _LANES:
        assert "critical=True" in _func_src(fn.__name__), f"{key} has no critical check"
        assert actuator and len(actuator) > 20, f"{key} names no actuator"


def test_no_literal_percent_in_sql_strings():
    """A literal % in a SQL string + a params tuple = psycopg2 500."""
    sql_kw = re.compile(r"\b(SELECT|INSERT INTO|CREATE TABLE|UPDATE)\b")
    bad = []
    for node in ast.walk(ast.parse(_src())):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        s = node.value
        if sql_kw.search(s) and "%" in s and "agreement_snapshots" not in s:
            bad.append(s[:100])
    assert not bad, f"literal % inside SQL: {bad}"


def test_shell_detection_requires_actual_routes():
    """★ `_brand_shell.py` is an HTML template helper with ZERO routes. The
    first cut of lane 5 matched on filename and reported it as "no kill
    switch" — meaningless for a module you cannot call, and exactly the
    false-alarm class this shell exists to prevent."""
    src = _src()
    assert "_is_route" in src, "route detection helper is gone"
    scan = _func_src("_scan_routes")
    assert "and route_fns" in scan, (
        "lane 5 classifies shells by FILENAME again — _brand_shell will be "
        "flagged forever")


def test_question_class_is_stamped_by_the_track_writer():
    """Lane 4's actuator. The classifier must be wired into the single place
    mcp_call_log params are built, or the lane can never go green."""
    fme = (_ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8")
    assert "from routes._question_class import classify" in fme
    assert '"question_class"' in fme
    i = fme.index("question_class")
    assert "except Exception" in fme[i:i + 900], \
        "the enrichment is not fail-soft — telemetry must never break a call"


def test_lane2_asserts_the_schema_not_the_data():
    """★ v1 checked "0 NULLs right now" — true by LUCK, since nothing stopped a
    writer inserting one. The column is now NOT NULL DEFAULT 0 on live, and the
    lane must assert THAT: a data check passes right up until the moment the
    invariant breaks, a schema check cannot."""
    body = _func_src("_lane_predicates")
    assert "information_schema.columns" in body and "is_nullable" in body, (
        "lane 2 checks live NULL counts again — assert the constraint, not the "
        "data it happens to hold today")
    assert "is_duplicate IS NULL" not in body, \
        "the old data-shaped check is back"


# ── live ──────────────────────────────────────────────────────────────

_DB = (os.environ.get("NEON_REPLICA_URL") or os.environ.get("DATABASE_URL")
       or os.environ.get("NEON_DATABASE_URL"))
_live = pytest.mark.skipif(not _DB, reason="no DB URL — live checks skipped")


@_live
def test_live_is_duplicate_is_not_nullable():
    """The migration itself, asserted against live. Reverting it silently
    re-arms 79 call sites in a table that is 75% duplicates."""
    import psycopg2
    c = psycopg2.connect(_DB, connect_timeout=10)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SELECT is_nullable, column_default FROM information_schema.columns"
                    " WHERE table_name='discovered_facilities'"
                    "   AND column_name='is_duplicate'")
        nullable, default = cur.fetchone()
    c.close()
    assert nullable == "NO", "discovered_facilities.is_duplicate is nullable again"
    assert (default or "").startswith("0"), f"default lost: {default!r}"


@_live
def test_live_tick_runs():
    from routes.agreement_master_shell import _run_tick
    p = _run_tick()
    assert p["ok"] is True and p["lanes_total"] == 5
    assert len(p["lanes"]) == 5


@_live
def test_live_public_gates_are_clean_and_the_check_can_still_fire():
    """★ POSITIVE CONTROL. A green lane proves nothing unless the same lane
    goes red on a known leak — so we re-introduce one and require detection."""
    import routes.brain_rag as br
    from routes.agreement_master_shell import _lane_public_gates, _conn
    c = _conn()
    clean = [ch for ch in _lane_public_gates(c, {}) if ch["id"] == "pg_gates"][0]
    assert clean["pass"] is True, f"live leak present: {clean['detail']}"

    original = br.CORPORA["press_releases"]
    try:
        br.CORPORA["press_releases"] = dict(original, where="coalesce(t.title,'') <> ''")
        leaked = [ch for ch in _lane_public_gates(c, {}) if ch["id"] == "pg_gates"][0]
        assert leaked["pass"] is False, \
            "lane 3 did not detect an ungated press_releases — it cannot catch the next leak"
        assert "press_releases" in leaked["detail"]
    finally:
        br.CORPORA["press_releases"] = original
