"""#3778 — POST /api/v1/mcp/track must STORE the registry `source` field.

dchub-mcp-server#331 sends `source` (glama, smithery, ...) on every tracked
call from a registry path. track_tool_call read the body with body.get(...) and
had no column for it, so it was accepted and silently dropped.

These tests run the REAL track_tool_call (pulled out of flask_mcp_endpoints.py
with ast — importing the module opens DB connections and registers blueprints)
against a recording fake connection. No network, no database.

Properties pinned:
  * a sanitised source lands in mcp_call_log.source, as its own column;
  * platform is untouched by it (source is NOT platform);
  * a hostile / malformed source becomes NULL, never a trimmed look-alike;
  * canonical /mcp traffic (no source) pays no catalog query and no DDL;
  * with the column absent the writer converges it under a real transaction,
    and if that cannot land the call row is STILL written (without source).
"""
import ast
import os
import re
import sys
import types
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_NAMES = (
    "_TRACK_SOURCE_MAX_LEN", "_TRACK_SOURCE_RE", "_sanitize_track_source",
    "_CALL_LOG_SOURCE_STATE", "_CALL_LOG_SOURCE_RETRY_S",
    "_CALL_LOG_SOURCE_PRESENT_SQL", "_ensure_call_log_source_column",
    "track_tool_call",
)


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._next = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        c = self.conn
        c.log.append(("execute", sql, params, c.autocommit))
        s = " ".join(sql.split())
        if s.startswith("SELECT 1 FROM information_schema.columns"):
            self._next = (1,) if c.has_source else None
        elif s.startswith("ALTER TABLE mcp_call_log"):
            if c.alter_fails:
                raise RuntimeError("canceling statement due to lock timeout")
            c.pending_source = True
        elif s.startswith("INSERT INTO mcp_call_log"):
            if "source" in s.split("VALUES")[0] and not c.has_source:
                raise RuntimeError('column "source" of relation "mcp_call_log" does not exist')
            c.inserts.append((s, params))
        else:
            self._next = None

    def fetchone(self):
        return self._next


class _Conn:
    def __init__(self, has_source=True, alter_fails=False):
        self.autocommit = True
        self.has_source = has_source
        self.alter_fails = alter_fails
        self.pending_source = False
        self.log = []
        self.inserts = []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.log.append(("commit", None, None, self.autocommit))
        if self.pending_source:
            self.has_source = True

    def rollback(self):
        self.log.append(("rollback", None, None, self.autocommit))
        self.pending_source = False

    def close(self):
        pass


class _Headers(dict):
    def get(self, k, default=None):
        return super().get(k, default)


def _load(conn, body, monkeypatch):
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    wanted = set(_NAMES)
    req = types.SimpleNamespace(get_json=lambda silent=True: body,
                                headers=_Headers(), remote_addr="203.0.113.9")
    ns = {
        "_re_mod": re, "os": os, "json": __import__("json"),
        "time": __import__("time"),
        "datetime": datetime, "timezone": timezone,
        "request": req,
        "jsonify": lambda *a, **k: (a[0] if a else k),
        "_open_track_conn": lambda: conn,
        # skip the legacy mcp_tool_calls dual-write + quota side-channels
        "_is_selfheal_synthetic": lambda *a: True,
        "_normalize_write_platform": lambda p: p,
        "_resolve_session_claimed_key": lambda c, s: None,
    }
    fake_db = types.ModuleType("db_utils")
    fake_db.try_get_db = lambda: None
    monkeypatch.setitem(sys.modules, "db_utils", fake_db)
    for node in tree.body:
        hit = False
        if isinstance(node, ast.Assign):
            hit = any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets)
        elif isinstance(node, ast.FunctionDef):
            hit = node.name in wanted
            if hit:
                node.decorator_list = []   # @mcp_bp.post / @_require_internal
        if hit:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "<flask_mcp_endpoints>", "exec"), ns)
    missing = wanted - set(ns)
    assert not missing, f"not found in flask_mcp_endpoints.py: {sorted(missing)}"
    return ns


def _track(monkeypatch, body, **conn_kw):
    conn = _Conn(**conn_kw)
    ns = _load(conn, body, monkeypatch)
    resp, code = ns["track_tool_call"]()
    return conn, resp, code


def _only_insert(conn):
    assert len(conn.inserts) == 1, conn.inserts
    sql, params = conn.inserts[0]
    # every placeholder has exactly one bound value
    assert sql.count("%s") == len(params), (sql, params)
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    return dict(zip(cols, params))


BASE = {"tool": "rank_markets", "platform": "claude", "status": "ok"}


# ── the bug: source was on the wire and never stored ────────────────────────

def test_source_lands_in_its_own_column(monkeypatch):
    conn, resp, code = _track(monkeypatch, dict(BASE, source="glama"))
    assert code == 200 and resp == {"ok": True}
    row = _only_insert(conn)
    assert row.get("source") == "glama"


def test_source_never_moves_platform(monkeypatch):
    # platform = which client, source = who sent them. Independent axes.
    conn, _, _ = _track(monkeypatch, dict(BASE, source="smithery"))
    row = _only_insert(conn)
    assert row["platform"] == "claude"
    assert row["source"] == "smithery"


def test_source_is_normalised_to_a_lowercase_slug(monkeypatch):
    conn, _, _ = _track(monkeypatch, dict(BASE, source="  PulseMCP "))
    assert _only_insert(conn)["source"] == "pulsemcp"


# ── caller-assertable: hostile or malformed values become NULL ──────────────

def test_malformed_source_is_dropped_not_trimmed(monkeypatch):
    for bad in ("glama'; drop table mcp_call_log;--", "glama smithery",
                "x" * 65, "", "   ", "-glama", "gläma", "gla\tma",
                "../glama", 123, ["glama"], {"source": "glama"}, None):
        conn, resp, _ = _track(monkeypatch, dict(BASE, source=bad))
        assert resp == {"ok": True}, bad
        row = _only_insert(conn)
        assert "source" not in row, (bad, row)


def test_sanitiser_boundaries(monkeypatch):
    ns = _load(_Conn(), {}, monkeypatch)
    s = ns["_sanitize_track_source"]
    assert s("a" * 64) == "a" * 64
    assert s("a" * 65) is None
    assert s("mcp.so") == "mcp.so" and s("lobe_hub-2") == "lobe_hub-2"


# ── the hot path: canonical /mcp traffic pays nothing ───────────────────────

def test_no_source_means_no_catalog_query_and_no_ddl(monkeypatch):
    conn, _, _ = _track(monkeypatch, dict(BASE), has_source=False)
    row = _only_insert(conn)
    assert "source" not in row
    sqls = [e[1] for e in conn.log if e[0] == "execute"]
    assert not any("information_schema" in s or "ALTER" in s for s in sqls), sqls


# ── deploy skew: column not there yet ───────────────────────────────────────

def test_absent_column_is_converged_in_a_real_transaction(monkeypatch):
    conn, _, _ = _track(monkeypatch, dict(BASE, source="glama"), has_source=False)
    assert _only_insert(conn)["source"] == "glama"
    steps = [(e[0], " ".join((e[1] or "").split())[:40], e[3]) for e in conn.log]
    i_set = next(i for i, s in enumerate(steps) if s[1].startswith("SET LOCAL lock_timeout"))
    i_alt = next(i for i, s in enumerate(steps) if s[1].startswith("ALTER TABLE mcp_call_log"))
    i_com = next(i for i, s in enumerate(steps) if s[0] == "commit")
    assert i_set < i_alt < i_com
    # SET LOCAL and ALTER ran with autocommit OFF (else SET LOCAL is a no-op) …
    assert steps[i_set][2] is False and steps[i_alt][2] is False
    # … and autocommit was restored before the INSERT.
    assert conn.autocommit is True
    ins = next(e for e in conn.log if e[1] and "INSERT INTO mcp_call_log" in e[1])
    assert ins[3] is True


def test_failed_convergence_still_writes_the_call_row(monkeypatch):
    conn, resp, _ = _track(monkeypatch, dict(BASE, source="glama"),
                           has_source=False, alter_fails=True)
    assert resp == {"ok": True}
    row = _only_insert(conn)
    assert "source" not in row and row["tool"] == "rank_markets"
    assert conn.autocommit is True
    assert any(e[0] == "rollback" for e in conn.log)


# ── declared schema ─────────────────────────────────────────────────────────

def test_migration_declares_the_column():
    path = os.path.join(ROOT, "migrations", "2026-09-23_mcp_call_log_source.sql")
    sql = open(path, encoding="utf-8").read()
    assert re.search(r"ALTER TABLE mcp_call_log ADD COLUMN IF NOT EXISTS source text;", sql)
    # psycopg2 treats % as a placeholder even inside -- comments
    assert "%" not in sql
