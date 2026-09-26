"""/api/ai/query?type=stats and /api/v1/stats/canonical must publish the SAME
facility count.

Measured 2026-09-25 (public GETs): /api/ai/query?type=stats answered
data.facilities = 23,172 and a suggested_response quoting "23,172", while
/api/v1/stats/canonical answered stats.facilities_distinct = 24,687 and
/api/v1/canon/phrases facilities = "24,600+". The stats branch read
canonical_stats' `facilities_verified` (the keeper-deduped alias), not the
distinct-building count the canonical endpoint says to cite.

Both handlers are run here against ONE fake database whose keeper-style
queries answer 23,173 and whose canonical distinct query answers 24,687, so a
handler that reads the wrong population produces the wrong number.

Per house rule main.py is never imported: ai_query is ast-extracted and
executed against stubs.
"""
import ast
import contextlib
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DISTINCT = 24687        # COUNT(DISTINCT canonical_slug) — the citeable count
KEEPER = 23173          # any is_duplicate / duplicate_of_id population
PHRASE = "24,600+"


class _Cur:
    description = [("x",)]

    def __init__(self):
        self._last = None

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())

    def fetchone(self):
        from util.facility_canon_count import CANON_SQL
        q = self._last or ""
        if q == " ".join(CANON_SQL.split()):
            return (DISTINCT,)
        if "is_duplicate" in q or "duplicate_of_id" in q:
            return (KEEPER,)
        if "SUM(capacity_mw)" in q:
            return (613748.5,)
        return (1234,)

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def cursor(self):
        return _Cur()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def _canon_cache(monkeypatch):
    import canonical_stats
    fake = {"deals": 1659, "markets": 331,
            "facilities_verified": KEEPER,
            "facilities_with_keeper_distinct": KEEPER,
            "facilities_distinct": DISTINCT}
    monkeypatch.setattr(canonical_stats, "get_canonical_stats",
                        lambda *a, **k: dict(fake))


def _ai_query_stats():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    body = ast.parse(src).body
    fns = [n for n in body
           if isinstance(n, ast.FunctionDef) and n.name == "ai_query"]
    assert len(fns) == 1, "ai_query not found in main.py — guard vacuous"
    fn = fns[0]
    fn.decorator_list = []
    # ai_query's stats branch calls these module-level helpers (#5622); lift
    # them from main.py too rather than restating them here.
    helpers = [n for n in body
               if (isinstance(n, ast.FunctionDef) and n.name == "_stats_citation_facilities")
               or (isinstance(n, ast.Assign) and any(
                   getattr(t, "id", None) == "_STATS_FACILITIES_SQL" for t in n.targets))]

    class _Args(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    class _Req:
        args = _Args({"type": "stats"})
        headers = _Args()

    @contextlib.contextmanager
    def _pg():
        yield _Conn()

    class _Log:
        def warning(self, *a, **k):
            raise AssertionError("ai_query hit its except path: %r" % (a,))

    ns = {
        "request": _Req,
        "jsonify": lambda p=None, **kw: p if p is not None else kw,
        "pg_connection": _pg,
        "logger": _Log(),
        "_DEALS_OK": "TRUE",
        "_canon_text": lambda s: s.replace("{canon_facilities}", PHRASE),
        "_honest_rest_wall": lambda plan: {"upgrade_url": "/pricing"},
        "get_ai_wars_key_info": lambda: None,
        "decode_jwt": lambda t: None,
    }
    mod = ast.Module(body=helpers + [fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "main.py", "exec"), ns)  # noqa: S102 — house pattern
    return ns["ai_query"]()


def _stats_canonical(monkeypatch):
    from flask import Flask
    import routes.facilities_by_dims as fbd
    monkeypatch.setattr(fbd, "_conn", lambda: _Conn())
    app = Flask(__name__)
    with app.test_request_context("/api/v1/stats/canonical"):
        resp = fbd.stats_canonical()
        resp = resp[0] if isinstance(resp, tuple) else resp
        return resp.get_json()


def test_ai_query_stats_facilities_equals_canonical_distinct(monkeypatch, _canon_cache):
    body = _ai_query_stats()
    canon = _stats_canonical(monkeypatch)
    served = body["data"]["facilities"]
    truth = canon["stats"]["facilities_distinct"]
    assert truth == DISTINCT, "fake DB did not reach the canonical query"
    assert served == truth, (
        f"/api/ai/query?type=stats facilities={served} but "
        f"/api/v1/stats/canonical facilities_distinct={truth}")


def test_ai_query_stats_prose_quotes_the_canon_phrase(_canon_cache):
    txt = _ai_query_stats()["suggested_response"]
    assert f"{PHRASE} data center facilities" in txt, txt
    assert f"{KEEPER:,}" not in txt, "prose quotes the keeper-deduped count"
