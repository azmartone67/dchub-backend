"""tests/test_testimonial_write_admin_auth.py — admin auth on the
ai_testimonials write routes in main.py (2026-09-24).

ai_testimonials holds AI-assistant quotes and human customer quotes
(source='claim_quote'); /cited-by shows the approved claim_quote rows as named
DC Hub customers. Every route that creates, approves, deletes or bulk-edits
rows must therefore require X-Admin-Key, fail closed when no key is
configured, and leave the public reads alone.

The route functions and the gate are lifted out of main.py with ast and
registered on a bare Flask app WITH their real decorators, so a route that
loses @_require_testimonial_admin fails here. No Postgres: the connection is
a recording fake, and each test asserts whether any SQL ran.
"""
import ast
import contextlib
import hmac
import logging
import pathlib
from functools import wraps

import pytest

flask = pytest.importorskip("flask")

_MAIN = pathlib.Path(__file__).resolve().parent.parent / "main.py"
STRONG = "c81f" + "5e2a9d74" * 7 + "b3a0"  # synthetic 64-hex shape
_KEY_ENVS = ("DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY", "ADMIN_KEY")

# (method, path, json body, handler name in main.py)
WRITE_ROUTES = [
    ("POST", "/api/v1/testimonials",
     {"quote": "q", "source": "claim_quote", "auto_approve": True}, "add_testimonial"),
    ("POST", "/api/v1/testimonials/7/approve", {"featured": True}, "approve_testimonial"),
    ("DELETE", "/api/v1/testimonials/7", None, "delete_testimonial"),
    ("POST", "/api/v1/testimonials/seed", None, "seed_testimonials"),
    ("POST", "/api/v1/testimonials/bulk-approve", None, "bulk_approve_testimonials"),
    ("POST", "/api/v1/testimonials/cleanup", None, "cleanup_testimonials"),
    ("POST", "/api/v1/testimonials/refresh-timestamps", None,
     "refresh_testimonial_timestamps"),
    ("POST", "/api/v1/testimonials/test-capture", None, "test_auto_capture"),
]
_GATE = "_require_testimonial_admin"


def _is_testimonial_route(dec):
    return (isinstance(dec, ast.Call)
            and getattr(dec.func, "attr", None) == "route"
            and dec.args and isinstance(dec.args[0], ast.Constant)
            and str(dec.args[0].value).startswith("/api/v1/testimonials"))


def _route_methods(dec):
    for kw in dec.keywords:
        if kw.arg == "methods":
            return {e.value for e in kw.value.elts}
    return {"GET"}


def _main_tree():
    return ast.parse(_MAIN.read_text(encoding="utf-8"))


# ─────────────────────────────── fakes ──────────────────────────────────
class _Cur:
    rowcount = 1

    def __init__(self, log):
        self._log = log

    def execute(self, sql, params=None):
        self._log.append(sql)

    def fetchone(self):
        return (42,)

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, log):
        self._log = log

    def cursor(self):
        return _Cur(self._log)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def harness(monkeypatch):
    for name in _KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    sql_log = []

    @contextlib.contextmanager
    def pg_connection():
        yield _Conn(sql_log)

    app = flask.Flask("testimonial_write_auth")
    ns = {
        "app": app, "request": flask.request, "jsonify": flask.jsonify,
        "wraps": wraps, "hmac": hmac, "logger": logging.getLogger("t"),
        "get_pg_connection": lambda: _Conn(sql_log),
        "pg_connection": pg_connection,
    }
    wanted = {_GATE} | {r[3] for r in WRITE_ROUTES}
    lifted = [n for n in _main_tree().body
              if isinstance(n, ast.FunctionDef) and n.name in wanted]
    # The gate is defined before any route that uses it.
    lifted.sort(key=lambda n: n.name != _GATE)
    assert {n.name for n in lifted} == wanted, (
        "could not lift %s from main.py" % sorted(wanted - {n.name for n in lifted}))
    exec(compile(ast.Module(body=lifted, type_ignores=[]), str(_MAIN), "exec"), ns)
    return app.test_client(), sql_log


def _call(client, method, path, body, headers=None):
    return client.open(path, method=method, json=body, headers=headers or {})


# ─────────────────────────────── tests ──────────────────────────────────
@pytest.mark.parametrize("method,path,body,name", WRITE_ROUTES,
                         ids=[r[3] for r in WRITE_ROUTES])
def test_write_route_refuses_without_key(harness, monkeypatch, method, path, body, name):
    client, sql_log = harness
    monkeypatch.setenv("DCHUB_ADMIN_KEY", STRONG)
    r = _call(client, method, path, body)
    assert r.status_code == 401, "%s %s answered %s with no key" % (method, path, r.status_code)
    assert "no-store" in r.headers.get("Cache-Control", "")
    assert sql_log == [], "%s ran SQL before auth: %r" % (name, sql_log)


@pytest.mark.parametrize("method,path,body,name", WRITE_ROUTES,
                         ids=[r[3] for r in WRITE_ROUTES])
def test_write_route_refuses_wrong_key(harness, monkeypatch, method, path, body, name):
    client, sql_log = harness
    monkeypatch.setenv("DCHUB_ADMIN_KEY", STRONG)
    r = _call(client, method, path, body, {"X-Admin-Key": STRONG[:-1] + "f"})
    assert r.status_code == 401
    assert sql_log == []


@pytest.mark.parametrize("method,path,body,name", WRITE_ROUTES,
                         ids=[r[3] for r in WRITE_ROUTES])
def test_write_route_fails_closed_when_key_unconfigured(harness, method, path, body, name):
    """No DCHUB_ADMIN_KEY on the service must not mean 'open'. An empty
    header must not match an empty configured key either."""
    client, sql_log = harness
    for hdr in ({}, {"X-Admin-Key": ""}, {"X-Admin-Key": STRONG}):
        r = _call(client, method, path, body, hdr)
        assert r.status_code == 401, (hdr, r.status_code)
    assert sql_log == []


@pytest.mark.parametrize("method,path,body,name", WRITE_ROUTES,
                         ids=[r[3] for r in WRITE_ROUTES])
def test_write_route_works_with_key(harness, monkeypatch, method, path, body, name):
    client, sql_log = harness
    monkeypatch.setenv("DCHUB_ADMIN_KEY", STRONG)
    r = _call(client, method, path, body, {"X-Admin-Key": STRONG})
    assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
    assert r.get_json().get("success") is True
    assert sql_log, "%s authenticated but ran no SQL" % name


def test_weak_configured_key_is_not_accepted(harness, monkeypatch):
    """The gate goes through util.admin_auth, so a weak DCHUB_ADMIN_KEY is
    refused even when the caller presents that exact value."""
    client, sql_log = harness
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "admin")
    r = _call(client, "POST", "/api/v1/testimonials/bulk-approve", None,
              {"X-Admin-Key": "admin"})
    assert r.status_code == 401
    assert sql_log == []


def test_every_testimonial_write_route_in_main_is_gated():
    """Catches a NEW write route under /api/v1/testimonials added to main.py
    without the gate, and a public read that gets gated by mistake."""
    writes, reads = {}, {}
    for node in ast.walk(_main_tree()):
        if not isinstance(node, ast.FunctionDef):
            continue
        names = {getattr(d, "id", None) for d in node.decorator_list}
        for dec in node.decorator_list:
            if _is_testimonial_route(dec):
                bucket = writes if _route_methods(dec) - {"GET", "HEAD"} else reads
                bucket[node.name] = _GATE in names
    assert set(writes) == {r[3] for r in WRITE_ROUTES}, (
        "testimonial write routes in main.py changed: %s — add the new one to "
        "WRITE_ROUTES (and gate it)" % sorted(writes))
    assert all(writes.values()), "ungated: %s" % [k for k, v in writes.items() if not v]
    assert {"get_testimonials", "testimonial_stats"} <= set(reads)
    assert not any(reads.values()), "public read gated: %s" % [k for k, v in reads.items() if v]
