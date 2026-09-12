"""tests/test_ungated_indexnow_reach.py — the ungated IndexNow/publishing sweep.

Six route handlers reached an IndexNow submitter or a social publisher with no
gate deciding first. Each is now gated with internal_auth.require_internal_or_admin.

House rules (tests/conftest.py + CLAUDE.md): NO DB, NEVER import main.py. Each
handler is compiled out of its own source with `ast` (decorators stripped) and
run inside a real Flask test_request_context, so `from flask import request`
INSIDE a handler body resolves to the real request — the reason a stubbed
`request` in the namespace cannot test these (main.py's two import flask in the
body). The gate is the REAL internal_auth function against REAL headers and REAL
env, so a pass means an anonymous request is refused end to end.

Run:  python3 -m pytest tests/test_ungated_indexnow_reach.py -q
"""
from __future__ import annotations

import ast
import contextlib
import copy
import datetime as _dt
import functools
import os
import pathlib
import sys
import types

import pytest
from flask import Flask

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_KEY = "test-internal-key-2026x"
_ADMIN = "test-admin-key-2026y"


class _PastGate(Exception):
    """Raised by a stubbed sink to prove execution got past the gate."""


@contextlib.contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        yield
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


@functools.lru_cache(maxsize=None)
def _tree(relpath: str):
    return ast.parse((_ROOT / relpath).read_text(encoding="utf-8"))


def _compile_handler(relpath: str, name: str, ns: dict):
    """The REAL handler from its own source, decorators stripped, bound into ns.

    Not a copy in this file: a hand-written mirror goes green through its own
    regression while the served handler stays broken.
    """
    for node in ast.walk(_tree(relpath)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            fn = copy.deepcopy(node)
            fn.decorator_list = []
            mod = ast.Module(body=[fn], type_ignores=[])
            ast.fix_missing_locations(mod)
            exec(compile(mod, relpath, "exec"), ns)
            return ns[name]
    raise AssertionError(f"{name}() not found in {relpath}")


def _status(rv):
    if isinstance(rv, tuple) and len(rv) >= 2 and isinstance(rv[1], int):
        return rv[1]
    return getattr(rv, "status_code", 200)


def _call(relpath, name, ns, *, headers=None, method="POST", query=None):
    app = Flask(__name__)
    with app.test_request_context("/t", method=method,
                                  headers=headers or {},
                                  query_string=query or {}):
        import internal_auth
        from flask import jsonify as _jsonify, request as _request
        ns.setdefault("require_internal_or_admin",
                      internal_auth.require_internal_or_admin)
        # The REAL flask proxies, which is what these modules bind at module
        # level. main.py's two handlers import them inside the body instead and
        # get the same objects — either way the gate reads a real request.
        ns.setdefault("request", _request)
        ns.setdefault("jsonify", _jsonify)
        fn = _compile_handler(relpath, name, ns)
        try:
            return _status(fn())
        except _PastGate:
            return "PAST"


# (relpath, handler, extra globals the body reads, method)
_GATED = [
    ("main.py", "_v1_daily_preview", lambda: {}, "POST"),
    ("main.py", "_v1_media_publish", lambda: {}, "POST"),
    ("routes/autopilot_routes.py", "seo_run", lambda: {}, "POST"),
    ("auto_pilot.py", "seo_run", lambda: {}, "POST"),
    ("intelligence_engine.py", "api_daily_intelligence",
     lambda: {"run_daily_intelligence": _sink, "get_daily_stats": _sink}, "POST"),
    ("ai_outreach_agent.py", "run_outreach",
     lambda: {"_last_cycle_result": {}, "Thread": _SyncThread,
              "run_outreach_cycle": _sink, "datetime": _Clock,
              "timezone": _dt.timezone, "logger": _Logger()}, "POST"),
]

_IDS = [f"{f}::{n}" for f, n, _g, _m in _GATED]


def _sink(*a, **k):
    raise _PastGate()


class _SyncThread:
    """Thread that runs its target inline, so a cycle started past the gate is
    observable instead of racing off into a daemon."""
    def __init__(self, target=None, **kw):
        self._t = target

    def start(self):
        if self._t:
            self._t()


class _Clock:
    @staticmethod
    def now(*a, **k):
        class _N:
            @staticmethod
            def isoformat():
                return "2026-09-11T00:00:00+00:00"
        return _N()


class _Logger:
    def __getattr__(self, _n):
        return lambda *a, **k: None


@pytest.mark.parametrize("relpath,name,extra,method", _GATED, ids=_IDS)
def test_no_credential_is_refused_and_the_body_never_runs(relpath, name, extra, method):
    """An anonymous request gets 401 and never reaches the sink.

    The sink stubs raise _PastGate, so "PAST" here would mean the handler ran
    its publishing body for a caller with no credential — the bug itself.
    """
    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN):
        got = _call(relpath, name, extra(), headers={}, method=method)
    assert got == 401, f"{relpath}::{name} answered {got} to an anonymous {method}"


class _StubMod(types.ModuleType):
    def __getattr__(self, _n):
        return _sink


@contextlib.contextmanager
def _no_heavy_engines():
    """Stand in for the publishing engines a handler imports INSIDE its body.

    Only reached once a call is already past the gate, so this cannot make a
    gate failure look like a pass: a handler that refuses returns 401 before the
    import runs, and the accept-direction assertion is `!= 401`. It keeps the
    suite off the real seo_promotion_engine / dchub_media import chains, which
    open a DB pool (house rule: no DB).
    """
    names = ("seo_promotion_engine", "dchub_media")
    old = {n: sys.modules.get(n) for n in names}
    try:
        for n in names:
            sys.modules[n] = _StubMod(n)
        yield
    finally:
        for n, m in old.items():
            sys.modules.pop(n, None) if m is None else sys.modules.__setitem__(n, m)


@pytest.mark.parametrize("relpath,name,extra,method", _GATED, ids=_IDS)
def test_a_valid_internal_key_gets_past_the_gate(relpath, name, extra, method):
    """The gate is not a wall: a real X-Internal-Key is let through.

    Without this, deleting the handler body would also make the test above pass.
    """
    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN), _no_heavy_engines():
        got = _call(relpath, name, extra(), method=method,
                    headers={"X-Internal-Key": _KEY})
    assert got != 401, f"{relpath}::{name} refused a valid X-Internal-Key"


@pytest.mark.parametrize("relpath,name,extra,method", _GATED, ids=_IDS)
def test_admin_key_header_is_accepted(relpath, name, extra, method):
    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN), _no_heavy_engines():
        got = _call(relpath, name, extra(), method=method,
                    headers={"X-Admin-Key": _ADMIN})
    assert got != 401, f"{relpath}::{name} refused a valid X-Admin-Key"


@pytest.mark.parametrize("relpath,name,extra,method", _GATED, ids=_IDS)
def test_no_secret_configured_still_refuses(relpath, name, extra, method):
    """Fail CLOSED. A gate keyed on an import-time snapshot, or one that skips
    when the env is unset, answers 200 here — the #4411 defect class."""
    with _env(DCHUB_INTERNAL_KEY=None, DCHUB_SYNC_KEY=None,
              INTERNAL_WORKER_SECRET=None, DCHUB_ADMIN_KEY=None):
        got = _call(relpath, name, extra(), method=method,
                    headers={"X-Internal-Key": _KEY})
    assert got == 401, f"{relpath}::{name} answered {got} with NO secret configured"


def test_wrong_key_is_refused():
    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN):
        got = _call("routes/autopilot_routes.py", "seo_run", {},
                    headers={"X-Internal-Key": "not-the-key"})
    assert got == 401


def test_daily_preview_post_true_cannot_publish_anonymously():
    """?post=true is the publishing branch — the one query parameter that turns
    the preview into dchub_media.run_daily(). It must be refused too."""
    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN):
        got = _call("main.py", "_v1_daily_preview", {},
                    headers={}, query={"post": "true"})
    assert got == 401


def test_admin_html_sends_a_parameter_the_gate_actually_reads():
    """static/admin.html's outreach helper sent ?key=, which
    require_internal_or_admin does not read (X-Internal-Key, X-Admin-Key,
    ?admin_key only). Gating /api/outreach/trigger without this would have left
    the admin page 401ing on every click."""
    src = (_ROOT / "static/admin.html").read_text(encoding="utf-8")
    i = src.index("async function orApiFetch")
    body = src[i:i + 1200]
    assert "admin_key=${ADMIN_KEY}" in body, "orApiFetch no longer sends ?admin_key"
    assert "}${sep}key=${ADMIN_KEY}" not in body, "orApiFetch still sends bare ?key="


def test_agent_hub_pages_have_no_anonymous_trigger():
    """/agent-hub is an ungated public HTML page. It shipped a Force Cycle
    button that POSTed to /api/outreach/run with no credential at all — the
    exposure, not a caller worth preserving."""
    for rel in ("agent_hub.py", "agent-hub.html", "static/agent-hub.html"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "forceOutreach" not in src, f"{rel} still wires an anonymous trigger"
