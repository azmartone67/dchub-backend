"""#3778 follow-up — /api/v1/reach splits REAL traffic by registry arrival source.

#5409 stored the MCP server's `source` tag in mcp_call_log.source, but reach
reads mcp_calls_identity — a view over mcp_tool_calls — so it could not break
down by source. track_tool_call now writes the tag to mcp_tool_calls too, and
reach joins the identity view back to mcp_tool_calls on its primary key.

Runs the REAL _reach_build_data (pulled out of flask_mcp_endpoints.py with
ast) against a fake pool that answers by SQL shape. No network, no database.

Properties pinned:
  * the split reads mcp_calls_identity JOIN mcp_tool_calls ON id and filters
    is_real_external — the same filter as the headline, so our probes and
    self-traffic are not counted as registry demand;
  * NULL source is reported as untagged_calls, never as a source named None;
  * the field is labelled attribution-only, never identity;
  * a missing column fails into a flag without disturbing the headline.
"""
import ast
import os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_NAMES = ("_REACH_STMT_TIMEOUT_MS", "_reach_bounded", "_reach_build_data")


class _Cur:
    def __init__(self, db):
        self.db = db
        self._res = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.db.sql.append(s)
        if s in ("BEGIN", "COMMIT", "ROLLBACK") or s.startswith("SET LOCAL"):
            return
        if "JOIN mcp_tool_calls" in s:
            if self.db.source_missing:
                raise RuntimeError('column tc.source does not exist')
            self._res = self.db.source_rows
        elif "FROM mcp_agent_retention_30d" in s:
            self._res = [(0, 0)]
        elif "FROM ai_citations" in s:
            self._res = [(0, 0)]
        elif s.startswith("SELECT COUNT(*) FILTER") and "NOT (" in s:
            self._res = [(40, 7, 900, 3)]   # real calls, real agents, probe calls, probe agents
        else:
            self._res = []

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res or [])


class _Conn:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self.db)


class _Pool:
    def __init__(self, source_rows=(), source_missing=False):
        self.source_rows = list(source_rows)
        self.source_missing = source_missing
        self.sql = []

    def connection(self):
        return _Conn(self)


def _build(pool):
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    ns = {
        "datetime": datetime, "timezone": timezone, "_pool": pool,
        "_DELOOP_PLATFORM_CASE": "platform",
        "_mark_wow_comparability": lambda *a, **k: None,
        "_rolling_spans": lambda *a, **k: None,
    }
    for node in ast.parse(src).body:
        hit = (isinstance(node, ast.FunctionDef) and node.name in _NAMES) or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id in _NAMES for t in node.targets))
        if hit:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "<flask_mcp_endpoints>", "exec"), ns)
    assert not set(_NAMES) - set(ns)
    return ns["_reach_build_data"]()


def _split_sql(pool):
    hits = [s for s in pool.sql if "JOIN mcp_tool_calls" in s]
    assert len(hits) == 1, hits
    return hits[0]


def test_reach_reports_real_traffic_by_arrival_source():
    pool = _Pool(source_rows=[(None, 31, 5), ("glama", 6, 2), ("smithery", 3, 1)])
    out = _build(pool)
    a = out["arrival_source_7d"]
    assert a["sources"] == [{"source": "glama", "calls": 6, "agents": 2},
                            {"source": "smithery", "calls": 3, "agents": 1}]
    assert a["untagged_calls"] == 31
    assert out["flags"]["arrival_source_available"] is True
    # the headline is untouched
    assert out["real_calls_7d"] == 40 and out["real_agents_7d"] == 7


def test_split_obeys_the_headlines_external_traffic_filter():
    pool = _Pool(source_rows=[])
    _build(pool)
    s = _split_sql(pool)
    # same rows as the headline: the identity view, joined back on its PK
    assert "FROM mcp_calls_identity i JOIN mcp_tool_calls tc ON tc.id = i.id" in s
    # and the same genuine-external filter — probes / self-traffic excluded
    where = s.split(" WHERE ", 1)[1]
    assert "i.is_real_external" in where
    assert "NOT i.is_real_external" not in where and "NOT (i.is_real_external" not in where
    assert "i.created_at >= NOW() - (7 * INTERVAL '1 day')" in where
    assert "GROUP BY 1" in s and "tc.source" in s.split(" FROM ", 1)[0]
    # agents use the same public-IP grain as real_agents_7d
    assert "COUNT(DISTINCT i.agent_id) FILTER (WHERE i.is_public_ip)" in s


def test_split_is_labelled_attribution_not_identity():
    out = _build(_Pool())
    assert out["arrival_source_7d"]["basis"] == "attribution_only"
    doc = out["source_columns"]["arrival_source_7d"]
    assert "never identity" in doc and "is_real_external" in doc


def test_missing_column_fails_into_a_flag_not_the_headline():
    pool = _Pool(source_missing=True)
    out = _build(pool)
    assert out["flags"]["arrival_source_available"] is False
    assert "source" in out["flags"]["arrival_source_error"]
    assert out["arrival_source_7d"]["sources"] == []
    assert out["real_calls_7d"] == 40 and out["flags"]["calls_available"] is True
    # rolled back, so the failed statement cannot poison a later read
    i = pool.sql.index(_split_sql(pool))
    assert pool.sql[i + 1] == "ROLLBACK"
