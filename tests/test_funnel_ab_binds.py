"""/api/v1/admin/funnel-ab binds (2026-09-02, finding 4b).

MEASURED 2026-09-02T00:24Z at the Railway origin: {"error":"tuple index out
of range","ok":false}, HTTP 500, for every request. Not a row-indexing bug.
`_ADMIN_EXCLUDE` carried `LIKE '/api/v1/admin/%'` and every read that embeds
it also binds the window (`(%s || ' days')`), so psycopg2 %-formatted the
whole statement client-side and the bare percent consumed a tuple slot —
the statement never reached Postgres. The fix is one doubled character;
the guard is a stub cursor that BINDS before it answers, so it fails on
exactly the shape the driver fails on.
"""
import os
import re
import sys
import contextlib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

flask = pytest.importorskip("flask")


def _bind(sql, params):
    return sql % tuple(repr(p) for p in params) if params is not None else sql


def test_the_exclusion_fragment_carries_no_bare_percent():
    from routes.paywall_hint_middleware import _ADMIN_EXCLUDE
    assert not re.search(r"%(?![%s])", _ADMIN_EXCLUDE), _ADMIN_EXCLUDE
    assert "NOT LIKE" in _ADMIN_EXCLUDE


def test_every_read_that_embeds_the_fragment_binds():
    """Assemble the three statements the way the handler does and bind them
    the way psycopg2 does — a stray percent raises here, as it did live."""
    from routes.paywall_hint_middleware import _ADMIN_EXCLUDE
    stmt = ("SELECT COUNT(*) FROM ab_funnel_log WHERE ts > NOW() - (%s || ' days')::interval AND "
            + _ADMIN_EXCLUDE)
    bound = _bind(stmt, ("7",))
    # after binding, LIKE sees a single wildcard — the same set of rows
    assert "NOT LIKE '/api/v1/admin/%'" in bound and "%%" not in bound


def test_the_fragment_is_also_safe_unbound():
    """LIKE '%%' is two wildcards — matches exactly what '%' matches — so a
    future read that passes no params does not regress the other way."""
    from routes.paywall_hint_middleware import _ADMIN_EXCLUDE
    assert re.search(r"LIKE '/api/v1/admin/%%'", _ADMIN_EXCLUDE)


class _Cur:
    def __init__(self, log):
        self.log = log
        self._rows = []

    def execute(self, sql, params=None):
        _bind(sql, params)           # ★ the driver's step, first
        self.log.append(sql)
        self._rows = []

    def fetchall(self):
        return []

    def fetchone(self):
        return None                  # an empty window: no rows at all

    def close(self):
        pass


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cur(self.log)

    def close(self):
        pass


def test_funnel_ab_answers_200_on_a_bound_read(monkeypatch):
    import db_utils
    from routes import paywall_hint_middleware as m
    log = []

    @contextlib.contextmanager
    def fake_safe_db():
        yield _Conn(log)

    monkeypatch.setattr(db_utils, "safe_db", fake_safe_db)
    monkeypatch.setattr(m, "_admin_authorized", lambda: True)
    app = flask.Flask("funnel-ab-test")
    app.register_blueprint(m.paywall_ab_admin_bp)
    r = app.test_client().get("/api/v1/admin/funnel-ab?days=7")
    body = r.get_json()
    assert r.status_code == 200, body
    assert body["ok"] is True and body["total_4xx"] == 0 and body["window_days"] == 7
    assert len(log) == 3 and all("ab_funnel_log" in s for s in log)


def test_funnel_ab_stays_admin_gated(monkeypatch):
    from routes import paywall_hint_middleware as m
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    app = flask.Flask("funnel-ab-gate")
    app.register_blueprint(m.paywall_ab_admin_bp)
    assert app.test_client().get("/api/v1/admin/funnel-ab").status_code == 401


def test_the_scalar_read_is_guarded_without_a_tuple_default():
    src = open(os.path.join(ROOT, "routes", "paywall_hint_middleware.py"), encoding="utf-8").read()
    i = src.index("def funnel_ab_stats")
    body = src[i:i + 4000]
    assert "(cur.fetchone() or [0])[0]" not in body
    assert "total = int(_row[0] or 0) if _row else 0" in body
