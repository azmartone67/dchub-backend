"""r-anon-wall-keyed (2026-09-25) — the anonymous per-IP count skips keyed calls.

routes/mcp_anon_usage.py feeds the gateway's anonymous daily cap and the hard
wall (cap x 10). It counted EVERY mcp_tool_calls row for the IP, so keyed
calls — which the gateway never walls — used up the anonymous budget of
everyone behind the same IP. Measured 2026-09-25: the owner's laptop, whose
keyed Claude sessions share its IP, walled the anonymous ChatGPT-directory
probe (115 of 137 calls anon_hard_wall, 0 data answers).

Pinned here, against a fake psycopg2 (no network, no database):
  * the count query skips keyed IS TRUE and keeps NULL rows;
  * before the column exists, it falls back to counting every row — never to
    the fail-open 0, which would lift the wall — with the statement timeout
    set again after the rollback;
  * only when both reads fail does it fail open to 0.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.conn.log.append(s)
        if s.startswith("SET LOCAL"):
            return
        if "keyed" in s and not self.conn.has_keyed:
            raise RuntimeError('column "keyed" does not exist')
        if self.conn.count_fails:
            raise RuntimeError("canceling statement due to statement timeout")
        self._row = (self.conn.anon if "keyed IS NOT TRUE" in s else self.conn.total,)

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, has_keyed=True, count_fails=False, total=412, anon=37):
        self.has_keyed = has_keyed
        self.count_fails = count_fails
        self.total = total
        self.anon = anon
        self.log = []

    def cursor(self):
        return _Cur(self)

    def rollback(self):
        self.log.append("ROLLBACK")

    def close(self):
        pass


def _count(monkeypatch, conn):
    fake = types.ModuleType("psycopg2")
    fake.connect = lambda *a, **k: conn
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    from routes import mcp_anon_usage
    return mcp_anon_usage._today_count_for_ip("198.51.100.7")


def test_counts_only_calls_made_without_a_key(monkeypatch):
    conn = _Conn()
    assert _count(monkeypatch, conn) == 37
    q = [s for s in conn.log if "COUNT(*)" in s]
    assert len(q) == 1 and q[0].endswith("AND keyed IS NOT TRUE")


def test_null_keyed_still_counts():
    # IS NOT TRUE, not = FALSE: rows written before the column (NULL) count.
    from routes import mcp_anon_usage
    sql = " ".join(mcp_anon_usage._COUNT_ANON_SQL.split())
    assert "keyed IS NOT TRUE" in sql
    assert "keyed = false" not in sql.lower() and "not keyed" not in sql.lower()


def test_absent_column_falls_back_to_all_rows_not_to_zero(monkeypatch):
    conn = _Conn(has_keyed=False)
    assert _count(monkeypatch, conn) == 412
    i_rb = conn.log.index("ROLLBACK")
    after = conn.log[i_rb + 1:]
    assert after[0].startswith("SET LOCAL statement_timeout")
    assert "COUNT(*)" in after[1] and "keyed" not in after[1]


def test_both_reads_failing_fails_open_to_zero(monkeypatch):
    assert _count(monkeypatch, _Conn(has_keyed=False, count_fails=True)) == 0
