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
    "_TRACK_COLUMN_STATE", "_TRACK_COLUMN_TYPE", "_TRACK_COLUMN_RETRY_S",
    "_TRACK_COLUMN_PRESENT_SQL", "_ensure_track_column", "_ensure_source_column",
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
            table, column = (tuple(params or ()) + ("mcp_call_log", "source"))[:2]
            if column == "keyed":
                present = c.tc_has_keyed
            else:
                present = c.has_source if table == "mcp_call_log" else c.tc_has_source
            self._next = (1,) if present else None
        elif s.startswith("ALTER TABLE mcp_call_log"):
            if c.alter_fails:
                raise RuntimeError("canceling statement due to lock timeout")
            c.pending_source = True
        elif s.startswith("ALTER TABLE mcp_tool_calls"):
            if c.alter_fails:
                raise RuntimeError("canceling statement due to lock timeout")
            if " keyed " in s + " ":
                c.pending_tc_keyed = True
            else:
                c.pending_tc_source = True
        elif s.startswith("INSERT INTO mcp_call_log"):
            if "source" in s.split("VALUES")[0] and not c.has_source:
                raise RuntimeError('column "source" of relation "mcp_call_log" does not exist')
            c.inserts.append((s, params))
        else:
            self._next = None

    def fetchone(self):
        return self._next


class _Conn:
    def __init__(self, has_source=True, alter_fails=False, tc_has_source=None,
                 tc_has_keyed=True):
        self.autocommit = True
        self.tc_has_keyed = tc_has_keyed
        self.pending_tc_keyed = False
        self.has_source = has_source
        self.tc_has_source = has_source if tc_has_source is None else tc_has_source
        self.pending_tc_source = False
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
        if self.pending_tc_source:
            self.tc_has_source = True
        if self.pending_tc_keyed:
            self.tc_has_keyed = True

    def rollback(self):
        self.log.append(("rollback", None, None, self.autocommit))
        self.pending_source = False
        self.pending_tc_source = False
        self.pending_tc_keyed = False

    def close(self):
        pass


class _Headers(dict):
    def get(self, k, default=None):
        return super().get(k, default)


def _load(conn, body, monkeypatch, pooled=None):
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
        # skip the legacy mcp_tool_calls dual-write + quota side-channels,
        # unless a pooled fake is passed to exercise that write
        "_is_selfheal_synthetic": lambda *a: pooled is None,
        "_normalize_write_platform": lambda p: p,
        "_resolve_session_claimed_key": lambda c, s: None,
    }
    fake_db = types.ModuleType("db_utils")
    fake_db.try_get_db = lambda: pooled
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


def _track(monkeypatch, body, pooled=None, **conn_kw):
    conn = _Conn(**conn_kw)
    ns = _load(conn, body, monkeypatch, pooled=pooled)
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


# ── r-reach-source: the SAME tag on mcp_tool_calls, the table reach reads ───
# /api/v1/reach reads mcp_calls_identity, a view over mcp_tool_calls — not
# mcp_call_log. Without the tag on mcp_tool_calls reach cannot split by
# source at all.

class _Pooled:
    """db_utils pooled connection: records the mcp_tool_calls INSERT. DDL
    here is a failure — db_utils may silently drop it (SKIP_DDL)."""
    def __init__(self, direct):
        self.direct = direct
        self.inserts = []
        self.ddl = []

    def cursor(self):
        pooled = self

        class _C:
            def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if s.startswith(("ALTER", "SET LOCAL", "SELECT 1 FROM information_schema")):
                    pooled.ddl.append(s)
                elif s.startswith("INSERT INTO mcp_tool_calls"):
                    if "source" in s.split("VALUES")[0] and not pooled.direct.tc_has_source:
                        raise RuntimeError('column "source" of relation "mcp_tool_calls" does not exist')
                    if "keyed" in s.split("VALUES")[0] and not pooled.direct.tc_has_keyed:
                        raise RuntimeError('column "keyed" of relation "mcp_tool_calls" does not exist')
                    pooled.inserts.append((s, params))
        return _C()

    def commit(self):
        pass

    def close(self):
        pass


def _tool_calls_row(pooled):
    assert len(pooled.inserts) == 1, pooled.inserts
    sql, params = pooled.inserts[0]
    assert sql.count("%s") == len(params), (sql, params)
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    return dict(zip(cols, params))


def _track_both(monkeypatch, body, **conn_kw):
    direct = _Conn(**conn_kw)
    pooled = _Pooled(direct)
    ns = _load(direct, body, monkeypatch, pooled=pooled)
    resp, code = ns["track_tool_call"]()
    return direct, pooled, resp, code


def test_source_also_lands_in_mcp_tool_calls(monkeypatch):
    direct, pooled, resp, _ = _track_both(monkeypatch, dict(BASE, source="glama"))
    assert resp == {"ok": True}
    assert _tool_calls_row(pooled)["source"] == "glama"
    assert _only_insert(direct)["source"] == "glama"


def test_tool_calls_source_is_the_sanitised_value(monkeypatch):
    _, pooled, _, _ = _track_both(monkeypatch, dict(BASE, source=" Smithery "))
    row = _tool_calls_row(pooled)
    assert row["source"] == "smithery" and row["platform"] == "claude"
    _, pooled, _, _ = _track_both(monkeypatch, dict(BASE, source="gla ma"))
    assert "source" not in _tool_calls_row(pooled)


def test_tool_calls_no_source_pays_nothing(monkeypatch):
    direct, pooled, _, _ = _track_both(monkeypatch, dict(BASE),
                                       has_source=False, tc_has_source=False)
    assert "source" not in _tool_calls_row(pooled)
    # No source work at all. (keyed is written on every call, so its own
    # catalog check does run — once, until the column is confirmed.)
    ex = [e for e in direct.log if e[0] == "execute"]
    assert not any("ALTER" in e[1] and "source" in e[1] for e in ex), ex
    assert not any("information_schema" in e[1] and "source" in (e[2] or ()) for e in ex), ex


def test_tool_calls_column_converged_on_direct_conn_not_pooled(monkeypatch):
    direct, pooled, _, _ = _track_both(monkeypatch, dict(BASE, source="glama"),
                                       tc_has_source=False)
    assert _tool_calls_row(pooled)["source"] == "glama"
    assert pooled.ddl == []
    alters = [e for e in direct.log
              if e[0] == "execute" and e[1].startswith("ALTER TABLE mcp_tool_calls")]
    assert len(alters) == 1 and alters[0][3] is False   # in a real txn
    assert direct.autocommit is True


def test_tool_calls_failed_convergence_still_writes_the_row(monkeypatch):
    direct, pooled, resp, _ = _track_both(monkeypatch, dict(BASE, source="glama"),
                                          tc_has_source=False, alter_fails=True,
                                          has_source=True)
    assert resp == {"ok": True}
    row = _tool_calls_row(pooled)
    assert "source" not in row and row["tool_name"] == "rank_markets"
    # the call_log column was already present: its tag still lands
    assert _only_insert(direct)["source"] == "glama"


def test_ensure_rejects_unlisted_tables(monkeypatch):
    ns = _load(_Conn(), {}, monkeypatch)
    conn = _Conn(has_source=False)
    assert ns["_ensure_source_column"](conn, "users; drop table x") is False
    assert conn.log == []


def test_tool_calls_migration_declares_the_column():
    path = os.path.join(ROOT, "migrations", "2026-09-24_mcp_tool_calls_source.sql")
    sql = open(path, encoding="utf-8").read()
    assert re.search(r"ALTER TABLE mcp_tool_calls ADD COLUMN IF NOT EXISTS source text;", sql)
    assert "%" not in sql


# ── r-anon-wall-keyed: mcp_tool_calls.keyed, read by the anonymous wall ─────
# routes/mcp_anon_usage.py counts an IP's anonymous calls from mcp_tool_calls.
# Without a key column every keyed call from the IP counted against the
# anonymous budget of everyone behind it.

def test_keyed_call_is_marked_keyed(monkeypatch):
    _, pooled, _, _ = _track_both(monkeypatch, dict(BASE, api_key="dch_live_abc"))
    assert _tool_calls_row(pooled)["keyed"] is True


def test_anonymous_call_is_marked_not_keyed(monkeypatch):
    for body in (dict(BASE), dict(BASE, api_key=None), dict(BASE, api_key="  ")):
        _, pooled, _, _ = _track_both(monkeypatch, body)
        assert _tool_calls_row(pooled)["keyed"] is False, body


def test_keyed_follows_the_gateway_key_not_the_session_bound_one(monkeypatch):
    # The wall checks the gateway's own c.api_key. A key this handler later
    # resolves from the session never reached the wall, so the call was
    # anonymous as far as the wall is concerned and must count.
    direct = _Conn()
    pooled = _Pooled(direct)
    ns = _load(direct, dict(BASE, session_id="s-1"), monkeypatch, pooled=pooled)
    ns["_resolve_session_claimed_key"] = lambda c, s: "dch_live_bound"
    ns["track_tool_call"]()
    # the session key WAS resolved (it is on the call-log row) …
    assert _only_insert(direct)["api_key"] == "dch_live_bound"
    # … and still does not make the call keyed for the wall
    assert _tool_calls_row(pooled)["keyed"] is False


def test_keyed_column_converged_on_direct_conn_not_pooled(monkeypatch):
    direct, pooled, _, _ = _track_both(monkeypatch, dict(BASE, api_key="k"),
                                       tc_has_keyed=False)
    assert _tool_calls_row(pooled)["keyed"] is True
    assert pooled.ddl == []
    alters = [e for e in direct.log if e[0] == "execute"
              and e[1].startswith("ALTER TABLE mcp_tool_calls")]
    assert len(alters) == 1 and alters[0][3] is False
    assert "ADD COLUMN IF NOT EXISTS keyed BOOLEAN" in alters[0][1]


def test_keyed_failed_convergence_still_writes_the_row(monkeypatch):
    direct, pooled, resp, _ = _track_both(monkeypatch, dict(BASE, api_key="k"),
                                          tc_has_keyed=False, alter_fails=True)
    assert resp == {"ok": True}
    row = _tool_calls_row(pooled)
    assert "keyed" not in row and row["tool_name"] == "rank_markets"
    assert direct.autocommit is True


def test_keyed_and_source_together_bind_in_column_order(monkeypatch):
    _, pooled, _, _ = _track_both(monkeypatch, dict(BASE, source="glama", api_key="k"))
    row = _tool_calls_row(pooled)
    assert row["source"] == "glama" and row["keyed"] is True


def test_ensure_rejects_unlisted_columns(monkeypatch):
    ns = _load(_Conn(), {}, monkeypatch)
    conn = _Conn(has_source=False)
    assert ns["_ensure_track_column"](conn, "mcp_tool_calls", "ip_address; drop") is False
    assert ns["_ensure_track_column"](conn, "mcp_call_log", "keyed") is False
    assert conn.log == []


def test_keyed_migration_declares_the_column():
    path = os.path.join(ROOT, "migrations", "2026-09-25_mcp_tool_calls_keyed.sql")
    sql = open(path, encoding="utf-8").read()
    assert re.search(r"ALTER TABLE mcp_tool_calls ADD COLUMN IF NOT EXISTS keyed boolean;", sql)
    assert "%" not in sql
