"""main.py::_crawler_split_7d — the /api/ai/tracking hot path (2026-08-18).

WHY THIS EXISTS. `/api/ai/tracking` is the one endpoint the /ai page blocks on
that is NOT edge-cacheable: zone rule 756625a0 sets {cache:false} on it ON
PURPOSE, because the payload carries `recent_activity`, a live feed. So every
single page load pays the origin cost, and measured at the Railway origin the
crawler-split block was the bulk of it:

    /api/ai/tracking                 2.87 - 2.90s
    /api/v1/ai-tracking/cumulative   0.33 - 0.52s
    /api/ai/recent                   0.56s
    /api/v1/ai/crawler-split         8.99 - 10.65s  (six such queries)

The fix is a 300s in-process cache over the 7-DAY AGGREGATE only — never over
the live feed. These tests pin the three properties that make that safe:
a hit must not re-query, a FAILURE must not be cached, and the TTL must stay
at 300s. If someone later caches the whole response instead, the live panel
goes stale and rule 756625a0's intent is quietly reversed.

House rule: tests never import main.py (it opens pools and registers ~200
blueprints). The function is pulled out of the source with `ast` and executed
against stubs, so what runs here is the shipped code.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "main.py")


def _load():
    """Execute the real _crawler_split_7d against stubs; return (fn, ns)."""
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=SRC)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_crawler_split_7d"),
              None)
    assert fn is not None, (
        "_crawler_split_7d is not a top-level def in main.py. If it was renamed "
        "or re-inlined, move this guard with it — it is what keeps the /ai hot "
        "path off an uncached multi-second scan."
    )
    cache = {"ts": 0.0, "val": None}
    calls = []

    class _Logger:
        def warning(self, *a, **k):
            calls.append(("log", a))

    ns = {"_CRAWLER_SPLIT_CACHE": cache, "logger": _Logger(),
          "__executed__": calls}
    mod = ast.Module(body=[fn], type_ignores=[])
    exec(compile(mod, SRC, "exec"), ns)          # noqa: S102 - shipped source
    return ns["_crawler_split_7d"], ns, cache, calls


class _Cur:
    def __init__(self, rows, boom=False):
        self._rows, self._boom = rows, boom

    def execute(self, sql, params=None):
        if self._boom:
            raise RuntimeError("statement timeout")

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _Conn:
    def __init__(self, rows, boom=False):
        self.rows, self.boom, self.cursors, self.rolled = rows, boom, 0, 0

    def cursor(self):
        self.cursors += 1
        return _Cur(self.rows, self.boom)

    def rollback(self):
        self.rolled += 1


ROWS = [("organic_content", 12), ("instructed_metadata", 574),
        ("ambiguous_data_api", 45660)]


def test_first_call_queries_and_returns_the_split():
    fn, ns, cache, _ = _load()
    conn = _Conn(ROWS)
    out = fn(conn)
    assert conn.cursors == 1, "expected exactly one DB round-trip"
    assert out["window_days"] == 7
    assert out["rows_classified"] == 12 + 574 + 45660
    assert "never_sum" in out, "the do-not-sum disclosure must survive caching"


def test_a_second_call_does_not_touch_the_database():
    """THE POINT OF THE CHANGE. Without this, every /ai load re-runs the scan."""
    fn, ns, cache, _ = _load()
    conn = _Conn(ROWS)
    first = fn(conn)
    second = fn(conn)
    assert conn.cursors == 1, (
        "second call re-queried — the cache is not being consulted, so the "
        "hot path is still paying the scan on every request")
    assert second == first


def test_a_failure_is_not_cached():
    """★A cached failure would pin crawler_split_7d to null for 5 minutes past
    the request that could have read a real value — the same 'never cache an
    UNMEASURED verdict' rule the ops lanes follow."""
    fn, ns, cache, _ = _load()
    boom = _Conn(ROWS, boom=True)
    assert fn(boom) is None
    assert boom.rolled == 1, "a failed query must roll back the shared conn"
    assert cache["val"] is None, "the failure was cached"
    # and the very next call must be free to succeed
    ok = _Conn(ROWS)
    assert fn(ok)["rows_classified"] == 46246
    assert ok.cursors == 1


def test_failure_returns_none_rather_than_raising():
    """It shares `conn` with the rest of the dashboard: raising here would cost
    the payload its other fields."""
    fn, _, _, _ = _load()
    assert fn(_Conn(ROWS, boom=True)) is None


def test_the_ttl_is_300s_and_covers_only_the_aggregate():
    """Two coupled guarantees. 300s over a 7-day aggregate is imperceptible;
    the SAME cache applied to `recent_activity` would stale the live feed that
    zone rule 756625a0 keeps uncached on purpose. So: the TTL is 300, and this
    function must not be the one reading the feed."""
    # ★ Strip the DOCSTRING as well as the # comments before asserting. A first
    # cut stripped only `#` lines and failed on this function's own docstring,
    # which explains the live feed by name. Prose that describes the trap is not
    # the code committing it — ast.unparse drops both.
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=SRC)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_crawler_split_7d")
    stripped = ast.FunctionDef(
        name=fn.name, args=fn.args, decorator_list=[], returns=None,
        type_comment=None,
        body=[n for n in fn.body
              if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                      and isinstance(n.value.value, str))] or [ast.Pass()])
    ast.fix_missing_locations(stripped)
    code = ast.unparse(stripped)

    # ★ Read the TTL as a NUMBER off the comparison node, never as a substring.
    # `assert "300" in code` passes on a mutant widened to 3000 — the same
    # prefix-match escape that let `latest_returning_agents_x` through earlier
    # today. A numeric guard has to compare numbers.
    ttls = [c.value for node in ast.walk(stripped)
            if isinstance(node, ast.Compare)
            for c in node.comparators
            if isinstance(c, ast.Constant) and isinstance(c.value, (int, float))
            and not isinstance(c.value, bool)]
    assert 300 in ttls, (
        "TTL is no longer exactly 300s (found %r). 300s over a 7-day aggregate "
        "is imperceptible; widening it starts to matter." % (ttls,))
    for leaked in ("feed_rows_for_surface", "recent_activity"):
        assert leaked not in code, (
            "%s must not be computed inside the cached aggregate — caching the "
            "live feed is exactly what rule 756625a0 prevents" % leaked)


@pytest.mark.parametrize("bad", [{}, None])
def test_empty_or_missing_counts_do_not_raise(bad):
    fn, _, _, _ = _load()
    conn = _Conn([] if bad == {} else [])
    out = fn(conn)
    assert out is None or out["rows_classified"] == 0
