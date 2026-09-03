"""The compliance QUERY, executed against a real Postgres.

tests/test_continuation_compliance.py pins the summariser. Nothing pinned the
SQL — and the SQL is where this measurement actually lives. Its first real
execution would otherwise be production, on the one endpoint whose whole point
is not reporting a wrong number confidently.

★ The query is READ OUT OF flask_mcp_endpoints.py, not copied here. A copy
would drift, and a drifted copy passing is worse than no test: it would report
that the shipped query behaves in a way the shipped query no longer does.

The fixture is built so every clause has a session that fails without it:

  s_act       treatment  called unlock_more_data after the gate   -> acted
  s_cont      treatment  called get_facility after the gate       -> continued, not acted
  s_dead      treatment  made no call at all after the gate       -> UNMEASURED, not 0%
  s_before    control    called the tool BEFORE the gate          -> not continued  (> boundary)
  s_internal  control    called it after, is_real_external=false  -> not continued  (filter)
  s_twice     t. then c. two gates, one session                   -> counted ONCE, under treatment
  s_null      control    mcp_client IS NULL                       -> 'unattributed'
  s_mcp       control    mcp_client = '  MCP  '                   -> trimmed+lowered to 'mcp'
  s_old       treatment  gated 30 days ago                        -> outside the 7-day window

Set CONTINUATION_SQL_DSN to run it. CI passes the db-parity service DSN and
then asserts this file did not skip — a skipped proof is not a proof.
"""
import ast
import datetime as dt
import os
import pathlib

import pytest

from continuation_compliance import (
    CONTINUATION_TOOLS, GENERIC_CLIENT, UNATTRIBUTED, summarize_compliance,
)

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("CONTINUATION_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="CONTINUATION_SQL_DSN not set — no Postgres to run against")

REPO = pathlib.Path(__file__).resolve().parents[1]
ROUTE = "mcp_continuation_compliance"


def shipped_sql():
    """The `sql = \"\"\"...\"\"\"` assigned inside the route, read from source.

    ast, not a regex and not an import: flask_mcp_endpoints pulls psycopg2 and
    Flask at module scope, which is the same reason the summariser lives in its
    own module. Parsing gets the real string without importing the world.
    """
    tree = ast.parse((REPO / "flask_mcp_endpoints.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == ROUTE:
            for stmt in ast.walk(node):
                if (isinstance(stmt, ast.Assign)
                        and any(getattr(t, "id", None) == "sql" for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    return stmt.value.value
    raise AssertionError(f"no `sql = \"...\"` found in {ROUTE}() — this test proves nothing")


DDL = """
DROP TABLE IF EXISTS mcp_upgrade_signals;
DROP TABLE IF EXISTS mcp_calls_identity;
-- In production mcp_calls_identity is a VIEW over the call log; only the four
-- columns this query reads are modelled here, with their production types.
CREATE TABLE mcp_upgrade_signals (
    session_id    TEXT,
    message_shown TEXT,
    mcp_client    TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE mcp_calls_identity (
    session_id       TEXT,
    tool_name        TEXT,
    is_real_external BOOLEAN,
    created_at       TIMESTAMP
);
"""

H = dt.timedelta(hours=1)
D = dt.timedelta(days=1)


def _fixture(cur):
    # NAIVE utc: mcp_upgrade_signals.created_at is TIMESTAMP without tz,
    # and utcnow() is deprecated on the 3.13 CI runs this job uses.
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    gate, old_gate = now - 2 * H, now - 30 * D

    def sig(sid, arm, client, at=None):
        cur.execute(
            "INSERT INTO mcp_upgrade_signals (session_id, message_shown, mcp_client, created_at)"
            " VALUES (%s, %s, %s, %s)",
            (sid, "trial_preview:" + arm, client, at or gate))

    def call(sid, tool, at, real=True):
        cur.execute(
            "INSERT INTO mcp_calls_identity (session_id, tool_name, is_real_external, created_at)"
            " VALUES (%s, %s, %s, %s)", (sid, tool, real, at))

    sig("s_act", "treatment", "claude");   call("s_act", "unlock_more_data", gate + H)
    sig("s_cont", "treatment", "claude");  call("s_cont", "get_facility", gate + H)
    sig("s_dead", "treatment", "grok")     # no calls at all
    sig("s_before", "control", "grok");    call("s_before", "unlock_more_data", gate - H)
    sig("s_internal", "control", "grok")
    call("s_internal", "unlock_more_data", gate + H, real=False)
    # two gates, one session: DISTINCT ON must keep only the earlier arm
    sig("s_twice", "treatment", "claude")
    sig("s_twice", "control", "claude", at=gate + 30 * dt.timedelta(minutes=1))
    call("s_twice", "claim_free_key", gate + H)
    sig("s_null", "control", None);        call("s_null", "bind_email", gate + H)
    sig("s_mcp", "control", "  MCP  ")     # no calls
    sig("s_old", "treatment", "claude", at=old_gate)
    call("s_old", "unlock_more_data", old_gate + H)


@pytest.fixture(scope="module")
def summary():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            _fixture(cur)
            # Executed exactly as the route executes it — same string, same
            # params, same order — so %% escaping and binding are proven too.
            cur.execute(shipped_sql(), (7, list(CONTINUATION_TOOLS)))
            rows = cur.fetchall()
    finally:
        conn.close()
    return summarize_compliance(rows), rows


def test_the_query_runs_and_returns_five_field_rows(summary):
    out, rows = summary
    assert rows, "the query returned nothing — the fixture or the window is wrong"
    assert all(len(r) == 5 for r in rows), f"row shape changed: {rows[0]}"
    assert out["dropped_rows"] == 0, "the summariser rejected rows the query produced"


def test_the_window_excludes_the_thirty_day_old_gate(summary):
    out, _ = summary
    assert out["totals"]["gated_sessions"] == 8   # nine sessions, s_old outside


def test_a_session_that_met_two_gates_counts_once_under_the_first(summary):
    out, _ = summary
    assert out["arms"]["treatment"]["gated_sessions"] == 4    # act, cont, dead, twice
    assert out["arms"]["control"]["gated_sessions"] == 4      # before, internal, null, mcp


def test_only_calls_after_the_gate_and_really_external_count_as_continued(summary):
    """s_before called the tool an hour EARLY and s_internal is not external.
    Either clause going missing turns both into compliant sessions."""
    out, _ = summary
    assert out["by_client"]["grok"]["state"] == "UNMEASURED"
    assert out["by_client"]["grok"]["gated_sessions"] == 3
    assert "no turn in which to comply" in out["by_client"]["grok"]["why"]


def test_continuation_tools_are_distinguished_from_any_other_call(summary):
    out, _ = summary
    t = out["arms"]["treatment"]
    assert t["continued_sessions"] == 3      # act, cont, twice — not dead
    assert t["acted_sessions"] == 2          # act, twice — s_cont called get_facility


def test_the_client_column_is_trimmed_lowered_and_defaulted_by_the_SQL(summary):
    """Asserted on the RAW rows, not the summary. parse_client also trims and
    lowercases, so checking the summary would pass even with the SQL's
    normalisation deleted — the test would be measuring the wrong layer."""
    _, rows = summary
    clients = {r[1] for r in rows}
    assert "mcp" in clients, f"'  MCP  ' was not trimmed+lowered by SQL: {clients}"
    assert "unattributed" in clients, f"NULL client was not defaulted: {clients}"
    assert "claude" in clients and "grok" in clients


def test_the_client_buckets_land_where_they_should(summary):
    out, _ = summary
    assert out["by_client"][UNATTRIBUTED]["gated_sessions"] == 1      # NULL client
    assert out["by_client"][GENERIC_CLIENT]["gated_sessions"] == 1    # '  MCP  '
    assert out["by_client"]["claude"]["gated_sessions"] == 3


def test_the_two_partitions_reconcile_on_real_query_output(summary):
    out, _ = summary
    for key in ("gated_sessions", "continued_sessions", "acted_sessions"):
        assert (sum(c[key] for c in out["by_client"].values())
                == out["totals"][key]
                == sum(out["arms"][a][key] for a in out["arms"]))
