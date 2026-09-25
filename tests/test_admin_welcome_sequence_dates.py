"""/api/v1/admin/welcome-sequence must survive offset-carrying users.created_at.

users.created_at is TEXT. Rows written before the utcnow retirement are bare
ISO (naive); later writers emit an offset (`+00:00`). The route parsed each one
and compared it to a naive utcnow() OUTSIDE any try, so a single offset row
raised "can't compare offset-naive and offset-aware datetimes" and the whole
route answered 500 (the edge turned that into 503). Seen 2026-09-25 from the
first keyed churn-watcher runs: churn-risk 200, welcome-sequence 500 in ~440ms.

The handler is extracted from main.py by AST and run against a stub cursor —
importing main.py boots the whole app.
"""
import ast
import os
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone

import flask
import pytest

MAIN = pathlib.Path(__file__).resolve().parent.parent / "main.py"


def _load_handler(rows):
    tree = ast.parse(MAIN.read_text())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_admin_welcome_sequence")
    fn.decorator_list = []
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(MAIN), "exec"), ns)

    class _Cur:
        def execute(self, *a, **k): pass
        def fetchall(self): return rows
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake = types.ModuleType("psycopg2")
    fake.connect = lambda *a, **k: _Conn()
    return ns["_admin_welcome_sequence"], fake


def _call(rows, monkeypatch):
    handler, fake = _load_handler(rows)
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub")
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    app = flask.Flask(__name__)
    with app.test_request_context("/api/v1/admin/welcome-sequence?days=7",
                                  headers={"X-Admin-Key": "k"}):
        rv = handler()
    resp, status = (rv if isinstance(rv, tuple) else (rv, 200))
    return status, resp.get_json()


def _row(email, created, calls):
    return (email, "n", "pro", created, calls, None, "key", None)


def test_offset_and_bare_created_at_both_count(monkeypatch):
    recent = datetime.now(timezone.utc) - timedelta(days=1)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    rows = [
        _row("a@x", recent.isoformat(), 0),                              # +00:00
        _row("b@x", recent.replace(tzinfo=None).isoformat(), 0),         # bare
        _row("c@x", recent.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), 3),        # Z
        _row("d@x", recent, 0),                                          # timestamptz
        _row("e@x", old.isoformat(), 0),                                 # outside window
    ]
    status, body = _call(rows, monkeypatch)
    assert status == 200, body
    assert body["still_zero_calls_count"] == 3
    assert body["first_call_made_count"] == 1
    assert {c["email"] for c in body["still_zero_calls"]} == {"a@x", "b@x", "d@x"}


def test_unparseable_created_at_is_skipped_not_fatal(monkeypatch):
    status, body = _call([_row("z@x", "not-a-date", 0)], monkeypatch)
    assert status == 200, body
    assert body["still_zero_calls_count"] == 0
