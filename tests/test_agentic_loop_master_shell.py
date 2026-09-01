"""Shell #65 (agentic loop) — what the shell must REFUSE to do, each guard
mutation-tested against a must-stay-green control.

CI-SAFETY: pure functions, stubs, AST and a Flask test client. No DB, no
network — the pre-merge job installs neither, and a guard that can only SKIP
is a silent green. Every sibling mechanism (part B's graduation_report /
queue_ages, part C's recall_negative_lessons / claim_lessons corpus) is
exercised BOTH present (via stubs) and absent, because the shell ships before
either lands and must read `?` — never PASS — in the gap.

WHAT THESE PIN, and why each one is a real failure and not a restatement:

  · A lane whose every read failed renders `?`, never PASS (the #54/#55
    contract). Tested by blinding every read helper and running every lane.
  · The GET never acts and never beats; only the POST tick does, and it beats
    status=error when the tick failed (never warn).
  · graduation_report() files inbox rows BY CONTRACT. The read path may only
    call it when it can switch filing off; otherwise it refuses. Calling a
    writer from a GET on the hope it is harmless is a GET acting.
  · The armed filing is bounded by the shell's own ledger: cap reached → not
    called; ledger unreadable → not called; unarmed → never called.
  · Each critical check has its must-fail case AND a must-stay-green control:
    a granted class failing grant_allowed, a tripped class executing after
    the trip, the claim_lessons corpus leaking into PUBLIC_CORPORA, a product
    detector dropping out of scan_all()'s tuple.
  · Kill switch answers 404, never 5xx; the scheduler drives the tick;
    main.py registers the shell in its OWN try/except; the feed has ONE writer.
"""
from __future__ import annotations

import ast
import datetime as _dt
import glob
import json
import os
import sys
import time
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "routes", "agentic_loop_master_shell.py")
MAIN = os.path.join(ROOT, "main.py")
CRON = os.path.join(ROOT, "routes", "cron_heartbeat.py")
FEED = "agentic-loop-shell-daily"
TICK_ROUTE = "/api/v1/brain/agentic-loop/master-tick"


def _src(path=SRC) -> str:
    return open(path, encoding="utf-8").read()


def _uncommented(body: str) -> str:
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


def _utc(h: float = 0.0) -> _dt.datetime:
    return _dt.datetime(2026, 8, 20, tzinfo=_dt.timezone.utc) + _dt.timedelta(hours=h)


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    pytest.importorskip("requests")
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import agentic_loop_master_shell as m
    return m


@pytest.fixture
def blind(shell, monkeypatch):
    """Every read fails: no DB, no sibling module, no token, no source."""
    monkeypatch.setattr(shell, "_q", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_import_attr", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_module", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_gh", lambda *a, **k: (None, "no GitHub token in this test — unverified, NOT assumed fine"))
    monkeypatch.setattr(shell, "_read", lambda *a, **k: "")
    monkeypatch.setattr(shell, "_conn", lambda: None)
    return shell


def _attr_router(monkeypatch, shell, table: dict):
    """Route _import_attr((module, name)) through a dict; anything not listed
    is ABSENT. Lets one test present and withhold part B/C callables."""
    monkeypatch.setattr(shell, "_import_attr",
                        lambda mod, name: table.get((mod, name)) or table.get(name))


def _q_router(monkeypatch, shell, table: dict, default=None):
    """Route _q(sql) by substring: the first key found in the SQL wins."""
    def _q(sql, params=None, conn=None, ctx=None):
        for key, rows in table.items():
            if key in sql:
                return rows
        return default
    monkeypatch.setattr(shell, "_q", _q)


_INBOX_SQL = "COUNT(action_class) OVER ()"       # routes _open_inbox()'s one read


def _inbox(rows, open_rows=None, classified=None):
    """Rows in _open_inbox()'s shape from 7-tuples of
    (id, title, status, action_class, action_url, requested_at, finding_key).

    The two totals are window functions computed BEFORE the LIMIT, so they ride
    on every row and stay true even when the row read itself was capped — pass
    open_rows/classified explicitly to model a truncated read.
    """
    total = len(rows) if open_rows is None else open_rows
    cl = sum(1 for r in rows if r[3]) if classified is None else classified
    return [tuple(r) + (total, cl) for r in rows]


def _row(rid, *, title="t", status="awaiting_decision", cls=None, url=None,
         requested=None, key=None):
    return (rid, title, status, cls, url, requested, key)


def _client(shell, monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(shell.agentic_loop_master_shell_bp)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    return app.test_client()


# ── "?" is a real verdict, never a soft pass ─────────────────────────────

def _extract_verdict_ns():
    tree = ast.parse(_src())
    ns: dict = {}
    want = {"_lane_verdict", "_check"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            exec(compile(ast.Module([node], []), SRC, "exec"), ns)
    missing = want - set(ns)
    assert not missing, f"EXTRACTION EMPTY: {sorted(missing)} not found in {SRC}"
    return ns


def test_a_lane_whose_reads_all_failed_is_not_green():
    ns = _extract_verdict_ns()
    v, c = ns["_lane_verdict"], ns["_check"]
    assert v([c("x", "readable", None, "no db", critical=True)]) == "?"
    assert v([c("x", "readable", None, "no db"), c("y", "other", None, "no db")]) == "?"


def test_a_failed_check_beats_everything_and_a_verified_lane_passes():
    ns = _extract_verdict_ns()
    v, c = ns["_lane_verdict"], ns["_check"]
    assert v([c("a", "read", True, "ok", critical=True), c("b", "inv", False, "x")]) == "FAIL"
    assert v([c("a", "read", None, "unreadable", critical=True), c("b", "x", True, "ok")]) == "?"
    assert v([c("a", "read", True, "ok", critical=True), c("b", "inv", True, "held")]) == "PASS"


def test_every_lane_with_every_read_failed_reads_question_mark_not_pass(blind):
    """The single most important property. Parts B and C are not on main yet
    and the CI box has no DB; if an unreadable lane rendered PASS the board
    would report health it never observed."""
    assert blind._LANES, "EXTRACTION EMPTY: no lanes"
    for key, name, _h, fn in blind._LANES:
        checks = fn({"conn": None})
        assert checks, f"lane {key} produced no checks"
        verdict = blind._lane_verdict(checks)
        assert verdict == "?", (
            f"lane {key} ({name}) read {verdict} with every read failed: "
            f"{[(c['id'], c['pass']) for c in checks]}")


def test_tick_with_nothing_readable_is_all_question_marks_and_does_not_raise(blind):
    out = blind._tick(act=False)
    assert out["ok"] is True and out["report_only"] is True and out["number"] == 65
    assert out["summary"]["PASS"] == 0
    assert out["summary"]["?"] == len(blind._LANES)
    assert out["tick_failed"] is False          # unreadable is not raised
    m = out["metrics"]
    for k in ("claims_confirmed", "refuted_kept", "retracted", "granted_classes",
              "recurrence_rate", "recurrence_delta_7d"):
        assert k in m and m[k] is None, f"{k} must be null (not measured), got {m[k]!r}"


# ── the shell must not become a hazard itself ────────────────────────────

def test_the_read_is_bounded_by_a_budget_inside_the_edge_timeout(shell, monkeypatch):
    """★ A board written to catch outages must not cause one.

    worker.js ROUTE_TIMEOUTS has no entry for /api/v1/brain/… or /admin/…, so
    both get DEFAULT = 15_000 ms, and /api/v1/* GETs are in RETRYABLE_PREFIXES:
    a read that overruns is retried (double load) and then answered 503 — and
    the worker reads any 5xx from Railway as a dead origin and fails the whole
    site over to stale Render. Measured 2026-08-22, the first cut of this shell
    took 77.9s. So the read carries a deadline and renders `?` for whatever it
    did not reach. The scheduled tick does not cross the edge (cron_heartbeat's
    BASE is the loopback) and gets a budget inside _hit()'s own 30s.
    """
    assert shell.READ_BUDGET_S < 15, (
        "the read budget must sit INSIDE worker.js ROUTE_TIMEOUTS.DEFAULT (15s)")
    assert shell.TICK_BUDGET_S < 30, (
        "the tick budget must sit inside cron_heartbeat._hit()'s 30s timeout")
    edge = _src(os.path.join(ROOT, "worker.js"))
    assert "'DEFAULT': 15_000" in edge, (
        "worker.js DEFAULT timeout moved — re-derive READ_BUDGET_S")

    monkeypatch.setattr(shell, "_conn", lambda: None)
    monkeypatch.setattr(shell, "_q", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_gh", lambda *a, **k: (None, "no GitHub token in this test — unverified, NOT assumed fine"))
    monkeypatch.setattr(shell, "_module", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_import_attr", lambda *a, **k: None)

    # CONTROL: with the shipped budget nothing is cut off
    out = shell._tick(act=False)
    assert out["budget"]["lanes_not_run"] == [] and out["tick_failed"] is False

    # MUST-DEGRADE: no budget at all -> every lane `?`, none PASS, tick_failed
    monkeypatch.setattr(shell, "READ_BUDGET_S", 0)
    out = shell._tick(act=False)
    assert out["budget"]["lanes_not_run"] == [k for k, _n, _h, _f in shell._LANES]
    assert out["summary"]["?"] == len(shell._LANES) and out["summary"]["PASS"] == 0
    assert out["tick_failed"] is True, (
        "a tick that measured NOTHING must beat status=error, not success")
    for ln in out["lanes"]:
        assert ln["verdict"] == "?"
        assert "budget" in ln["checks"][0]["detail"]



def _blind_but_for_the_db(shell, monkeypatch):
    monkeypatch.setattr(shell, "_gh", lambda *a, **k: (None, "no GitHub token in this test — unverified, NOT assumed fine"))
    monkeypatch.setattr(shell, "_read", lambda *a, **k: "")
    monkeypatch.setattr(shell, "_module", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_import_attr", lambda *a, **k: None)


def test_a_hanging_database_is_dialled_ONCE_per_tick_not_once_per_read(
        shell, monkeypatch):
    """★ THE 128s READ ON A 15s ROUTE. Measured against the shipped shell.

    _q() fell back to _conn() whenever the connection it was handed was None —
    and all 16 call sites hand it ctx["conn"], which is None precisely when
    _conn() has just failed. A Neon that HANGS rather than refuses therefore
    made one read into 13 fresh connect attempts: 3.30s at a 0.25s stall, i.e.
    ~66s at the shipped connect_timeout, on a route worker.js gives 15s before
    it retries it, answers 503, and reads the 5xx from Railway as a dead origin
    — failing the whole site over to the stale Render mirror. The board becomes
    the outage it exists to detect.
    """
    calls = []

    def hanging():
        calls.append(1)
        time.sleep(0.05)          # hangs, then gives up — what connect_timeout does
        return None
    monkeypatch.setattr(shell, "_conn", hanging)
    _blind_but_for_the_db(shell, monkeypatch)

    out = shell._tick(act=False)
    assert len(calls) == 1, (
        f"a hanging database was dialled {len(calls)}x inside ONE read "
        f"(~{len(calls) * shell._CONNECT_TIMEOUT_S}s at the shipped "
        f"connect_timeout, on a 15s route) — _q() must never open a connection")
    assert out["summary"]["PASS"] == 0 and out["summary"]["?"] == len(shell._LANES)
    assert out["db"] is False

    # and the fallback cannot come back without this failing
    q = next(n for n in ast.walk(ast.parse(_src()))
             if isinstance(n, ast.FunctionDef) and n.name == "_q")
    assert not [n for n in ast.walk(q) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "_conn"], (
        "_q() opens its own connection again — one unreachable DB becomes 16 "
        "connect attempts, and 16 more connections against a saturated pool")


def test_a_slow_github_read_cannot_outlive_the_budget(shell, monkeypatch):
    """★ The THIRD half: the DB is fine and api.github.com is slow.

    _q() composed with the budget because it refuses to START under
    _QUERY_MIN_S == statement_timeout. _gh() consulted no deadline at all, and
    it is the most expensive call in the tick — _TIMEOUT = 6s, twice the DB
    bound. So a GitHub read could START at deadline-e and run six seconds past
    it: READ_BUDGET_S + _TIMEOUT = 17s against worker.js's 15s, on a route
    whose own 5xx fails the site over to the stale Render mirror.

    Measured on prod 2026-08-23 before this fix: tick_ms=16102 on the first hit
    after an idle gap; 8665/9777/10141/9263ms warm. Warm ticks already sat
    inside ONE GitHub timeout of the edge, so a slow (not failing)
    api.github.com was sufficient — no outage required.
    """
    # the arithmetic, on the SHIPPED constants, before this test shrinks them
    assert shell._GH_MIN_S >= shell._TIMEOUT, (
        "a GitHub read may only start with a full _TIMEOUT of budget left, or "
        "the one it starts finishes AFTER the deadline")
    assert shell.READ_BUDGET_S + shell._STATEMENT_TIMEOUT_S <= 15, (
        "READ_BUDGET_S plus the longest read still IN FLIGHT at the deadline "
        "must sit inside worker.js ROUTE_TIMEOUTS.DEFAULT")

    started = []

    def _slow_get(url, **kw):
        started.append(url)
        time.sleep(0.4)                    # api.github.com, degraded not down
        raise AssertionError("unreachable in this test")

    def _install(gh_min):
        started.clear()
        monkeypatch.setattr(shell, "_conn", lambda: None)
        monkeypatch.setattr(shell, "_q", lambda *a, **k: None)
        monkeypatch.setattr(shell, "_read", lambda *a, **k: "")
        monkeypatch.setattr(shell, "_module", lambda *a, **k: None)
        monkeypatch.setattr(shell, "_import_attr", lambda *a, **k: None)
        monkeypatch.setattr(shell, "_gh_token", lambda: "t0ken")
        monkeypatch.setattr(shell.requests, "get", _slow_get)
        monkeypatch.setattr(shell, "_TIMEOUT", 0.4)
        monkeypatch.setattr(shell, "_GH_MIN_S", gh_min)

    # MUST-DEGRADE FIRST: with the pre-gate DISABLED the call is made even with
    # no budget left. This is the shipped-before-today behaviour, and it is what
    # makes the assertion below a real one rather than a vacuous pass.
    # ★ -1e9, not 0.0: a spent budget is NEGATIVE, so a gate at 0.0 still fires
    #   and the control would pass by accident of the very guard it exists to
    #   switch off.
    _install(gh_min=-1e9)
    ctx = {"conn": None, "deadline": time.monotonic() - 5.0}   # budget long gone
    val, why = shell._gh("/repos/x/y/actions/workflows/w/runs", ctx=ctx)
    assert len(started) == 1, (
        "CONTROL FAILED: with _GH_MIN_S=0 the read should still be attempted — "
        "if it is not, the test below proves nothing")

    # THE GUARD: at the shipped gate, a spent budget means the call is never made
    _install(gh_min=float(shell._TIMEOUT))
    ctx = {"conn": None, "deadline": time.monotonic() - 5.0}
    val, why = shell._gh("/repos/x/y/actions/workflows/w/runs", ctx=ctx)
    assert started == [], (
        f"a GitHub read was STARTED with the budget spent: {started} — it can "
        f"outlive the deadline by its whole {shell._TIMEOUT}s timeout")
    assert val is None
    # ★ and it says WHICH. Blaming a missing credential for a spent budget is
    #   the #3093 defect: it sends a reader to Railway's env vars to look for a
    #   token that is already there.
    assert "budget" in why and "token" not in why.split("needs")[0], (
        f"the refusal must name the budget, not a credential: {why!r}")


def test_a_github_failure_never_reports_itself_as_a_missing_token(shell, monkeypatch):
    """★ One hard-coded cause for five real ones (#3093's class, in lane 2).

    _gh() returned a bare None for: no token, non-200, timeout, transport
    error, bad JSON. b_digest_run published ONE reason for all five —
    "no GitHub token available (prod has PR_SUBMIT_TOKEN / GITHUB_TOKEN)".
    403/429 IS the rate limit and is by far the likeliest of the five, so the
    shell's standing advice was to go hunting a production credential that was
    never missing.
    """
    monkeypatch.setattr(shell, "_gh_token", lambda: "t0ken")

    class _R:
        status_code = 403

        def json(self):
            return {}

    monkeypatch.setattr(shell.requests, "get", lambda url, **kw: _R())
    val, why = shell._gh("/repos/x/y/z")
    assert val is None
    assert "403" in why, f"the status must be named: {why!r}"
    assert "token IS present" in why, (
        f"a rate limit must not read as a missing credential: {why!r}")

    # and the no-token case still says so, distinctly
    monkeypatch.setattr(shell, "_gh_token", lambda: "")
    val, why = shell._gh("/repos/x/y/z")
    assert val is None and "no GitHub token" in why


def test_a_hanging_query_cannot_spend_more_than_the_read_budget(shell, monkeypatch):
    """★ The other half: the connect SUCCEEDED and now every query hangs.

    The deadline was consulted only between lanes and at two in-lane points —
    lane 2 alone fires four back-to-back reads with nothing checked between
    them, so a stalled Neon overran the budget by whatever a lane costs. Every
    read now asks how much is left before it spends any, and refuses below one
    statement_timeout, so a read that IS started still lands inside the window.
    """
    # the arithmetic that makes READ_BUDGET_S a ceiling rather than a wish —
    # asserted on the SHIPPED constants, before this test shrinks them
    assert shell._QUERY_MIN_S >= shell._STATEMENT_TIMEOUT_S, (
        "a read may only start with a full statement_timeout of budget left, "
        "or the one it starts finishes AFTER the deadline")
    assert shell._CONNECT_TIMEOUT_S + shell._STATEMENT_TIMEOUT_S <= shell.READ_BUDGET_S
    started = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if sql.split()[0].upper() in ("BEGIN", "COMMIT", "ROLLBACK", "SET"):
                return
            started.append(sql[:40])
            time.sleep(0.4)               # a query Postgres has not killed yet

        def fetchall(self):
            return [(0, 0)]

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    monkeypatch.setattr(shell, "_conn", lambda: _Conn())
    _blind_but_for_the_db(shell, monkeypatch)
    monkeypatch.setattr(shell, "READ_BUDGET_S", 1.0)
    monkeypatch.setattr(shell, "_QUERY_MIN_S", 0.35)

    t = time.monotonic()
    out = shell._tick(act=False)
    elapsed = time.monotonic() - t

    # DETERMINISTIC: 1.0s of budget at 0.4s a read, refusing below 0.35s left,
    # is two reads. Without the check the lanes fire every read they reach.
    assert len(started) <= 3, (
        f"{len(started)} reads were STARTED inside a {shell.READ_BUDGET_S}s "
        f"budget at 0.4s each: {started}")
    assert elapsed < 2 * shell.READ_BUDGET_S, (
        f"the read took {elapsed:.2f}s against its own {shell.READ_BUDGET_S}s "
        f"budget — worker.js retries at 15s and then 503s, and a 5xx from "
        f"Railway fails the site over to stale Render")
    assert out["summary"]["PASS"] == 0


def test_every_read_runs_under_a_statement_timeout_postgres_can_enforce(shell):
    """Python cannot abort a psycopg2 call in flight; Postgres can. Pinned on
    the SET LOCAL form specifically: on Neon's pooled endpoint pgbouncer
    rejects startup options at connect and a plain session SET lands on a
    different backend than the query (flask_mcp_endpoints._reach_bounded,
    verified live 2026-07-01), so both of the cheaper spellings are no-ops."""
    seen = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            seen.append((sql, params))

        def fetchall(self):
            return [(1,)]

    class _Conn:
        def cursor(self):
            return _Cur()

    rows = shell._q("SELECT 1", conn=_Conn())
    assert rows == [(1,)]
    verbs = [s.split()[0].upper() + (" LOCAL" if s.upper().startswith("SET LOCAL") else "")
             for s, _p in seen]
    assert verbs[:2] == ["BEGIN", "SET LOCAL"] and verbs[-1] == "COMMIT", verbs
    assert f"statement_timeout = {int(shell._STATEMENT_TIMEOUT_S * 1000)}" in seen[1][0]
    # no params -> psycopg2 must be handed None, never () (the repo's literal-% 500)
    assert seen[2] == ("SELECT 1", None)
    assert shell._q("SELECT 1", conn=None) is None, "no connection is an unreadable read"


def test_part_bs_own_reader_is_bounded_like_every_other_read(shell, monkeypatch):
    """★ "Deadline-bounded on EVERY path" has to mean every path.

    _registry_rows does not run a SQL string of ours — it hands this tick's
    cursor to part B's class_rows(cur). That read sat outside the envelope: no
    deadline consulted, no statement_timeout, so a wedged brain_action_classes
    blocked the lane for as long as the server allowed.
    """
    seen = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            seen.append(sql)

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    _attr_router(monkeypatch, shell, {"class_rows": lambda cur: [{"class": "x"}]})
    rows, why = shell._registry_rows({"conn": _Conn()})
    assert rows == [{"class": "x"}] and why == "ok"
    verbs = [s.split()[0].upper() + (" LOCAL" if s.upper().startswith("SET LOCAL") else "")
             for s in seen]
    assert verbs == ["BEGIN", "SET LOCAL", "COMMIT"], verbs
    assert f"statement_timeout = {int(shell._STATEMENT_TIMEOUT_S * 1000)}" in seen[1]

    # and it refuses to START once the budget is gone — `?`, never a blank pass
    spent = {"conn": _Conn(), "deadline": time.monotonic() - 1}
    rows, why = shell._registry_rows(spent)
    assert rows is None and "budget was spent" in why

    # no connection is still an unreadable read, not a re-dial
    monkeypatch.setattr(shell, "_conn", lambda: pytest.fail("_registry_rows dialled"))
    rows, why = shell._registry_rows({"conn": None})
    assert rows is None and why == "no DB connection"


def test_kill_switch_never_returns_5xx_and_states_a_code():
    tree = ast.parse(_src())
    codes = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and isinstance(node.test.func, ast.Name) and node.test.func.id == "_disabled"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Tuple):
                    for el in sub.value.elts:
                        if isinstance(el, ast.Constant) and isinstance(el.value, int):
                            codes.append(el.value)
    assert len(codes) == 3, f"EXTRACTION: expected 3 route kill guards, found {codes}"
    assert all(c == 404 for c in codes), f"kill switch returns {codes}; must be 404"


def test_kill_switch_is_honoured_on_every_route(shell, monkeypatch):
    monkeypatch.setenv("AGENTIC_LOOP_SHELL_DISABLE", "1")
    assert shell._disabled() is True
    monkeypatch.setattr(shell, "_tick", lambda act: pytest.fail("a disabled shell ran its tick"))
    beats = []
    monkeypatch.setattr(shell, "_beat_ledger", lambda ok, note: beats.append(ok))
    c = _client(shell, monkeypatch)
    hdr = {"X-Admin-Key": "test-admin-key"}
    assert c.get("/api/v1/brain/agentic-loop", headers=hdr).status_code == 404
    assert c.get("/admin/agentic-loop", headers=hdr).status_code == 404
    assert c.post(TICK_ROUTE, headers=hdr).status_code == 404
    assert beats == [], "a disabled shell must not beat"
    monkeypatch.delenv("AGENTIC_LOOP_SHELL_DISABLE")
    assert shell._disabled() is False


def test_admin_gate_runs_before_anything_acts(shell, monkeypatch):
    monkeypatch.setattr(shell, "_tick", lambda act: pytest.fail("the tick ran without an admin key"))
    beats = []
    monkeypatch.setattr(shell, "_beat_ledger", lambda ok, note: beats.append(ok))
    c = _client(shell, monkeypatch)
    assert c.get("/api/v1/brain/agentic-loop").status_code == 401
    assert c.get("/admin/agentic-loop").status_code == 401
    assert c.post(TICK_ROUTE).status_code == 401
    assert beats == []


def test_every_lane_is_wrapped_so_one_failure_cannot_5xx_the_tick(shell, monkeypatch):
    tree = ast.parse(_src())
    tick = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_tick"]
    assert tick, "EXTRACTION EMPTY: _tick not found"
    assert any(isinstance(n, ast.Try) for n in ast.walk(tick[0]))

    def _boom(ctx):
        raise RuntimeError("lane exploded")

    def _fine(ctx):
        return [shell._check("ok", "fine", True, "ok", critical=True)]
    monkeypatch.setattr(shell, "_conn", lambda: None)
    monkeypatch.setattr(shell, "_import_attr", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_LANES", (("9", "boom", "h", _boom), ("8", "fine", "h", _fine)))
    out = shell._tick(act=False)
    by = {ln["lane"]: ln["verdict"] for ln in out["lanes"]}
    assert by == {"9": "?", "8": "PASS"}
    assert out["tick_failed"] is False          # one lane raised, one measured
    monkeypatch.setattr(shell, "_LANES", (("9", "boom", "h", _boom),))
    assert shell._tick(act=False)["tick_failed"] is True   # EVERY lane raised


# ── the GET never acts, never beats; the POST beats honestly ─────────────

def _quiet(shell, monkeypatch):
    """A tick with nothing to read and spies on every write path."""
    spies = {"beat": [], "beat_status": [], "ensure": [], "snapshot": [], "filing": []}
    monkeypatch.setattr(shell, "_conn", lambda: None)
    monkeypatch.setattr(shell, "_import_attr", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_LANES", (("1", "x", "h", lambda ctx: [
        shell._check("a", "read", True, "ok", critical=True)]),))
    # ★2026-09-01: records the STATUS WORD as well as ok. The beat now carries
    # three states (success / lanes_failing / error) because `error` is what
    # ingestion-integrity-tick's producer_liveness lane treats as a BROKEN
    # producer — a spy that only saw the boolean could not tell a red board from
    # a crashed one, which is exactly the distinction that was lost.
    monkeypatch.setattr(shell, "_beat_ledger",
                        lambda ok, note, status=None: (spies["beat"].append(ok),
                                                       spies["beat_status"].append(status))[0])
    monkeypatch.setattr(shell, "_ensure_ledger", lambda ctx: spies["ensure"].append(1) or True)
    monkeypatch.setattr(shell, "_ledger_add",
                        lambda ctx, kind, n, payload: spies["snapshot"].append(kind) or True)
    real_filing = shell._armed_filing
    monkeypatch.setattr(shell, "_armed_filing",
                        lambda ctx: spies["filing"].append(1) or real_filing(ctx))
    return spies


def test_get_json_and_board_never_write_or_beat(shell, monkeypatch):
    spies = _quiet(shell, monkeypatch)
    c = _client(shell, monkeypatch)
    hdr = {"X-Admin-Key": "test-admin-key"}
    r = c.get("/api/v1/brain/agentic-loop", headers=hdr)
    assert r.status_code == 200 and r.get_json()["acted"] is False
    assert "no-store" in r.headers.get("Cache-Control", "")
    b = c.get("/admin/agentic-loop", headers=hdr)
    assert b.status_code == 200 and b"Agentic loop master shell #65" in b.data
    assert spies == {"beat": [], "beat_status": [], "ensure": [], "snapshot": [],
                     "filing": []}, ("a GET wrote or beat: %r" % spies)


def test_post_tick_writes_its_snapshot_and_beats_success(shell, monkeypatch):
    spies = _quiet(shell, monkeypatch)
    c = _client(shell, monkeypatch)
    r = c.post(TICK_ROUTE, headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["acted"] is True and j["snapshot"]["written"] is True
    assert spies["snapshot"] == ["tick"] and spies["filing"] == [1]
    assert spies["beat"] == [True]
    assert j["graduation_filing"]["armed"] is False and j["graduation_filing"]["filed"] == 0


def test_post_tick_beats_error_when_every_lane_raised_and_never_5xx(shell, monkeypatch):
    spies = _quiet(shell, monkeypatch)

    def _boom(ctx):
        raise RuntimeError("x")
    monkeypatch.setattr(shell, "_LANES", (("1", "x", "h", _boom),))
    c = _client(shell, monkeypatch)
    r = c.post(TICK_ROUTE, headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200, "a failed tick must not 5xx (CF fails the site over)"
    assert r.get_json()["tick_failed"] is True
    assert spies["beat"] == [False]
    # ★2026-09-01: this test is named "beats_error" and used to assert only
    # ok is False, which no longer distinguishes error from lanes_failing.
    # EVERY lane raised here, so nothing was measured — that is a broken
    # producer and must still be `error`, not the red-board word.
    assert spies["beat_status"] == ["error"]
    # and a tick that RAISES outright still beats error, still 200
    monkeypatch.setattr(shell, "_tick", lambda act: (_ for _ in ()).throw(RuntimeError("tick")))
    r = c.post(TICK_ROUTE, headers={"X-Admin-Key": "test-admin-key"})
    assert r.status_code == 200 and spies["beat"] == [False, False]
    assert spies["beat_status"] == ["error", "error"]


def test_beat_body_is_the_house_shape_and_error_is_never_warn(shell, monkeypatch):
    sent = []
    monkeypatch.setattr(shell.requests, "post",
                        lambda url, data=None, timeout=None, headers=None: sent.append((url, json.loads(data), headers)))
    shell._beat_ledger(True, "n")
    shell._beat_ledger(False, "n")
    assert len(sent) == 2
    url, ok_body, hdr = sent[0]
    assert url.endswith("/api/v1/admin/ingest-runs/beat")
    assert ok_body["feed"] == FEED and ok_body["status"] == "success"
    # ★ batch-3/Screen D: this used to send `1 if ok else 0`. BOTH values are
    # fabrications — a shell inserts no rows — and the 0 on the error path
    # additionally CLIMBS ingest_runs' consecutive-zero counter toward a
    # second, unrelated alarm. record_beat() leaves that counter untouched
    # when the field is absent, so the house shape for a shell OMITS it.
    assert "rows_inserted" not in ok_body and ok_body["cadence_hours"] == 24
    assert hdr["User-Agent"].startswith("dchub-")
    assert sent[1][1]["status"] == "error" and "rows_inserted" not in sent[1][1]
    assert "warn" not in {sent[0][1]["status"], sent[1][1]["status"]}

    def _raise(*a, **k):
        raise RuntimeError("loopback down")
    monkeypatch.setattr(shell.requests, "post", _raise)
    shell._beat_ledger(True, "n")       # must never raise into the tick


def test_the_feed_has_exactly_one_writer():
    """A feed with two writers reads alive while either is dead."""
    writers = []
    for path in glob.glob(os.path.join(ROOT, "routes", "*.py")) + glob.glob(os.path.join(ROOT, "*.py")):
        raw = _src(path)
        if FEED not in raw:
            continue
        try:
            tree = ast.parse(raw)
        except SyntaxError:
            continue
        if any(isinstance(n, ast.Constant) and n.value == FEED for n in ast.walk(tree)):
            writers.append(os.path.relpath(path, ROOT))
    assert writers == ["routes/agentic_loop_master_shell.py"], writers


# ── lane 1: graduation on track record (critical checks mutation-tested) ──

def _class_row(cls, **over):
    row = {"class": cls, "granted": True, "reversible": True,
           "verifier_url": "/api/v1/admin/x/count", "bound_params": {"country": "NL"},
           "breaker_tripped": False, "consecutive_failed": 0}
    row.update(over)
    return row


def test_a_granted_class_that_fails_grant_allowed_is_red_and_a_passing_one_is_green(shell, monkeypatch):
    sac = pytest.importorskip("routes.squasher_action_classes")   # the REAL gate
    cls = next(iter(sac.ACTION_CLASSES))
    monkeypatch.setattr(shell, "_graduation_report", lambda **k: (None, "absent"))
    monkeypatch.setattr(shell, "_q", lambda *a, **k: [])
    # control: a granted row that the real gate accepts
    monkeypatch.setattr(shell, "_registry_rows", lambda ctx: ([_class_row(cls)], "ok"))
    checks = shell._lane_graduation({"conn": object()})
    gate = next(c for c in checks if c["id"] == "a_granted_pass_gate")
    assert gate["pass"] is True and gate["critical"] is True
    # MUST-FAIL: the same row granted but not reversible
    monkeypatch.setattr(shell, "_registry_rows",
                        lambda ctx: ([_class_row(cls, reversible=False)], "ok"))
    checks = shell._lane_graduation({"conn": object()})
    gate = next(c for c in checks if c["id"] == "a_granted_pass_gate")
    assert gate["pass"] is False and "reversible" in gate["detail"]
    assert shell._lane_verdict(checks) == "FAIL"
    # scope control: an UNGRANTED row failing the gate is a candidate, not a breach
    monkeypatch.setattr(shell, "_registry_rows",
                        lambda ctx: ([_class_row(cls), _class_row(cls + "_x", granted=False, reversible=False)], "ok"))
    checks = shell._lane_graduation({"conn": object()})
    assert next(c for c in checks if c["id"] == "a_granted_pass_gate")["pass"] is True
    assert next(c for c in checks if c["id"] == "a_candidate_exists")["pass"] is True


def test_no_granted_class_means_the_gate_is_unverified_not_green(shell, monkeypatch):
    monkeypatch.setattr(shell, "_graduation_report", lambda **k: (None, "absent"))
    monkeypatch.setattr(shell, "_q", lambda *a, **k: [])
    monkeypatch.setattr(shell, "_registry_rows", lambda ctx: ([_class_row("c", granted=False)], "ok"))
    checks = shell._lane_graduation({"conn": object()})
    gate = next(c for c in checks if c["id"] == "a_granted_pass_gate")
    assert gate["pass"] is None and gate["critical"] is True
    assert shell._lane_verdict(checks) == "?"


def test_breaker_violations_pure_rule_and_its_controls(shell):
    fail = lambda h: ("c", _utc(h), True, False, False)   # noqa: E731
    ok = lambda h: ("c", _utc(h), True, True, False)      # noqa: E731
    dry = lambda h: ("c", _utc(h), True, False, True)     # noqa: E731
    # MUST-FAIL: three failures trip the breaker; a fourth execution is a breach
    assert shell._breaker_violations([fail(0), fail(1), fail(2), fail(3)], 3) == {"c": 1}
    # controls
    assert shell._breaker_violations([fail(0), fail(1), fail(2)], 3) == {"c": 0}       # the trip itself
    assert shell._breaker_violations([fail(0), fail(1), ok(2), fail(3), fail(4), fail(5)], 3) == {"c": 0}  # reset
    assert shell._breaker_violations([fail(0), fail(1), fail(2), dry(3)], 3) == {"c": 0}  # dry runs never count
    assert shell._breaker_violations([("c", _utc(0), False, False, False)] * 4, 3) == {}  # never executed
    assert shell._breaker_violations([fail(3), fail(1), fail(0), fail(2)], 3) == {"c": 1}  # order-independent
    assert shell._breaker_violations([fail(0), fail(1), fail(2), fail(3)], 3,
                                     window_start=_utc(10)) == {"c": 0}                # outside the window


def test_a_tripped_class_executing_after_its_trip_is_red_in_the_lane(shell, monkeypatch):
    monkeypatch.setattr(shell, "_graduation_report", lambda **k: (None, "absent"))
    now = shell._now()
    runs = [("c", now - _dt.timedelta(hours=h), True, False, False) for h in (30, 20, 10)]
    monkeypatch.setattr(shell, "_registry_rows",
                        lambda ctx: ([_class_row("c", breaker_tripped=True)], "ok"))
    # control: tripped, nothing ran after the trip
    _q_router(monkeypatch, shell, {"brain_action_class_runs": runs}, default=[])
    checks = shell._lane_graduation({"conn": object()})
    brk = next(c for c in checks if c["id"] == "a_breaker_no_exec")
    assert brk["pass"] is True and brk["critical"] is True
    # MUST-FAIL: one more execution after the trip, inside 7d
    _q_router(monkeypatch, shell,
              {"brain_action_class_runs": runs + [("c", now - _dt.timedelta(hours=1), True, True, False)]},
              default=[])
    checks = shell._lane_graduation({"conn": object()})
    brk = next(c for c in checks if c["id"] == "a_breaker_no_exec")
    assert brk["pass"] is False and shell._lane_verdict(checks) == "FAIL"
    # unreadable ledger for a tripped class is `?`, never green
    _q_router(monkeypatch, shell, {}, default=None)
    checks = shell._lane_graduation({"conn": object()})
    assert next(c for c in checks if c["id"] == "a_breaker_no_exec")["pass"] is None


def test_eligible_candidate_without_a_decision_row_is_red(shell, monkeypatch):
    rep = {"classes": [{"class": "news_entity_reresolve", "eligible_for_grant": True},
                       {"class": "deals_exact_dupe_quarantine", "eligible_for_grant": False}]}
    monkeypatch.setattr(shell, "_graduation_report", lambda **k: (rep, "ok"))
    monkeypatch.setattr(shell, "_registry_rows", lambda ctx: ([_class_row("news_entity_reresolve", granted=False)], "ok"))
    filed = _inbox([_row(1, title="Grant action class news_entity_reresolve?",
                         cls="news_entity_reresolve",
                         key="action-class-grant:news_entity_reresolve")])
    _q_router(monkeypatch, shell, {_INBOX_SQL: filed}, default=[])       # control: row exists
    checks = shell._lane_graduation({"conn": object()})
    assert next(c for c in checks if c["id"] == "a_eligible_decision_row")["pass"] is True
    assert next(c for c in checks if c["id"] == "a_report")["pass"] is True
    _q_router(monkeypatch, shell, {_INBOX_SQL: []}, default=[])          # MUST-FAIL: silently waiting
    checks = shell._lane_graduation({"conn": object()})
    ed = next(c for c in checks if c["id"] == "a_eligible_decision_row")
    assert ed["pass"] is False and "news_entity_reresolve" in ed["detail"]
    # an UNREADABLE queue is `?`, never "silently waiting" and never fine
    _q_router(monkeypatch, shell, {_INBOX_SQL: None}, default=[])
    checks = shell._lane_graduation({"conn": object()})
    ed = next(c for c in checks if c["id"] == "a_eligible_decision_row")
    assert ed["pass"] is None and "unreadable" in ed["detail"]
    assert shell._eligible_classes(None) is None
    assert shell._eligible_classes({"a": {"eligible_for_grant": True}}) == ["a"]
    assert shell._eligible_classes([{"class": "b", "eligible_for_grant": 1}]) == ["b"]


def test_lane_1_does_not_report_zero_off_a_graduation_report_that_failed(
        shell, monkeypatch):
    """★ Part B answers, it does not raise. Probed with its real shape:

        PASS  a_report               0 class(es) reported; 0 eligible
        PASS  a_eligible_decision_row  no class is eligible for a grant yet —
                                       nothing can be silently waiting

    while graduation_report() had in fact failed to read the database. Once
    part B seeds candidates (so a_candidate_exists goes green) that is lane 1
    rendering FULLY GREEN having reported nothing. `0 eligible` is a claim; an
    unreadable report cannot make it.
    """
    def failsoft(file: bool = False, max_file: int = 3, by: str = "graduation"):
        return {"known": False, "error": "OperationalError: could not connect"}
    _attr_router(monkeypatch, shell, {"graduation_report": failsoft})
    monkeypatch.setattr(shell, "_registry_rows",
                        lambda ctx: ([_class_row("news_entity_reresolve", granted=False)], "ok"))
    _q_router(monkeypatch, shell, {}, default=[])
    by = {c["id"]: c for c in shell._lane_graduation({"conn": object()})}
    assert by["a_report"]["pass"] is None, (
        "a graduation_report() that failed to read was reported as read: "
        + by["a_report"]["detail"])
    assert by["a_eligible_decision_row"]["pass"] is None, (
        "'no class is eligible for a grant yet' was claimed off an unreadable "
        "report: " + by["a_eligible_decision_row"]["detail"])
    assert shell._lane_verdict(list(by.values())) != "PASS"

    # CONTROL: the SAME shape with the flag true is a real, empty report
    def real(file: bool = False, max_file: int = 3, by: str = "graduation"):
        return {"known": True, "classes": []}
    _attr_router(monkeypatch, shell, {"graduation_report": real})
    by = {c["id"]: c for c in shell._lane_graduation({"conn": object()})}
    assert by["a_report"]["pass"] is True and by["a_eligible_decision_row"]["pass"] is True


# ── graduation_report(): a writer is never called from a read ────────────

def test_the_daily_file_cap_binds_part_bs_REAL_signature(shell, monkeypatch):
    """★ THE BUDGET GUARD WAS ONLY EVER EXERCISED AGAINST INVENTED SIGNATURES.

    Both existing tests stub `report(dry_run=False, limit=None)`. Part B ships
    `graduation_report(file=False, max_file=3, by="graduation")` (PR #3073), and
    `max_file` was not in _BUDGET_PARAMS. Measured against that exact signature:
    the shell called it with {'file': True, 'max_file': 3, 'by': 'graduation'}
    while its own remaining budget was 1 — so with 2 rows already ledgered today
    an armed tick files 3 more = 5 inbox rows on a day whose declared ceiling is
    3, and the shell notices only afterwards (over_budget=True). A budget that
    does not reach the callee is a receipt, not a cap.
    """
    seen = {}

    def graduation_report(file: bool = False, max_file: int = 3, by: str = "graduation"):
        seen.clear()
        seen.update(file=file, max_file=max_file, by=by)
        return {"filed": max_file}
    _attr_router(monkeypatch, shell, {"graduation_report": graduation_report})

    rep, why = shell._graduation_report(file_rows=True, budget=1)
    assert why == "ok" and seen["file"] is True
    assert seen["max_file"] == 1, (
        f"the shell's remaining daily budget never reached part B: {seen} — "
        f"it will file {seen['max_file']} rows against a budget of 1")

    # the read path still switches filing OFF and spends no budget
    seen.clear()
    shell._graduation_report(file_rows=False)
    assert seen["file"] is False and seen["max_file"] == 3

    # end to end through the shell's own ledger: 2 filed today -> part B capped at 1
    monkeypatch.setenv("AGENTIC_LOOP_ARM", "1")
    monkeypatch.setattr(shell, "_filed_today", lambda ctx: 2)
    monkeypatch.setattr(shell, "_ledger_add", lambda ctx, kind, n, payload: True)
    seen.clear()
    r = shell._armed_filing({"conn": object()})
    assert seen["max_file"] == shell.FILE_CAP_PER_DAY - 2 == 1, seen
    assert r["filed"] == 1 and r["over_budget"] is False
    assert "max_file" in shell._BUDGET_PARAMS


def test_read_path_refuses_a_graduation_report_it_cannot_switch_to_dry_run(shell, monkeypatch):
    calls = []

    def report():
        calls.append("FILED")
        return {"classes": []}
    _attr_router(monkeypatch, shell, {"graduation_report": report})
    rep, why = shell._graduation_report(file_rows=False)
    assert rep is None and "NOT called" in why and calls == [], (
        "the read path called a filing-only graduation_report()")
    # control: a dry_run parameter lets the read call it with filing OFF
    seen = {}

    def report2(dry_run=False, limit=None):
        seen.update(dry_run=dry_run, limit=limit)
        return {"classes": [{"class": "x", "eligible_for_grant": True}]}
    _attr_router(monkeypatch, shell, {"graduation_report": report2})
    rep, why = shell._graduation_report(file_rows=False)
    assert why == "ok" and seen == {"dry_run": True, "limit": None}
    assert shell._eligible_classes(rep) == ["x"]
    # the armed tick flips it ON and passes the budget
    rep, why = shell._graduation_report(file_rows=True, budget=2)
    assert seen == {"dry_run": False, "limit": 2}
    # absent (part B not landed) is `?`, never called
    _attr_router(monkeypatch, shell, {})
    rep, why = shell._graduation_report(file_rows=False)
    assert rep is None and "part B" in why


def test_armed_filing_is_bounded_by_the_ledger_and_never_runs_unarmed(shell, monkeypatch):
    calls, added = [], []

    def report(dry_run=False, limit=None):
        calls.append((dry_run, limit))
        return {"filed": limit}
    _attr_router(monkeypatch, shell, {"graduation_report": report})
    monkeypatch.setattr(shell, "_ledger_add", lambda ctx, kind, n, payload: added.append((kind, n)) or True)
    ctx = {"conn": object()}
    # unarmed: never called, whatever the budget
    monkeypatch.delenv("AGENTIC_LOOP_ARM", raising=False)
    monkeypatch.setattr(shell, "_filed_today", lambda ctx: 0)
    r = shell._armed_filing(ctx)
    assert r["armed"] is False and calls == [] and added == []
    monkeypatch.setenv("AGENTIC_LOOP_ARM", "1")
    # cap reached: not called
    monkeypatch.setattr(shell, "_filed_today", lambda ctx: shell.FILE_CAP_PER_DAY)
    r = shell._armed_filing(ctx)
    assert r["filed"] == 0 and r["budget"] == 0 and calls == []
    # ledger unreadable = NO budget
    monkeypatch.setattr(shell, "_filed_today", lambda ctx: None)
    r = shell._armed_filing(ctx)
    assert calls == [] and "NOT filing" in r["note"]
    # one already filed today: called once with the remaining budget, ledgered
    monkeypatch.setattr(shell, "_filed_today", lambda ctx: 1)
    r = shell._armed_filing(ctx)
    assert calls == [(False, shell.FILE_CAP_PER_DAY - 1)]
    assert r["filed"] == shell.FILE_CAP_PER_DAY - 1 and r["over_budget"] is False
    assert added == [("graduation_file", shell.FILE_CAP_PER_DAY - 1)]
    assert shell.FILE_CAP_PER_DAY == 3


def test_filed_count_is_conservative_when_the_report_does_not_say(shell):
    assert shell._filed_count({"filed": 2}, fallback=3) == 2
    assert shell._filed_count({"filed": ["a", "b"]}, fallback=3) == 2
    assert shell._filed_count({"classes": []}, fallback=3) == 3      # unreported write spends the budget
    assert shell._filed_count(None, fallback=1) == 1


# ── PARTIAL blindness: one True beside an unreadable queue is not health ─

def _lane_critical_ids(fn_name: str) -> set:
    """Check ids the SHIPPED source declares critical=True inside one lane."""
    tree = ast.parse(_src())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and getattr(sub.func, "id", None) == "_check"
                        and sub.args and isinstance(sub.args[0], ast.Constant)
                        and any(k.arg == "critical"
                                and getattr(k.value, "value", None) is True
                                for k in sub.keywords)):
                    out.add(sub.args[0].value)
    return out


def test_every_lane_declares_a_critical_check_so_none_can_pass_by_fallback():
    """★ THE STRUCTURAL HOLE, pinned so it cannot reopen in any lane.

    _lane_verdict has two ways to reach `?`: a critical check that is None, or
    the fallback — None somewhere and NOTHING True anywhere. A lane with zero
    critical checks has only the fallback, so a SINGLE accidental True beside
    total blindness renders PASS. Lane 2 shipped exactly like that: `_check`
    was called 14 times in _lane_human_queues and not once with critical=True.
    """
    from routes import agentic_loop_master_shell as m
    bare = [f.__name__ for _k, _n, _h, f in m._LANES
            if not _lane_critical_ids(f.__name__)]
    assert not bare, (
        f"{bare} declare NO critical=True check. A lane with none can only "
        f"reach `?` when nothing at all was True, so one incidental green "
        f"renders PASS over an unreadable board — the green-by-silence class "
        f"this shell exists to detect.")
    # ★ AND EACH LANE'S GATEWAY READ BY NAME. "at least one critical" is not
    #   enough on its own: lanes 1 and 4 carry several, so deleting the flag
    #   from the ONE read everything else depends on left the whole suite green
    #   (in the all-blind case those lanes return only None checks and reach `?`
    #   through the fallback anyway). Pin the gateways.
    assert _lane_critical_ids("_lane_graduation") >= {"a_read"}, (
        "the action-class registry read gates every other check in lane 1")
    assert _lane_critical_ids("_lane_human_queues") >= {
        "b_oldest_decision", "b_platform_items", "b_stale_recs_named"}, (
        "lane 2 says 'every human queue has an age, a ceiling and a one-click "
        "decision' — the decision inbox, the platform feed and the strategic "
        "recs must each block a PASS when they cannot be read")
    assert _lane_critical_ids("_lane_learn") >= {"c_corpus_registered"}
    assert _lane_critical_ids("_lane_detectors") >= {"d_convergence_read"}


def test_lane_2_cannot_read_pass_while_the_queues_it_covers_are_unreadable(
        shell, monkeypatch):
    """★ REPRODUCED, not inferred (2026-08-22, against the shipped lane).

    DB down, no GitHub token, part B deployed but its queue_ages() fail-softing
    to {"known": False, "error": "OperationalError: too many connections"}:

        True b_queue_ages   None b_oldest_decision   None b_platform_items
        None b_stale_recs_named   None b_digest_run  None b_collapse_ratio
        -> VERDICT: PASS

    The board printed 'every human queue has an age, a ceiling and a one-click
    decision: PASS' during a total database outage, with part B's own error
    string as the DETAIL of the pass.

    test_every_lane_with_every_read_failed_… cannot catch this: it blinds
    _import_attr wholesale, so queue_ages is ABSENT and every check is None,
    and the lane reaches `?` through the fallback rule. It takes ONE True
    beside the blindness — which is what a fail-softing sibling supplies.
    """
    failsoft = {"known": False, "error": "OperationalError: too many connections"}
    by = _lane2(shell, monkeypatch, ages=failsoft)
    assert by["b_queue_ages"]["pass"] is None, (
        "part B's fail-soft envelope was read as a successful queue_ages(): "
        + by["b_queue_ages"]["detail"])
    assert "known=false" in by["b_queue_ages"]["detail"]
    assert shell._lane_verdict(list(by.values())) != "PASS"

    # the sharper shape: a queue_ages() that IS readable but carries no
    # awaiting_decision age, so exactly one check is True and every queue the
    # lane covers is unreadable behind it.
    by = _lane2(shell, monkeypatch, ages={"awaiting_ops": {"oldest_age_hours": 3}})
    assert by["b_queue_ages"]["pass"] is True                      # the one True
    for cid in ("b_oldest_decision", "b_platform_items", "b_stale_recs_named"):
        assert by[cid]["pass"] is None, cid
    v = shell._lane_verdict(list(by.values()))
    assert v == "?", (
        f"lane 2 rendered {v} with one incidental True and every queue it "
        f"covers unreadable: {[(c['id'], c['pass']) for c in by.values()]}")

    # CONTROL — the same lane still goes green when the queues really ARE read
    by = _lane2(
        shell, monkeypatch,
        ages={"awaiting_decision": {"count": 1, "oldest_age_hours": 2}},
        updates={"ok": True, "withheld": []},
        q={"COUNT(*), MIN(created_at) FROM brain_strategic_recommendations": [(0, None)],
           "COUNT(*), COUNT(DISTINCT": [(4, 2)]},
        gh={"workflow_runs": [{"conclusion": "success",
                               "created_at": shell._now().isoformat()}]})
    assert shell._lane_verdict(list(by.values())) == "PASS", (
        "the criticals must not pin the lane at `?` when every queue read: "
        + str([(c["id"], c["pass"]) for c in by.values()]))


def test_a_sibling_fail_soft_envelope_is_an_unreadable_read_never_a_pass(shell):
    """Parts B and C ANSWER instead of raising when their own read fails. The
    flag is the only thing separating 'nothing is pending' from 'I could not
    look', and the payload beside it is identical."""
    r = shell._readable
    for envelope in ({"known": False, "error": "OperationalError"},
                     {"ok": False, "reason": "platform updates unavailable"},
                     {"ok": False, "cards": [], "withheld": [], "withheld_count": 0}):
        v, why = r(envelope)
        assert v is None, f"{envelope} was read as a successful call"
        assert "NOT assumed fine" in why
    # a real read is passed straight through, flag or no flag
    assert r({"ok": True, "withheld": []})[0] == {"ok": True, "withheld": []}
    assert r({"known": True, "rows": []})[0] == {"known": True, "rows": []}
    assert r({"withheld": []})[0] == {"withheld": []}
    assert r([1, 2])[0] == [1, 2]
    assert r(None, "no db") == (None, "no db")
    # `ok`/`known` must both be honoured — one alone leaves half the siblings open
    assert shell._FAILSOFT_FLAGS == ("known", "ok")


# ── the decision row is found by the identity part B files it under ──────

def test_the_decision_row_title_arm_matched_a_title_nothing_ever_files(shell):
    """★ THE FALLBACK THAT COULD NOT FIRE (2026-08-23).

    The check looked for the substring "Grant class <cls>". Part B files the row
    as "Grant action class <cls>?" — the word `action` sits between them — so
    that arm never matched a row this system has ever written. It read as a
    second safety net behind `action_class`, and file_decision_row's REFRESH
    path does not backfill action_class: on such a row BOTH arms miss and lane 1
    reports a candidate as silently waiting while its decision row is sitting in
    the human's inbox. A false RED on the board written to catch false GREENs.

    RED on the old code: a row carrying ONLY the real title is not found.
    """
    cls = "news_entity_reresolve"
    key_of = lambda c: "action-class-grant:" + c

    title_only = _inbox([_row(1, title="Grant action class %s?" % cls)])
    assert shell._has_decision_row(title_only, cls, key_of) is True, (
        "the title arm does not match the title part B actually files")

    # the arm the old code searched for is not a title anything writes
    assert "Grant class %s" % cls not in title_only[0][shell.I_TITLE]

    # each identity on its own is enough — and the key works with the class NULL
    key_only = _inbox([_row(2, title="anything", key=key_of(cls))])
    assert shell._has_decision_row(key_only, cls, key_of) is True
    class_only = _inbox([_row(3, title="anything", cls=cls)])
    assert shell._has_decision_row(class_only, cls, key_of) is True

    # MUST-FAIL: another class's row is not this class's decision row
    other = _inbox([_row(4, title="Grant action class deals_exact_dupe_quarantine?",
                         cls="deals_exact_dupe_quarantine",
                         key=key_of("deals_exact_dupe_quarantine"))])
    assert shell._has_decision_row(other, cls, key_of) is False

    # MUST-FAIL: an awaiting_ops row is not a row awaiting a DECISION
    ops = _inbox([_row(5, title="Grant action class %s?" % cls, cls=cls,
                       key=key_of(cls), status="awaiting_ops")])
    assert shell._has_decision_row(ops, cls, key_of) is False

    # part B absent: the key arm simply does not contribute, it never raises
    assert shell._has_decision_row(key_only, cls, None) is False
    assert shell._has_decision_row(title_only, cls, None) is True


def test_a_missing_decision_row_names_the_filer_that_never_ran(shell, monkeypatch):
    """A red here is not a mystery: nothing files these rows except part B's
    graduation_report(file=True), and the only caller is the tick under
    AGENTIC_LOOP_ARM=1. Measured on prod 2026-08-23: 2 eligible classes, 0
    decision rows, armed=false."""
    rep = {"classes": [{"class": "news_entity_reresolve", "eligible_for_grant": True}]}
    monkeypatch.setattr(shell, "_graduation_report", lambda **k: (rep, "ok"))
    monkeypatch.setattr(shell, "_registry_rows",
                        lambda ctx: ([_class_row("news_entity_reresolve", granted=False)], "ok"))
    monkeypatch.delenv("AGENTIC_LOOP_ARM", raising=False)
    _q_router(monkeypatch, shell, {_INBOX_SQL: []}, default=[])
    ed = {c["id"]: c for c in shell._lane_graduation({"conn": object()})}["a_eligible_decision_row"]
    assert ed["pass"] is False
    assert "AGENTIC_LOOP_ARM" in ed["detail"] and "armed=False" in ed["detail"], (
        "a missing decision row did not name the disarmed filer: " + ed["detail"])


# ── one inbox read serves lane 1, lane 2 and decide_today ────────────────

def test_the_inbox_is_read_once_per_tick_and_decide_today_reuses_it(shell, monkeypatch):
    """★ THE FIX FOR THE READ THAT ALWAYS LOST (2026-08-23).

    squasher_work_queue was read three times a tick — once per eligible class in
    lane 1, once for lane 2's collapse ratio, and once more by _decide_today,
    LAST, on whatever budget survived. On prod it lost every time: tick_ms
    8766-9398 against an 11s budget, so the board published "inbox UNREADABLE"
    beside lane 2's own successful count of the same rows in the same second.

    RED on the old code twice over: the read count was 3+, and the rows never
    reached the decide-today list.
    """
    rows = _inbox([_row(7, title="drip CTA prices the retired tier",
                        cls="pricing_copy", url="/api/v1/brain/x")])
    seen = []

    def _q(sql, params=None, conn=None, ctx=None):
        if _INBOX_SQL in sql:
            seen.append(sql)
            return rows
        if "MIN(requested_at)" in sql:
            return [(None, 0)]        # an aggregate always yields exactly one row
        return []

    monkeypatch.setattr(shell, "_q", _q)
    # ★ _gh takes ctx and returns (value, why) since the GET's GitHub read got
    #   its own budget gate (#3097). A stub with the OLD shape does not fail
    #   this test's subject — it TypeErrors the lane, which is why main went red
    #   here the moment #3097 and #3098 landed together.
    monkeypatch.setattr(shell, "_gh", lambda *a, **k: (
        None, "no GitHub token in this test — unverified, NOT assumed fine"))
    rep = {"classes": [{"class": "news_entity_reresolve", "eligible_for_grant": True},
                       {"class": "deals_exact_dupe_quarantine", "eligible_for_grant": True}]}
    monkeypatch.setattr(shell, "_graduation_report", lambda **k: (rep, "ok"))
    monkeypatch.setattr(shell, "_registry_rows",
                        lambda ctx: ([_class_row("news_entity_reresolve", granted=False)], "ok"))
    _attr_router(monkeypatch, shell, {})

    ctx = {"conn": object()}
    shell._lane_graduation(ctx)
    shell._lane_human_queues(ctx)
    items = shell._decide_today(ctx)

    assert len(seen) == 1, (
        "squasher_work_queue was read %d times in one tick — two eligible "
        "classes used to cost one read EACH before decide_today read it again"
        % len(seen))
    inbox = [i for i in items if str(i.get("kind", "")).startswith("inbox:")]
    assert inbox and inbox[0]["id"] == 7, (
        "the rows lane 2 had already read did not reach the decide-today list")
    assert inbox[0]["decide_url"] == "/api/v1/brain/squasher/resolve"
    assert inbox[0]["class_url"] == "/api/v1/brain/squasher/resolve-class"
    assert not [i for i in items if i.get("kind") == "unreadable"], (
        "a readable inbox reported itself unreadable")


# ── lane 2: the human queues ─────────────────────────────────────────────

def _lane2(shell, monkeypatch, *, ages=None, q=None, updates=None, gh=None):
    table = {}
    if ages is not None:
        table[("routes.squasher_queue", "queue_ages")] = lambda: ages
    if updates is not None:
        table[("routes.platform_updates", "published_updates")] = lambda: updates
    _attr_router(monkeypatch, shell, table)
    _q_router(monkeypatch, shell, q or {}, default=None)
    monkeypatch.setattr(shell, "_gh",
                        lambda path, ctx=None: (gh, "ok") if gh is not None
                        else (None, "no GitHub token in this test — unverified, NOT assumed fine"))
    checks = shell._lane_human_queues({"conn": object()})
    return {c["id"]: c for c in checks}


def test_oldest_decision_past_the_declared_ceiling_is_red(shell, monkeypatch):
    ceiling_h = shell.DECISION_AGE_CEILING_DAYS * 24
    by = _lane2(shell, monkeypatch, ages={"awaiting_decision": {"count": 2, "oldest_age_hours": ceiling_h - 1}})
    assert by["b_queue_ages"]["pass"] is True
    assert by["b_oldest_decision"]["pass"] is True                                  # control
    by = _lane2(shell, monkeypatch, ages={"awaiting_decision": {"count": 2, "oldest_age_hours": ceiling_h + 1}})
    assert by["b_oldest_decision"]["pass"] is False                                 # MUST-FAIL
    # part B's shape may be rows; the oldest across classes wins
    rows = [{"status": "awaiting_decision", "class": "a", "count": 1, "oldest_age_hours": 5},
            {"status": "awaiting_decision", "class": "b", "count": 1, "oldest_age_hours": ceiling_h + 5},
            {"status": "awaiting_ops", "class": "c", "count": 9, "oldest_age_hours": 999}]
    assert shell._oldest_age_hours(rows, "awaiting_decision") == ceiling_h + 5
    assert shell._oldest_age_hours({"by_status": {"awaiting_decision": {"oldest_age_days": 2}}}, "awaiting_decision") == 48.0
    assert shell._oldest_age_hours({"nothing": 1}, "awaiting_decision") is None
    # queue_ages() absent → direct read; an empty queue is a real pass, a stale row is red
    by = _lane2(shell, monkeypatch, q={"MIN(requested_at)": [(None, 0)]})
    assert by["b_queue_ages"]["pass"] is None and "part B" in by["b_queue_ages"]["detail"]
    assert by["b_oldest_decision"]["pass"] is True and "empty" in by["b_oldest_decision"]["detail"]
    old = shell._now() - _dt.timedelta(hours=ceiling_h + 10)
    by = _lane2(shell, monkeypatch, q={"MIN(requested_at)": [(old, 3)]})
    assert by["b_oldest_decision"]["pass"] is False and "direct read" in by["b_oldest_decision"]["detail"]


def test_platform_items_must_carry_an_age_and_a_decision_url(shell, monkeypatch):
    lacking = {"withheld": [{"id": "x", "reason": "not approved (status is not \"published\")"}]}
    by = _lane2(shell, monkeypatch, updates=lacking)
    assert by["b_platform_items"]["pass"] is False
    assert "awaiting_decision=1" in by["b_platform_items"]["detail"]
    carrying = {"withheld": [{"id": "x", "reason": "not approved", "announced": "2026-08-17",
                              "decision_url": "https://github.com/x/pull/1"}]}
    by = _lane2(shell, monkeypatch, updates=carrying)
    assert by["b_platform_items"]["pass"] is True                                    # control
    by = _lane2(shell, monkeypatch, updates={"ok": True, "withheld": []})
    assert by["b_platform_items"]["pass"] is True
    by = _lane2(shell, monkeypatch)                                                  # feed absent
    assert by["b_platform_items"]["pass"] is None

    # ★ THE ASSERTION ABOVE USED TO BE VACUOUS. `{"withheld": []}` alone is
    #   indistinguishable from published_updates()'s OWN fail-soft envelope,
    #   which is what it returns on ANY exception — a dict that passes the type
    #   test, with an empty `withheld` beside a false flag. The shipped lane read
    #   that as `pass=True, "pending=0 withheld=0 … every entry carries both"`:
    #   a platform-updates outage published as "the platform queue is clean".
    #   Probed live 2026-08-22: True. Same payload, opposite verdicts, and the
    #   only thing separating them is the flag.
    outage = {"ok": False, "cards": [], "withheld_count": 0, "withheld": [],
              "reason": "platform updates unavailable (OperationalError)"}
    by = _lane2(shell, monkeypatch, updates=outage)
    assert by["b_platform_items"]["pass"] is None, (
        "a platform-updates OUTAGE was published as a clean queue: "
        + by["b_platform_items"]["detail"])
    assert "ok=false" in by["b_platform_items"]["detail"]
    assert by["b_platform_items"]["critical"] is True, (
        "the platform queue must block a lane PASS when it cannot be read")


def test_stale_recs_reach_is_decided_from_the_digest_window_not_by_rendering_it(
        shell, monkeypatch):
    """★ The read path must NEVER call render_weekly_digest().

    Measured 2026-08-22 against live Neon: render_weekly_digest() takes 35-55s
    (returning rec_count=0). /api/v1/brain/agentic-loop and /admin/agentic-loop
    both run this lane, and a read that slow trips the Cloudflare route timeout
    — a 5xx from Railway fails the whole site over to stale Render. So the
    reach question is answered from the digest's SELECTION WINDOW, by AST.
    """
    old = shell._now() - _dt.timedelta(days=30)
    counted = {"COUNT(*), MIN(created_at) FROM brain_strategic_recommendations": [(42, old)]}
    sample = [(1, "Ship the parcel layer for Loudoun", old)]

    # a single-ISO-week window cannot contain a 30-day-old rec: RED.
    # ★ The defect is stubbed EXPLICITLY. Before 2026-08-23 the live digest
    # supplied it for free, so this assertion passed by accident of the bug it
    # describes; once the digest gained its age-based pass, reading the real
    # file here would have exercised the GREEN path while claiming to test RED.
    monkeypatch.setattr(shell, "_digest_reaches_aged_recs", lambda src: False)
    by = _lane2(shell, monkeypatch,
                q={**counted, "ORDER BY created_at LIMIT 10": sample})
    c = by["b_stale_recs_named"]
    assert c["pass"] is False, c["detail"]
    assert "42 rec(s)" in c["detail"] and "ONE ISO week" in c["detail"]
    assert "30d" in c["detail"], "the age comes from MIN(created_at), not from the sample"

    # CONTROL: nothing stale is a real pass
    by = _lane2(shell, monkeypatch,
                q={"COUNT(*), MIN(created_at) FROM brain_strategic_recommendations": [(0, None)]})
    assert by["b_stale_recs_named"]["pass"] is True

    # unreadable table stays unverified
    by = _lane2(shell, monkeypatch, q={})
    assert by["b_stale_recs_named"]["pass"] is None

    # a digest that DOES reach aged rows is a real pass, and the detail must say
    # what it proved (reach) and what it did not (that a human triaged them)
    monkeypatch.setattr(shell, "_digest_reaches_aged_recs", lambda src: True)
    by = _lane2(shell, monkeypatch,
                q={**counted, "ORDER BY created_at LIMIT 10": sample})
    c = by["b_stale_recs_named"]
    assert c["pass"] is True, c["detail"]
    assert "REACH, NOT TRIAGE" in c["detail"]
    assert "42 rec(s)" in c["detail"], "a pass must still publish the backlog size"

    # the renderer missing entirely is `?`, never a soft pass
    monkeypatch.setattr(shell, "_digest_reaches_aged_recs", lambda src: None)
    by = _lane2(shell, monkeypatch,
                q={**counted, "ORDER BY created_at LIMIT 10": sample})
    c = by["b_stale_recs_named"]
    assert c["pass"] is None and "could not be read" in c["detail"]


def test_the_digest_reach_rule_is_ast_and_a_stub_cannot_satisfy_it(shell):
    """The live file must satisfy the rule, and each way of faking it must not.

    ★ Both halves are load-bearing. Calling the reader while the reader itself
    filters by week_of restores the ORIGINAL defect (a rec ages out of the
    window again) behind a detector that would otherwise read green."""
    w = shell._digest_reaches_aged_recs
    real = w(open(os.path.join(ROOT, "routes", "brain_weekly_digest.py"),
                  encoding="utf-8").read())
    assert real is True, ("render_weekly_digest no longer reaches aged recs via "
                          "a week-independent _read_stale_recs — lane 2's reach "
                          "rule is broken, re-derive it before relaxing this")

    # the original defect: one ISO week only
    assert w("def render_weekly_digest(week_of=None):\n"
             "    return _read_recs_for(week_of)\n") is False

    # a comment cannot satisfy it
    assert w("def render_weekly_digest(week_of=None):\n"
             "    # we also read _read_stale_recs() for aged rows\n"
             "    return {}\n") is False, "a comment satisfied the rule"

    # a reader that is DEFINED but never called cannot satisfy it
    assert w("def _read_stale_recs(d):\n"
             "    return 'status = %s AND created_at < NOW() - make_interval(days => %s)'\n"
             "def render_weekly_digest(week_of=None):\n"
             "    return _read_recs_for(week_of)\n") is False, (
        "an uncalled helper satisfied the rule")

    # ★ called, but the reader reintroduces a week bound — the defect restored
    assert w("def _read_stale_recs(d):\n"
             "    return 'WHERE week_of = %s AND created_at < NOW() - make_interval(days => %s)'\n"
             "def render_weekly_digest(week_of=None):\n"
             "    return _read_stale_recs(14)\n") is False, (
        "a week-bounded aged reader satisfied the rule")

    # called, week-independent, age-bounded — the fix
    assert w("def _read_stale_recs(d):\n"
             "    return \"status = 'new' AND created_at < NOW() - make_interval(days => %s)\"\n"
             "def render_weekly_digest(week_of=None):\n"
             "    return _read_stale_recs(14)\n") is True

    assert w("def something_else():\n    pass\n") is None
    assert w("def render_weekly_digest(:::") is None          # unparseable


def test_no_lane_calls_the_weekly_digest_renderer_on_the_read_path():
    """Pinned on EXECUTABLE text, because the cost only shows up in production:
    render_weekly_digest() measured 35-55s against live Neon and every lane runs
    on the GET. The name is allowed to appear in prose (this file's own reason
    for the rule lives there) — it may not appear as a call or as the attribute
    _import_attr() would fetch."""
    tree = ast.parse(_src())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if (getattr(node.func, "id", None)
                    or getattr(node.func, "attr", None)) == "render_weekly_digest":
                bad.append("direct call")
            for a in node.args:
                if isinstance(a, ast.Constant) and a.value == "render_weekly_digest":
                    bad.append("fetched via " + (getattr(node.func, "id", None)
                                                 or getattr(node.func, "attr", "?")))
    assert not bad, (
        "a lane reaches render_weekly_digest() (%s) — that read takes 35-55s "
        "against live Neon and would trip the Cloudflare route timeout on "
        "/admin/agentic-loop, and a 5xx from Railway fails the site over"
        % ", ".join(sorted(set(bad))))


def test_digest_workflow_run_must_be_green_and_recent(shell, monkeypatch):
    fresh = shell._now().isoformat()
    by = _lane2(shell, monkeypatch, gh={"workflow_runs": [{"conclusion": "success", "created_at": fresh}]})
    assert by["b_digest_run"]["pass"] is True                                        # control
    by = _lane2(shell, monkeypatch, gh={"workflow_runs": [{"conclusion": "failure", "created_at": fresh}]})
    assert by["b_digest_run"]["pass"] is False
    stale = (shell._now() - _dt.timedelta(days=shell.DIGEST_MAX_AGE_DAYS + 2)).isoformat()
    by = _lane2(shell, monkeypatch, gh={"workflow_runs": [{"conclusion": "success", "created_at": stale}]})
    assert by["b_digest_run"]["pass"] is False
    by = _lane2(shell, monkeypatch, gh={"workflow_runs": []})
    assert by["b_digest_run"]["pass"] is False
    by = _lane2(shell, monkeypatch, gh=None)                                         # no token
    assert by["b_digest_run"]["pass"] is None and "NOT assumed" in by["b_digest_run"]["detail"]


def test_collapse_ratio_is_published_not_judged(shell, monkeypatch):
    """(open_rows, classified_rows, distinct_classes) — the ratio is over the
    rows that CARRY a class, never over the synthetic 'unclassified' bucket."""
    q = {_INBOX_SQL: _inbox([_row(i, cls="c%d" % (i % 3)) for i in range(12)])}
    by = _lane2(shell, monkeypatch, q=q)
    assert by["b_collapse_ratio"]["pass"] is None
    assert "3/12 = 0.25" in by["b_collapse_ratio"]["detail"]


def test_an_unclassified_queue_cannot_report_a_good_collapse_ratio(shell, monkeypatch):
    """★ THE INVERSION. The old query counted
    COUNT(DISTINCT COALESCE(action_class, 'unclassified')), so a queue where
    NOTHING is classified collapsed into one synthetic bucket and published
    1/11 = 0.09 — its best possible number — while the true answer was that no
    class decision could clear a single row.

    Measured on prod 2026-08-23: 11 open rows, all action_class NULL, ten
    different kinds of finding.
    """
    by = _lane2(shell, monkeypatch,
                q={_INBOX_SQL: _inbox([_row(i) for i in range(11)])})
    d = by["b_collapse_ratio"]["detail"]
    assert "0.09" not in d and "1/11" not in d, (
        "an unclassified queue published a collapse ratio as if it had collapsed")
    assert "UNDEFINED, not good" in d
    assert "0 of 11" in d


def test_a_partly_classified_queue_names_the_remainder(shell, monkeypatch):
    """The unclassified rows are published beside the ratio, not folded in."""
    rows = [_row(i, cls=("c%d" % (i % 2)) if i < 4 else None) for i in range(10)]
    by = _lane2(shell, monkeypatch, q={_INBOX_SQL: _inbox(rows)})
    d = by["b_collapse_ratio"]["detail"]
    assert "2/4 = 0.5" in d
    assert "6 of 10 open row(s) carry NO class" in d


def test_a_capped_inbox_read_publishes_the_ratio_as_a_floor(shell, monkeypatch):
    """The row read is capped; the CLAIM is not. COUNT(*) OVER () is computed
    before the LIMIT, so a truncated read is detectable — and a distinct-class
    count taken from rows we did not all see is a FLOOR, published as one
    rather than as the queue's true value."""
    rows = _inbox([_row(i, cls="c%d" % i) for i in range(3)],
                  open_rows=40, classified=40)
    d = _lane2(shell, monkeypatch, q={_INBOX_SQL: rows})["b_collapse_ratio"]["detail"]
    assert "READ CAPPED at 3 of 40" in d and "FLOOR" in d, (
        "a capped read published its partial class count as the whole queue's: " + d)
    # CONTROL: a complete read carries no floor caveat
    full = _inbox([_row(i, cls="c%d" % i) for i in range(3)])
    d = _lane2(shell, monkeypatch, q={_INBOX_SQL: full})["b_collapse_ratio"]["detail"]
    assert "READ CAPPED" not in d and "3/3 = 1.0" in d


# ── lane 3: the learn station ────────────────────────────────────────────

def _rag_stub(registered: bool, public: bool = False):
    m = types.ModuleType("routes.brain_rag")
    m.CORPORA = {"brain_findings": {"where": "1=1"}}
    m.LESSON_CORPORA = ("autopilot_outcomes",)
    m.PUBLIC_CORPORA = ("news_articles",)
    if registered:
        m.CORPORA["claim_lessons"] = {"where": "t.outcome IN ('refuted','retracted')"}
        m.LESSON_CORPORA += ("claim_lessons",)
    if public:
        m.PUBLIC_CORPORA += ("claim_lessons",)
    return m


def _lane3(shell, monkeypatch, *, rag=None, claim=None, q=None, attrs=None, planner_src="", ws=None):
    mods = {}
    if rag is not None:
        mods["routes.brain_rag"] = rag
    if ws is not None:
        mods["routes.brain_work_selector"] = ws
    monkeypatch.setattr(shell, "_module", lambda name: mods.get(name))
    _attr_router(monkeypatch, shell, attrs or {})
    table = dict(q or {})
    table.setdefault("ORDER BY outcome_at", [claim] if claim else [])
    table.setdefault("COUNT(*) FROM brain_predictions_log", [(1 if claim else 0,)])
    _q_router(monkeypatch, shell, table, default=None)
    monkeypatch.setattr(shell, "_read", lambda rel: planner_src)
    checks = shell._lane_learn({"conn": object()})
    return {c["id"]: c for c in checks}


def _claim(hours_ago=10.0):
    return (77, "canon:public.deals", "1,800+ tracked M&A deals", "refuted",
            _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours_ago))


def test_claim_lessons_corpus_must_be_registered_and_never_public(shell, monkeypatch):
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=_claim())
    assert by["c_corpus_registered"]["pass"] is True and by["c_corpus_registered"]["critical"] is True
    # MUST-FAIL: the leak class — brain internals in the keyless corpus
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True, public=True), claim=_claim())
    assert by["c_corpus_registered"]["pass"] is False and "PUBLIC" in by["c_corpus_registered"]["detail"]
    assert shell._lane_verdict(list(by.values())) == "FAIL"
    # absent with a refuted claim on the ledger is red; absent before any is `?`
    by = _lane3(shell, monkeypatch, rag=_rag_stub(False), claim=_claim())
    assert by["c_corpus_registered"]["pass"] is False and by["c_corpus_registered"]["critical"] is True
    by = _lane3(shell, monkeypatch, rag=_rag_stub(False), claim=None)
    assert by["c_corpus_registered"]["pass"] is None and by["c_corpus_registered"]["critical"] is True
    assert shell._lane_verdict(list(by.values())) == "?"
    # registered but nothing to recall yet is STILL `?` — a ledger read alone is not a learn station
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=None)
    assert by["c_corpus_registered"]["pass"] is True
    assert shell._lane_verdict(list(by.values())) == "?"


def test_corpus_must_embed_within_one_reindex_cycle(shell, monkeypatch):
    now = _dt.datetime.now(_dt.timezone.utc)
    # control: embedded after the newest negative outcome
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=_claim(10),
                q={"brain_corpus_embeddings": [(now - _dt.timedelta(hours=2), 1)]})
    assert by["c_embedded_fresh"]["pass"] is True and by["c_embedded_fresh"]["critical"] is True
    # MUST-FAIL: newest lesson 10h old, last embed before it, a cycle has passed
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=_claim(10),
                q={"brain_corpus_embeddings": [(now - _dt.timedelta(hours=20), 1)]})
    assert by["c_embedded_fresh"]["pass"] is False
    # not yet due (1h old, cycle 4h + 1h grace) is unverified, not red
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=_claim(1),
                q={"brain_corpus_embeddings": [(now - _dt.timedelta(hours=20), 1)]})
    assert by["c_embedded_fresh"]["pass"] is None and "not yet due" in by["c_embedded_fresh"]["detail"]


def test_recall_selftest_must_return_the_refuted_claim(shell, monkeypatch):
    claim = _claim()
    hit = lambda q, k=4: [{"text": "REFUTED: 1,800+ tracked M&A deals | expected 1,800+ | actual 1,900+"}]  # noqa: E731
    miss = lambda q, k=4: [{"text": "something unrelated"}]                                                  # noqa: E731
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=claim,
                attrs={("routes.brain_rag", "recall_negative_lessons"): hit})
    assert by["c_recall_selftest"]["pass"] is True and by["c_recall_selftest"]["critical"] is True
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=claim,
                attrs={("routes.brain_rag", "recall_negative_lessons"): miss})
    assert by["c_recall_selftest"]["pass"] is False                                  # MUST-FAIL
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=claim)                # part C absent
    assert by["c_recall_selftest"]["pass"] is None and "part C" in by["c_recall_selftest"]["detail"]
    assert shell._mentions({"meta": {"claim_id": 77}}, claim) is True
    assert shell._mentions({"text": "canon:public.deals drifted"}, claim) is True
    assert shell._mentions({"text": "nope"}, claim) is False


PLANNER_WITH = "def _build_prompt(ctx):\n    s = []\n    if ctx.get('refuted_claims'):\n        s.append('WHAT WE GOT WRONG (do not repeat):')\n    return ''.join(s)\n"
PLANNER_COMMENT_ONLY = "def _build_prompt(ctx):\n    # refuted_claims: WHAT WE GOT WRONG — todo\n    return ''\n"


def test_planner_prompt_section_is_pinned_by_ast_not_grep(shell, monkeypatch):
    assert shell._prompt_names_refuted_claims(PLANNER_WITH) is True
    assert shell._prompt_names_refuted_claims(PLANNER_COMMENT_ONLY) is False, "a comment satisfied the check"
    assert shell._prompt_names_refuted_claims("def other():\n    return 'refuted_claims'\n") is None
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=_claim(), planner_src=PLANNER_WITH)
    assert by["c_planner_section"]["pass"] is True
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=_claim(), planner_src=PLANNER_COMMENT_ONLY)
    assert by["c_planner_section"]["pass"] is False                                  # lessons exist, no section
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), claim=None, planner_src=PLANNER_COMMENT_ONLY)
    assert by["c_planner_section"]["pass"] is None                                   # nothing to render yet


def test_bandit_weights_non_empty_once_the_floor_is_met(shell, monkeypatch):
    ws = types.ModuleType("routes.brain_work_selector")
    ws.WORK_MIN_SAMPLES, ws.WORK_WINDOW_DAYS = 3, 45
    ws._learned_class_weights = lambda classes: {k: {"weight": 1.1, "samples": 5} for k in classes}
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), ws=ws,
                q={"brain_fix_outcomes": [("dedup", 5)], "autopilot_outcomes": []})
    assert by["c_bandit_weights"]["pass"] is True                                    # control
    ws.WORK_MIN_SAMPLES = 3
    ws._learned_class_weights = lambda classes: {k: {"weight": 1.0, "samples": 0} for k in classes}
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), ws=ws,
                q={"brain_fix_outcomes": [("dedup", 5)], "autopilot_outcomes": []})
    assert by["c_bandit_weights"]["pass"] is False                                   # MUST-FAIL: floor met, weights empty
    by = _lane3(shell, monkeypatch, rag=_rag_stub(True), ws=ws,
                q={"brain_fix_outcomes": [("dedup", 2)], "autopilot_outcomes": []})
    assert by["c_bandit_weights"]["pass"] is None and "data-starved" in by["c_bandit_weights"]["detail"]


# ── lane 4: detectors-with-the-fix, measured ─────────────────────────────

def _step4_detectors_from_the_gate_test() -> tuple:
    src = _src(os.path.join(ROOT, "tests", "test_brain_prs_carry_detector.py"))
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "STEP4_DETECTORS" for t in node.targets):
            return tuple(e.value for e in node.value.elts)
    raise AssertionError("STEP4_DETECTORS not found in the gate test")


def test_the_three_product_detectors_are_the_gates_own_list(shell):
    assert tuple(shell.PRODUCT_DETECTORS) == _step4_detectors_from_the_gate_test()


def _lane4(shell, monkeypatch, *, feed=None, names=None, findings=None, conv=None, prior=None, radar_src="def scan_all(): pass"):
    table = {}
    if feed is not None:
        table[("routes.ops_claims", "read_feed")] = lambda limit=1: feed
    if names is not None:
        table[("util.brain_detector_rule", "registered_checks")] = lambda src: set(names)
    if conv is not None:
        table[("routes.squasher_queue", "convergence")] = lambda days: conv
    _attr_router(monkeypatch, shell, table)
    _q_router(monkeypatch, shell, {"brain_findings": findings if findings is not None else [(0, None)]}, default=None)
    monkeypatch.setattr(shell, "_read", lambda rel: radar_src)
    monkeypatch.setattr(shell, "_rate_7d_ago", lambda ctx: prior)
    ctx = {"conn": object()}
    checks = shell._lane_detectors(ctx)
    return {c["id"]: c for c in checks}, ctx


def test_a_detector_dropping_out_of_the_sweep_tuple_is_red(shell, monkeypatch):
    full = list(shell.PRODUCT_DETECTORS) + ["check_other"]
    by, _ = _lane4(shell, monkeypatch, names=full)
    assert by["d_sweep_tuple"]["pass"] is True                                       # control
    by, _ = _lane4(shell, monkeypatch, names=full[1:])
    assert by["d_sweep_tuple"]["pass"] is False and full[0] in by["d_sweep_tuple"]["detail"]
    by, _ = _lane4(shell, monkeypatch, names=full, radar_src="")
    assert by["d_sweep_tuple"]["pass"] is None


def test_prs_with_detector_absent_is_unverified_and_blocks_a_pass(shell, monkeypatch):
    full = list(shell.PRODUCT_DETECTORS)
    conv = {"ok": True, "closed": 10, "recurred": 7, "recurrence_rate": 0.7}
    week_with = {"week": {"week_start": "2026-08-17", "brain_prs_with_detector": {
        "with_detector": 2, "checked": 3, "unknown": 0, "prs": 3, "basis": "b"}}}
    by, ctx = _lane4(shell, monkeypatch, feed=week_with, names=full, findings=[(4, "t")], conv=conv, prior=0.75)
    assert by["d_prs_with_detector"]["pass"] is True and by["d_prs_with_detector"]["critical"] is True
    assert all(by[f"d_fired_{n}"]["pass"] is True for n in full)
    assert by["d_convergence_read"]["pass"] is True
    assert by["d_recurrence_delta"]["pass"] is None and "delta=-0.05" in by["d_recurrence_delta"]["detail"]
    assert ctx["recurrence"] == {"rate": 0.7, "rate_7d_ago": 0.75, "delta_7d": -0.05}
    assert shell._lane_verdict(list(by.values())) == "PASS"
    # the instrument absent from the feed → `?`, and the lane cannot PASS on the AST check alone
    by, _ = _lane4(shell, monkeypatch, feed={"week": {"week_start": "x"}}, names=full, findings=[(4, "t")], conv=conv)
    assert by["d_prs_with_detector"]["pass"] is None
    assert shell._lane_verdict(list(by.values())) == "?"
    # a detector that has not fired reads `measuring`, not red
    by, _ = _lane4(shell, monkeypatch, feed=week_with, names=full, findings=[(0, None)], conv=conv)
    assert all(by[f"d_fired_{n}"]["pass"] is None and "measuring" in by[f"d_fired_{n}"]["detail"] for n in full)


def test_headline_metrics_come_from_the_feed_and_convergence(shell, monkeypatch):
    feed = {"ok": True, "week": {"week_start": "2026-08-17", "confirmed": 3, "refuted_kept": 1,
                                 "retracted": 0, "granted_action_classes": 1}}
    conv = {"ok": True, "recurrence_rate": 0.714}
    _attr_router(monkeypatch, shell, {("routes.ops_claims", "read_feed"): lambda limit=1: feed,
                                      ("routes.squasher_queue", "convergence"): lambda days: conv})
    m = shell._headline({"conn": None})
    assert (m["claims_confirmed"], m["refuted_kept"], m["retracted"], m["granted_classes"],
            m["recurrence_rate"]) == (3, 1, 0, 1, 0.714)
    assert m["recurrence_delta_7d"] is None                                          # no snapshot yet = null


def test_the_heavy_in_lane_reads_refuse_to_start_when_the_budget_is_gone(
        shell, monkeypatch):
    """★ Lane-level budgeting alone does NOT bind.

    Measured 2026-08-22 the lanes cost 0.1s / 1s / 9s / 12s, so a deadline
    checked only BETWEEN lanes let the last one START at 10s and run to 22s —
    past the edge's 15s. The two reads that dominate ask how much budget is
    left before they spend it: the effect bandit (one fresh DB connection PER
    CLASS inside brain_work_selector) and the per-detector findings loop.
    """
    import time as _t
    assert shell._budget_left({}) > 1e6, "no deadline must mean no limit"
    assert shell._budget_left({"deadline": _t.monotonic() - 1}) < 0

    # lane 4: CONTROL first — with budget, each detector is read
    full = list(shell.PRODUCT_DETECTORS)
    by, _ = _lane4(shell, monkeypatch, names=full, findings=[(3, None)])
    for name in shell.PRODUCT_DETECTORS:
        assert by[f"d_fired_{name}"]["pass"] is True

    def _spent_ctx():
        return {"conn": object(), "deadline": _t.monotonic() - 5}

    _attr_router(monkeypatch, shell, {})
    _q_router(monkeypatch, shell, {"brain_findings": [(3, None)]}, default=None)
    monkeypatch.setattr(shell, "_read", lambda rel: "def scan_all(): pass")
    monkeypatch.setattr(shell, "_rate_7d_ago", lambda ctx: None)
    by = {c["id"]: c for c in shell._lane_detectors(_spent_ctx())}
    for name in shell.PRODUCT_DETECTORS:
        c = by[f"d_fired_{name}"]
        assert c["pass"] is None and "budget" in c["detail"], (
            "a findings read started with no budget left: %s" % c["detail"])

    # lane 3: the bandit read is skipped, and skipped is `?` — never PASS
    ws = types.ModuleType("routes.brain_work_selector")
    ws.WORK_MIN_SAMPLES, ws.WORK_WINDOW_DAYS = 3, 45
    called = []

    def _weights(classes):
        called.append(classes)
        return {c: {"samples": 9, "weight": 1.3} for c in classes}
    ws._learned_class_weights = _weights
    monkeypatch.setattr(shell, "_module",
                        lambda name: ws if name == "routes.brain_work_selector" else None)
    _attr_router(monkeypatch, shell, {})
    _q_router(monkeypatch, shell,
              {"brain_fix_outcomes": [("brain_code_pr", 9)],
               "autopilot_outcomes": [],
               "ORDER BY outcome_at": [], "COUNT(*) FROM brain_predictions_log": [(0,)]},
              default=None)
    monkeypatch.setattr(shell, "_read", lambda rel: "")
    by = {c["id"]: c for c in shell._lane_learn({"conn": object()})}      # control
    assert by["c_bandit_weights"]["pass"] is True and called, "control never read the weights"
    called.clear()
    by = {c["id"]: c for c in shell._lane_learn(_spent_ctx())}
    assert by["c_bandit_weights"]["pass"] is None and "NOT read" in by["c_bandit_weights"]["detail"]
    assert not called, "the bandit read ran with no budget left (one connection per class)"


# ── wiring: scheduler, registration, vault-map detection, report-only ────

def test_the_scheduler_drives_the_tick(shell):
    assert callable(shell._beat_ledger)
    cron = _uncommented(_src(CRON))
    assert "agentic_loop_shell_daily" in cron
    assert TICK_ROUTE in cron, "shell #65 declares a beat with no cron_heartbeat dispatch entry"
    assert "AGENTIC_LOOP_SHELL_DISABLE" in cron, "the dispatch predicate ignores the kill switch"
    import re
    heavy = re.search(r"_HEAVY_LABELS = frozenset\(\{(.*?)\}\)", _src(CRON), re.S)
    assert heavy and '"agentic_loop_shell_daily"' in _uncommented(heavy.group(1)), (
        "the tick is heavy (GitHub reads, digest render, RAG recall) and must be throttle-pooled")


def test_main_registers_the_shell_in_its_own_try_except():
    """Sibling shells share one guard; a failure there skips every registration
    after it, silently. This shell must not inherit that."""
    tree = ast.parse(_src(MAIN))
    owning = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        imports = [n for n in node.body if isinstance(n, ast.ImportFrom)]
        if any(i.module == "routes.agentic_loop_master_shell" for i in imports):
            owning.append((node, imports))
    assert len(owning) == 1, f"expected exactly one try block importing the shell, found {len(owning)}"
    node, imports = owning[0]
    assert [i.module for i in imports] == ["routes.agentic_loop_master_shell"], (
        "the shell shares its try/except with another import")
    regs = [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "register_blueprint"]
    assert len(regs) == 1 and regs[0].args[0].id == "agentic_loop_master_shell_bp"


def test_vault_map_generator_sees_the_board_route_and_the_kill_switch():
    """scripts/generate_vault_map.py reads the FIRST /admin/... literal and the
    FIRST os.environ.get('…DISABLE…') in the source; a docstring mentioning
    another shell's board first would document this one as its sibling."""
    import re
    src = _src()
    route = re.search(r"[\"'](/admin/[a-z0-9\-]+)[\"']", src)
    kill = re.search(r"os\.environ\.get\(\s*[\"']([A-Z0-9_]*DISABLE[A-Z0-9_]*)[\"']", src)
    assert route and route.group(1) == "/admin/agentic-loop", route and route.group(1)
    assert kill and kill.group(1) == "AGENTIC_LOOP_SHELL_DISABLE", kill and kill.group(1)
    assert "/admin/agentic-loop" in _src(CRON), "the generator detects the cron by the board path"


def test_the_shell_is_report_only_apart_from_its_beat_and_its_own_ledger():
    tree = ast.parse(_src())
    writes = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in ("post", "put", "patch", "delete"):
                    writes.append((fn.name, name))
    assert writes == [("_beat_ledger", "post")], (
        f"HTTP write calls outside the beat: {writes} — the shell is REPORT-ONLY")
    sql_writes = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str)
                  and any(v in n.value.upper() for v in ("INSERT INTO", "UPDATE ", "DELETE FROM"))]
    assert sql_writes, "EXTRACTION EMPTY: the snapshot upsert is gone"
    for s in sql_writes:
        assert "agentic_loop_shell_ledger" in s and "ON CONFLICT" in s.upper(), (
            "a SQL write outside the shell's own ledger (or without ON CONFLICT): %r" % s[:80])
    src = _src()
    assert "gh pr merge" not in src and "urllib" not in _uncommented(src)


def test_shell_number_is_unique():
    others = []
    for path in glob.glob(os.path.join(ROOT, "routes", "*_master_shell.py")):
        if path.endswith("agentic_loop_master_shell.py"):
            continue
        raw = _src(path)
        if "SHELL_NUMBER = 65" in raw or "(#65," in raw or "(#65)" in raw:
            others.append(os.path.basename(path))
    assert not others, f"shell number 65 is already claimed by {others}"


def test_ledger_sql_names_the_table_the_constant_names(shell):
    """The SQL carries the table name as a LITERAL (so AST and lint can see it);
    this pins it to the constant the reads use."""
    assert shell.LEDGER_TABLE in shell._LEDGER_DDL and shell.LEDGER_TABLE in shell._LEDGER_UPSERT
    assert "ON CONFLICT (kind, day)" in shell._LEDGER_UPSERT


# ── parts B and C are imported LAZILY (they merge after this shell) ───────

# Every sibling mechanism the lanes read. Parts B and C add symbols to the
# first three; the rest already exist on main but are still reached lazily,
# because the shell must import on a deploy where any of them is broken.
_SIBLING_PREFIXES = ("routes", "util", "services", "utils")


def test_no_sibling_module_is_imported_at_module_level():
    """★ Parts B and C merge AFTER this shell (B → C → A).

    A module-level `from routes.squasher_action_classes import
    graduation_report` raises ImportError at boot while part B is unmerged —
    and main.py's own try/except SWALLOWS it, so /admin/agentic-loop 404s with
    nothing but a log warning to say why. Every sibling is reached through
    _module()/_import_attr() instead, which import inside try/except and
    return None, so the lanes render `?`.

    Pinned on the module's top-level import statements, not on a grep: a
    comment or a docstring naming the module cannot satisfy this.
    """
    tree = ast.parse(_src())
    top = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            top.append(node.module or "")
    assert top, "EXTRACTION EMPTY: no module-level imports parsed"
    leaked = sorted({m for m in top if m.split(".")[0] in _SIBLING_PREFIXES})
    assert not leaked, (
        "imported at module level: %s — a sibling that is missing or broken "
        "would take the whole shell off the deploy (main.py swallows the "
        "ImportError). Reach it through _module()/_import_attr()." % leaked)


def test_the_lazy_helpers_absorb_a_missing_sibling_and_the_lanes_read_question_mark(
        shell, monkeypatch):
    """The behavioural half: with EVERY sibling import raising (part B and
    part C not on this deploy), the helpers return None, the tick does not
    raise, and no lane claims PASS."""
    import importlib

    def _boom(name, *a, **k):
        raise ImportError("not on this deploy: " + name)

    monkeypatch.setattr(importlib, "import_module", _boom)
    assert shell._module("routes.squasher_action_classes") is None
    assert shell._import_attr("routes.squasher_action_classes", "graduation_report") is None
    assert shell._import_attr("routes.brain_rag", "recall_negative_lessons") is None
    # the read path must also refuse to call part B's writer through the gap
    report, why = shell._graduation_report(file_rows=False)
    assert report is None and "part B" in why

    monkeypatch.setattr(shell, "_conn", lambda: None)
    monkeypatch.setattr(shell, "_q", lambda *a, **k: None)
    monkeypatch.setattr(shell, "_gh", lambda *a, **k: (None, "no GitHub token in this test — unverified, NOT assumed fine"))
    out = shell._tick(act=False)
    assert out["ok"] is True and out["tick_failed"] is False
    assert out["summary"]["PASS"] == 0, (
        "a lane claimed PASS with its mechanism absent: "
        + str([(ln["lane"], ln["verdict"]) for ln in out["lanes"]]))


# ── the tick measures what it spends ─────────────────────────────────
def test_budget_spent_attributes_the_tick_to_reads_not_just_to_q(shell, monkeypatch):
    """★ THE BUDGET GOVERNED 12 OF 103 ROUND TRIPS.

    Measured 2026-08-23 against prod Neon: one tick opened TWENTY-NINE database
    connections and made 103 statement round trips; _q() — the read this shell
    bounds so carefully — was ONE connection and twelve of them. The rest were
    opened inside the siblings called through _call(), which have their own
    _conn() and never see this deadline. tick_ms published the total and
    attributed none of it, so a wrong hypothesis about where the time went
    ("~25-30 _q round trips") survived a full day of work.

    The report must therefore split SIBLING wall time from the db_read time the
    budget actually governs. A meter that only counts _q() would have confirmed
    the wrong hypothesis instead of refuting it.
    """
    monkeypatch.setattr(shell, "_conn", lambda: object())
    monkeypatch.setattr(shell, "_q", lambda *a, **k: [])

    def _slow_sibling():
        time.sleep(0.05)
        return {"ok": True}
    monkeypatch.setattr(shell, "_import_attr", lambda mod, name: _slow_sibling)
    monkeypatch.setattr(shell, "_module", lambda *a, **k: None)

    out = shell._tick(act=False)
    spent = out["budget"]["spent"]
    kinds = spent["by_kind"]
    assert "sibling" in kinds, (
        f"budget.spent attributes nothing to siblings ({sorted(kinds)}) — they are "
        f"where 28 of the tick's 29 connections were opened")
    assert kinds["sibling"]["ms"] >= 40, kinds["sibling"]
    assert spent["measured_ms"] <= spent["tick_ms"] + 1, spent
    assert {"tick_ms", "measured_ms", "unmeasured_ms", "top", "why"} <= set(spent)


def test_budget_spent_counts_the_reads_the_budget_STOPPED(shell, monkeypatch):
    """★ What a budget costs is the reads it stopped, so a spend report that
    only lists work DONE flatters the tick it measures. Both refusal paths —
    _q()'s own, and a pre-gate that never reaches _q() — must land on the
    ledger, or lane 4's three unread detectors are billed at zero and read as
    though they never needed the budget."""
    ctx = {"conn": object(), "deadline": time.monotonic() - 1}   # budget already gone
    assert shell._q("SELECT 1", ctx=ctx) is None
    kinds = shell._spend_report(ctx, 10)["by_kind"]
    assert kinds.get("db_refused", {}).get("calls") == 1, (
        f"a refused read left no trace on the ledger ({kinds}) — the budget's own "
        f"cost is invisible")

    # the pre-gate path: lane 4 skips its detector reads WITHOUT calling _q()
    src = _uncommented(_src())
    gate = src.split("if _budget_left(ctx) <= _DETECTOR_MIN_S:")[1].split("continue")[0]
    assert "_spent(" in gate, (
        "the detector pre-gate skips a read without recording it — _q()'s refusal "
        "ledger cannot see a read that never reached _q()")


def test_the_meter_can_never_break_the_tick_it_measures(shell):
    """A meter that can raise is a new way for the board to go dark. _spent()
    swallows everything, including a ctx that is not a dict at all."""
    for bad in (None, object(), 7, "ctx"):
        shell._spent(bad, "db_read", "x", 1.0)          # must not raise
    ctx: dict = {}
    for i in range(shell._SPEND_CAP + 25):
        shell._spent(ctx, "db_read", "q%d" % i, 1.0)
    assert len(ctx["spend"]) == shell._SPEND_CAP, "the ledger is unbounded"
    assert shell._spend_report(ctx, 1)["truncated"] is True, (
        "the ledger silently dropped reads — a truncated meter that does not say "
        "so publishes a smaller tick than the one that ran")
