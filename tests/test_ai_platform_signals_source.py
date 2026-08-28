"""
tests/test_ai_platform_signals_source.py — the public AI-platform figures must
come from a live per-day source, and must never restate an all-time total as a
30-day one (2026-08-28).

WHAT WENT WRONG. routes/audience_signals.py:_ai_platform_signals() read
`ai_usage_tracking`, a DEAD table: 11,029 rows, last write 2026-08-20,
`tracked_at` NULL throughout, every recent row `platform='Unknown'`. Its
`timestamp` column is TEXT, so the 30-day filter compared it against
`(NOW() - INTERVAL '30 days')::text` — a STRING comparison doing a date
filter's job.

That could not raise. `timestamptz >= text` has no operator in Postgres, so the
fact that the query returned at all proved the column was text. The failure was
therefore SILENT, and public keyless /api/v1/audience/summary served
`top_platforms: []` and `ai_requests_30d: 16` — while the live source carried
365,256 all-time requests across 16 platforms, last write minutes old.

THE TRAP IN THE OBVIOUS FIX. The live roster endpoint reads `ai_cumulative`, so
"point it at ai_cumulative" is the natural repair and it is WRONG.
`ai_cumulative` is one row per platform holding an ALL-TIME `total_requests`
plus a 7-day column; it holds no 30-day figure at all. Publishing
`total_requests` under a `_30d` key would restate 365,256 all-time requests as
a monthly number — a bigger lie than the 16 it replaced. `ai_daily_stats
(date DATE, platform, request_count)` is the per-day source that
`update_7d_rolling()` already sums, and its `date` is a real DATE.

WHAT THIS LOCKS. The function reads `ai_daily_stats`; it never reads
`ai_usage_tracking` or `ai_cumulative`; all three published figures share one
window and one exclusion list; and rows in the window produce a non-empty
breakdown rather than the silent `[]` that shipped in public for weeks.

Every structural assertion is made on the AST, never on source text, and each
carries a must-fail control that mutates the tree and requires the check to go
red — a check that cannot fail is not a check.
"""
import ast
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIGNALS = ROOT / "routes" / "audience_signals.py"
FN = "_ai_platform_signals"


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"function {name!r} not found — this test is fencing nothing")


def _sql_constants(fn):
    """Every string constant inside `fn`, excluding its docstring."""
    doc = ast.get_docstring(fn, clean=False)
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            if doc is not None and n.value == doc:
                continue
            out.append(n.value)
    return out


def _tables_read(fn):
    """Table names appearing after FROM/JOIN in any SQL constant inside `fn`."""
    import re
    names = set()
    for c in _sql_constants(fn):
        for m in re.finditer(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", c, re.I):
            names.add(m.group(1).lower())
    return names


def _execute_queries(fn):
    """The SQL of each `cur.execute(...)` call, flattened.

    The queries are built from adjacent string literals wrapped in an f-string,
    so the AST holds a JoinedStr whose parts are Constants and FormattedValues.
    Checking the raw constants one at a time splits a single query into
    fragments and silently misses anything that spans them — so join the parts,
    rendering each interpolation as `{name}` to keep the seam visible.
    """
    out = []
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "execute"
                and n.args):
            continue
        a = n.args[0]
        parts = a.values if isinstance(a, ast.JoinedStr) else [a]
        buf = []
        for pnode in parts:
            if isinstance(pnode, ast.Constant) and isinstance(pnode.value, str):
                buf.append(pnode.value)
            elif isinstance(pnode, ast.FormattedValue):
                v = pnode.value
                buf.append("{%s}" % (v.id if isinstance(v, ast.Name) else "?"))
        out.append("".join(buf))
    return out


def _fn_node():
    return _func(ast.parse(SIGNALS.read_text(encoding="utf-8")), FN)


# ── 1. The dead table is gone, and ai_cumulative was not substituted ──

def test_reads_ai_daily_stats():
    assert "ai_daily_stats" in _tables_read(_fn_node()), (
        "the 30-day figures must come from the per-day table"
    )


def test_never_reads_the_dead_table():
    assert "ai_usage_tracking" not in _tables_read(_fn_node()), (
        "ai_usage_tracking is dead (last write 2026-08-20, text timestamp)"
    )


def test_no_thirty_day_figure_is_taken_from_ai_cumulative():
    """ai_cumulative holds all-time and 7d only — never a 30-day number."""
    assert "ai_cumulative" not in _tables_read(_fn_node()), (
        "ai_cumulative has no 30-day column; reading it here restates an "
        "all-time total as a monthly one"
    )


@pytest.mark.parametrize("planted", ["ai_usage_tracking", "ai_cumulative"])
def test_control_table_checks_can_fail(planted):
    """MUST-FAIL CONTROL: plant the forbidden table and require a red check."""
    src = SIGNALS.read_text(encoding="utf-8").replace(
        "FROM ai_daily_stats", f"FROM {planted}", 1
    )
    fn = _func(ast.parse(src), FN)
    assert planted in _tables_read(fn), (
        "MUST-FAIL CONTROL DID NOT APPLY — the mutation did not land, so the "
        "checks above are fencing nothing"
    )


# ── 2. One window and one exclusion list across all three figures ────

def test_all_three_queries_share_one_window_and_one_exclusion_list():
    """A total padded with internal buckets while the breakdown excludes them
    would make the published figures disagree with each other."""
    fn = _fn_node()
    reads = [q for q in _execute_queries(fn) if "ai_daily_stats" in q]
    assert len(reads) == 3, f"expected 3 reads of ai_daily_stats, found {len(reads)}"
    for q in reads:
        assert "ALL(%s)" in q, f"query does not apply the exclusion list: {q[:80]}"
    windows = {w for q in reads for w in ("{_AI_30D_WINDOW}",) if w in q}
    assert windows == {"{_AI_30D_WINDOW}"}, (
        "every read must interpolate the one shared window constant"
    )
    assert all("{_AI_30D_WINDOW}" in q for q in reads), (
        "a read with its own window makes the three published figures disagree"
    )


def test_control_shared_window_check_can_fail():
    """MUST-FAIL CONTROL: give one query its own window and require a red check."""
    src = SIGNALS.read_text(encoding="utf-8").replace(
        '_AI_30D_WINDOW = "date >= CURRENT_DATE - 30"',
        '_AI_30D_WINDOW = "date >= CURRENT_DATE - 30"\n'
        '_AI_OTHER_WINDOW = "date >= CURRENT_DATE - 7"',
        1,
    ).replace('f"WHERE {_AI_30D_WINDOW} "', 'f"WHERE {_AI_OTHER_WINDOW} "', 1)
    fn = _func(ast.parse(src), FN)
    reads = [q for q in _execute_queries(fn) if "ai_daily_stats" in q]
    assert not all("{_AI_30D_WINDOW}" in q for q in reads), (
        "MUST-FAIL CONTROL DID NOT APPLY — the second window was not planted, "
        "so the shared-window check is fencing nothing"
    )


# ── 3. Behaviour: rows in the window produce a non-empty breakdown ───

class _Cur:
    """Cursor returning one canned row per execute, in call order."""
    def __init__(self, results):
        self.results = list(results)
        self.queries = []
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql, args=None):
        self.queries.append((sql, args))
    def fetchone(self):
        return self.results.pop(0)
    def fetchall(self):
        return self.results.pop(0)


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.closed = False
    def cursor(self):
        return self._cur
    def close(self):
        self.closed = True


@pytest.fixture
def fake_main():
    saved = sys.modules.get("main")

    def _install(conn):
        mod = types.ModuleType("main")
        mod.get_db = lambda: conn
        sys.modules["main"] = mod
    yield _install
    if saved is not None:
        sys.modules["main"] = saved
    else:
        sys.modules.pop("main", None)


def _signals_with(rows_total, rows_distinct, rows_top, fake_main):
    from routes import audience_signals
    # _bound() issues a SET first; it is swallowed by the same cursor.
    cur = _Cur([rows_total, rows_distinct, rows_top])
    conn = _Conn(cur)
    fake_main(conn)
    return audience_signals._ai_platform_signals(), cur, conn


def test_rows_in_window_produce_a_non_empty_breakdown(fake_main):
    """The exact shape that shipped broken: data exists, output was []."""
    out, cur, conn = _signals_with(
        (2438,), (16,),
        [("perplexity", 1200), ("chatgpt", 800), ("claude", 438)],
        fake_main,
    )
    assert out.get("_error") is None, out.get("_error")
    assert out["total_requests_30d"] == 2438
    assert out["distinct_platforms"] == 16
    assert out["top_platforms"] == [
        {"name": "perplexity", "count": 1200},
        {"name": "chatgpt", "count": 800},
        {"name": "claude", "count": 438},
    ]
    assert conn.closed, "connection must be closed on the success path"
    reads = [q for q, _ in cur.queries if "ai_daily_stats" in q]
    assert len(reads) == 3, f"expected 3 reads of ai_daily_stats, got {len(reads)}"


def test_control_breakdown_check_can_fail(fake_main):
    """MUST-FAIL CONTROL: an empty top-platform result must NOT pass the
    assertion above — this is the exact state that shipped in public."""
    out, _, _ = _signals_with((0,), (0,), [], fake_main)
    assert out["top_platforms"] == [], "control setup wrong"
    with pytest.raises(AssertionError):
        assert out["top_platforms"], (
            "empty breakdown must be distinguishable from a populated one"
        )


def test_db_failure_returns_zeros_and_an_error_not_a_number(fake_main):
    """Absent must read as absent — never as a flattering zero with no marker."""
    class _Boom(_Cur):
        def execute(self, sql, args=None):
            raise RuntimeError("relation \"ai_daily_stats\" does not exist")
    conn = _Conn(_Boom([]))
    fake_main(conn)
    from routes import audience_signals
    out = audience_signals._ai_platform_signals()
    assert out["_error"], "a failed read must set _error"
    assert out["top_platforms"] == []
    assert out["total_requests_30d"] == 0
