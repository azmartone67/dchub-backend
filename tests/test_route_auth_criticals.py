"""tests/test_route_auth_criticals.py — audit-CRITICAL route-auth remediation.

Guards the fix that gated/validated the unauth audit-critical routes (social
test, qa autofix arm+fire, tax-incentives PUT, GSC writes, intelligence
linkedin-post) and validated team_create against Stripe.

House rules (see tests/conftest.py + CLAUDE.md): NO DB, NEVER import main.py.
The route handlers are sliced out of their source with `ast` and executed
against stubs; the shared gate (internal_auth.require_internal_or_admin) is the
REAL function, exercised with real request headers + real env so the test proves
end-to-end that a no-credential request is REJECTED (401/403) before any sink and
that a valid X-Internal-Key / X-Admin-Key / ?admin_key is ACCEPTED (execution
reaches past the gate). Nothing runs at module scope.

Run:  python3 -m pytest tests/test_route_auth_criticals.py -q
"""
from __future__ import annotations

import ast
import contextlib
import functools
import os
import pathlib
import sys
import textwrap

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import internal_auth  # leaf module — no flask, no main, safe to import

_KEY = "test-internal-key-2026x"
_ADMIN = "test-admin-key-2026y"


# ── harness ───────────────────────────────────────────────────────────

class _PastGate(Exception):
    """Raised by a stubbed downstream sink to prove execution passed the gate."""


class _Hdrs(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class _FakeReq:
    def __init__(self, headers=None, args=None, method="POST", json=None, form=None):
        self.headers = _Hdrs(headers or {})
        self.args = _Hdrs(args or {})
        self.method = method
        self._json = json
        self.json = json
        self.form = _Hdrs(form or {})

    def get_json(self, *a, **k):
        return self._json


def _jsonify(*a, **k):
    return {"_payload": a[0] if a else k}


def _status(rv):
    if isinstance(rv, tuple) and len(rv) >= 2 and isinstance(rv[1], int):
        return rv[1]
    return 200


def _extract(relpath: str, name: str) -> str:
    """Slice `def name(` (dedented, decorators stripped) out of a source file."""
    src = (_ROOT / relpath).read_text(encoding="utf-8")
    lines = src.splitlines()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent("\n".join(lines[node.lineno - 1:node.end_lineno]))
    raise AssertionError(f"{name}() not found in {relpath}")


def _run(seg: str, name: str, ns: dict, req, *call_args):
    ns = dict(ns)
    ns.setdefault("jsonify", _jsonify)
    ns["require_internal_or_admin"] = internal_auth.require_internal_or_admin
    ns["request"] = req
    exec(compile(seg, "<handler>", "exec"), ns)
    fn = ns[name]
    try:
        return fn(*call_args)
    except _PastGate:
        return ("__PAST__",)


@contextlib.contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _reached(rv) -> bool:
    """True when execution got PAST the gate (a sink sentinel fired, or the
    handler returned any non-401/403 status)."""
    if rv == ("__PAST__",):
        return True
    return _status(rv) not in (401, 403)


def _rejected(rv) -> bool:
    return rv != ("__PAST__",) and _status(rv) in (401, 403)


# ── the shared canonical gate ─────────────────────────────────────────

def test_require_internal_or_admin_accepts_each_repo_standard_slot():
    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN,
              DCHUB_SYNC_KEY=None, INTERNAL_WORKER_SECRET=None):
        f = internal_auth.require_internal_or_admin
        assert f(_FakeReq(headers={"X-Internal-Key": _KEY})) is True
        assert f(_FakeReq(headers={"X-Admin-Key": _ADMIN})) is True
        assert f(_FakeReq(args={"admin_key": _ADMIN})) is True
        # fail-closed: nothing, or a wrong value
        assert f(_FakeReq()) is False
        assert f(_FakeReq(headers={"X-Internal-Key": "nope"})) is False


def test_require_internal_or_admin_fails_closed_when_env_unset():
    with _env(DCHUB_INTERNAL_KEY=None, DCHUB_ADMIN_KEY=None,
              DCHUB_SYNC_KEY=None, INTERNAL_WORKER_SECRET=None):
        # even a plausible header cannot pass when NO secret is configured
        assert internal_auth.require_internal_or_admin(
            _FakeReq(headers={"X-Internal-Key": _KEY})) is False


# ── per-endpoint: rejects no-key, accepts X-Internal-Key / X-Admin-Key ─

def _assert_gate(relpath, name, ns_factory, nokey_req_kw, withkey_req_kw,
                 call_args=()):
    seg = _extract(relpath, name)
    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN):
        # no credential → rejected before any sink
        assert _rejected(_run(seg, name, ns_factory(), _FakeReq(**nokey_req_kw),
                              *call_args)), \
            f"{name} did not reject a no-key request"
        # X-Internal-Key → accepted (execution reaches past the gate)
        r_int = dict(withkey_req_kw)
        r_int["headers"] = dict(r_int.get("headers", {}), **{"X-Internal-Key": _KEY})
        assert _reached(_run(seg, name, ns_factory(), _FakeReq(**r_int),
                             *call_args)), \
            f"{name} did not accept a valid X-Internal-Key"
        # X-Admin-Key → accepted
        r_adm = dict(withkey_req_kw)
        r_adm["headers"] = dict(r_adm.get("headers", {}), **{"X-Admin-Key": _ADMIN})
        assert _reached(_run(seg, name, ns_factory(), _FakeReq(**r_adm),
                             *call_args)), \
            f"{name} did not accept a valid X-Admin-Key"


def test_autopilot_routes_social_test_gated():
    _assert_gate(
        "routes/autopilot_routes.py", "social_test",
        lambda: {"_AUTOPILOT_AVAILABLE": False, "_discovery_engine": None},
        {"method": "POST"}, {"method": "POST"})
    # GET must also be gated (the GET path reaches the sink too)
    seg = _extract("routes/autopilot_routes.py", "social_test")
    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN):
        ns = {"_AUTOPILOT_AVAILABLE": False, "_discovery_engine": None}
        assert _rejected(_run(seg, "social_test", ns, _FakeReq(method="GET")))


def test_auto_pilot_dead_twin_social_test_gated():
    _assert_gate(
        "auto_pilot.py", "social_test",
        lambda: {"_AUTOPILOT_AVAILABLE": False, "_discovery_engine": None},
        {"method": "POST"}, {"method": "POST"})


def test_intelligence_engine_linkedin_post_gated():
    def sink(*a, **k):
        raise _PastGate()
    _assert_gate(
        "intelligence_engine.py", "api_linkedin_post",
        lambda: {"post_to_linkedin": sink, "generate_linkedin_post": sink},
        {"json": None}, {"json": {"content": "x"}})


def test_linkedin_autopost_post_now_gated():
    # get_json() -> None makes the post-gate body return 400 (past the gate).
    _assert_gate(
        "linkedin_autopost.py", "linkedin_post_now",
        lambda: {}, {"json": None}, {"json": None})


def test_infrastructure_weekly_digest_post_gated():
    # Live registered unauth LinkedIn post — gated in the residual-gap wave.
    def sink(*a, **k):
        raise _PastGate()

    class _LI:
        generate_weekly_digest = staticmethod(sink)
        post_to_linkedin = staticmethod(sink)
        save_weekly_post = staticmethod(sink)

    _assert_gate(
        "infrastructure_discovery.py", "post_weekly_digest",
        lambda: {"engine": type("E", (), {"linkedin": _LI()})()},
        {"method": "POST"}, {"method": "POST"})


def test_gsc_status_gated():
    def sink(*a, **k):
        raise _PastGate()
    _assert_gate(
        "google_search_console.py", "gsc_status",
        lambda: {"get_access_token": sink},
        {"method": "GET"}, {"method": "GET"})


def test_gsc_index_requests_gated():
    def sink(*a, **k):
        raise _PastGate()
    _assert_gate(
        "google_search_console.py", "get_index_requests",
        lambda: {"get_db": sink},
        {"method": "GET"}, {"method": "GET"})


def test_qa_fix_pattern_gated():
    def sink(*a, **k):
        raise _PastGate()
    _assert_gate(
        "routes/qa_patterns.py", "fix_pattern",
        lambda: {"run_auto_fix": sink},
        {"args": {}}, {"args": {}}, call_args=(1,))


def test_qa_assign_fix_gated():
    # unknown fix_func_name -> 400 past the gate; empty FIX_REGISTRY, no DB.
    _assert_gate(
        "routes/qa_patterns.py", "assign_fix",
        lambda: {"FIX_REGISTRY": {}, "json": None},
        {"json": {"fix_func_name": "nope"}}, {"json": {"fix_func_name": "nope"}},
        call_args=(1,))


def test_qa_learn_novel_gated():
    def sink(*a, **k):
        raise _PastGate()
    _assert_gate(
        "routes/qa_patterns.py", "learn_novel",
        lambda: {"_ensure_tables": sink}, {}, {})


def test_qa_list_patterns_gated():
    def sink(*a, **k):
        raise _PastGate()
    _assert_gate(
        "routes/qa_patterns.py", "list_patterns",
        lambda: {"_ensure_tables": sink}, {"method": "GET"}, {"method": "GET"})


def test_qa_coverage_gated():
    def sink(*a, **k):
        raise _PastGate()
    _assert_gate(
        "routes/qa_patterns.py", "coverage",
        lambda: {"_ensure_tables": sink}, {"method": "GET"}, {"method": "GET"})


def test_tax_incentives_put_gated():
    # past the gate, abbr not in {} -> 404 (proves acceptance without a DB).
    _assert_gate(
        "tax_incentives_routes.py", "update_state_incentive",
        lambda: {"incentives_data": {}},
        {"method": "PUT"}, {"method": "PUT"}, call_args=("CA",))


def test_tax_incentives_put_options_preflight_still_open():
    """OPTIONS preflight short-circuits (204) BEFORE the gate — CORS must work."""
    seg = _extract("tax_incentives_routes.py", "update_state_incentive")
    ns = {"incentives_data": {}}
    ns["require_internal_or_admin"] = internal_auth.require_internal_or_admin
    ns["jsonify"] = _jsonify
    ns["request"] = _FakeReq(method="OPTIONS")
    exec(compile(seg, "<h>", "exec"), ns)
    rv = ns["update_state_incentive"]("CA")
    assert _status(rv) == 204


def test_gsc_verify_gated():
    def token(*a, **k):
        raise _PastGate()
    _assert_gate(
        "google_search_console.py", "gsc_verify",
        lambda: {"get_access_token": token}, {}, {})


def test_gsc_require_gsc_auth_decorator_gates_the_caller():
    """Folding the caller check into require_gsc_auth gates submit/delete/
    indexing-request/reads in one place — before the server->Google token.
    The `decorated` wrapper reads the module-global `request`, which lives in the
    exec namespace == wrapped.__globals__, so we rebind it there per call."""
    seg = _extract("google_search_console.py", "require_gsc_auth")

    def token(*a, **k):
        raise _PastGate()  # reached only AFTER the caller gate passes

    ns = {"wraps": functools.wraps, "jsonify": _jsonify, "get_access_token": token,
          "require_internal_or_admin": internal_auth.require_internal_or_admin}
    exec(compile(seg, "<h>", "exec"), ns)
    wrapped = ns["require_gsc_auth"](lambda tok: (_jsonify({"ok": True}), 200))

    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN):
        ns["request"] = _FakeReq(method="POST")            # no credential
        assert _status(wrapped()) == 401
        ns["request"] = _FakeReq(method="POST", headers={"X-Internal-Key": _KEY})
        with pytest.raises(_PastGate):                     # gate passed -> token mint
            wrapped()
        ns["request"] = _FakeReq(method="POST", headers={"X-Admin-Key": _ADMIN})
        with pytest.raises(_PastGate):
            wrapped()


# ── team_accounts: validate the Stripe subscription before minting ────

def _load_team_validator(fake_stripe):
    seg = _extract("routes/team_accounts.py", "_sub_get") + "\n\n" + \
        _extract("routes/team_accounts.py", "_validate_team_subscription")
    ns = {"os": os, "logger": __import__("logging").getLogger("t"),
          "_TEAM_STRIPE_PRICE_ID": "price_team", "Optional": __import__("typing").Optional}
    sys.modules["stripe"] = fake_stripe
    exec(compile(seg, "<team>", "exec"), ns)
    return ns["_validate_team_subscription"]


class _Stripe:
    api_key = None

    def __init__(self, ret=None, raise_=False):
        self._ret = ret
        self._raise = raise_
        outer = self

        class _Sub:
            @staticmethod
            def retrieve(sid):
                if outer._raise:
                    raise Exception("stripe boom")
                return outer._ret
        self.Subscription = _Sub


def test_team_create_rejects_absent_subscription():
    val = _load_team_validator(_Stripe(ret=None))
    with _env(STRIPE_SECRET_KEY="sk_test"):
        ok, err, code = val(None, "a@b.com")
        assert ok is False and err == "missing_subscription" and code == 400


def test_team_create_rejects_inactive_or_price_mismatch():
    with _env(STRIPE_SECRET_KEY="sk_test"):
        # canceled subscription
        val = _load_team_validator(_Stripe(ret={"status": "canceled",
            "items": {"data": [{"price": {"id": "price_team"}}]}}))
        ok, err, code = val("sub_1", "a@b.com")
        assert ok is False and err == "subscription_not_active" and code == 402
        # active but wrong price
        val = _load_team_validator(_Stripe(ret={"status": "active",
            "items": {"data": [{"price": {"id": "price_OTHER"}}]}}))
        ok, err, code = val("sub_1", "a@b.com")
        assert ok is False and err == "price_mismatch" and code == 402


def test_team_create_rejects_on_stripe_lookup_error_failclosed():
    val = _load_team_validator(_Stripe(raise_=True))
    with _env(STRIPE_SECRET_KEY="sk_test"):
        ok, err, code = val("sub_1", "a@b.com")
        assert ok is False and err == "subscription_lookup_failed" and code == 402


def test_team_create_rejects_when_stripe_not_configured():
    val = _load_team_validator(_Stripe(ret={"status": "active"}))
    with _env(STRIPE_SECRET_KEY=None):
        ok, err, code = val("sub_1", "a@b.com")
        assert ok is False and err == "stripe_not_configured" and code == 503


def test_team_create_accepts_active_matching_subscription():
    val = _load_team_validator(_Stripe(ret={"status": "active",
        "items": {"data": [{"price": {"id": "price_team"}}]}}))
    with _env(STRIPE_SECRET_KEY="sk_test"):
        ok, err, code = val("sub_1", "a@b.com")
        assert ok is True and code == 200
        # trialing is also acceptable
    val = _load_team_validator(_Stripe(ret={"status": "trialing",
        "items": {"data": [{"price": {"id": "price_team"}}]}}))
    with _env(STRIPE_SECRET_KEY="sk_test"):
        ok, _e, code = val("sub_1", "a@b.com")
        assert ok is True and code == 200


def _load_team_view():
    seg = _extract("routes/team_accounts.py", "_team_view")
    ns: dict = {}
    exec(compile(seg, "<tv>", "exec"), ns)
    return ns["_team_view"]


def test_team_view_redacts_key_for_email_only_caller():
    # The idempotent /team/create branch must not hand an existing team's live
    # Pro key to a caller who only knows the owner email (no admin, no sub proof).
    tv = _load_team_view()
    team = {"id": 1, "owner_email": "a@b.com",
            "stripe_subscription_id": "sub_X", "shared_api_key": "dch_team_secret"}
    r = tv(team, None, False)            # unauth, no subscription presented
    assert "shared_api_key" not in r and r.get("key_redacted") is True
    r2 = tv(team, "sub_WRONG", False)    # wrong subscription id
    assert "shared_api_key" not in r2


def test_team_view_returns_key_for_admin_or_matching_subscription():
    tv = _load_team_view()
    team = {"id": 1, "stripe_subscription_id": "sub_X",
            "shared_api_key": "dch_team_secret"}
    assert tv(team, None, True)["shared_api_key"] == "dch_team_secret"      # admin
    assert tv(team, "sub_X", False)["shared_api_key"] == "dch_team_secret"  # owns sub


# ── IndexNow admin surface (2026-08-09) ───────────────────────────────
# routes/indexnow_route.py shipped a SECOND IndexNow blueprint whose POST
# /api/v1/admin/indexnow/submit had a docstring saying "Admin-only" and NO gate
# at all — an anonymous POST went straight to request.get_json() (verified live:
# 400 "missing 'urls' array", a response from INSIDE the handler, not a 401).
# It submitted under our published key, so any caller could burn the 10,000
# URL/day per-host quota. Gated in #2478, then deleted as fully redundant.
#
# routes/indexnow.py is now the ONLY IndexNow surface. These guard it, and
# guard against a third ungated twin appearing.

def _run_indexnow_endpoint(req):
    """Exec the REAL indexnow_endpoint together with the REAL _admin_ok/_wants
    it calls, in ONE namespace, so the gate under test is the shipped code and
    not a stub. submit_to_indexnow is the sink that proves we got past it."""
    def _sink(*a, **k):
        raise _PastGate("reached submit_to_indexnow")

    ns = {
        "jsonify": _jsonify, "_ADMIN_KEY": _ADMIN, "HOST": "dchub.cloud",
        "KEY_LOCATION": "https://dchub.cloud/k.txt",
        "_load_last": lambda: {}, "submit_to_indexnow": _sink,
        "ping_new_facilities": _sink, "_recent_facility_urls": lambda n: [],
        "_recent_dcpi_urls": lambda n: [], "_sitemap_recent": lambda n: [],
    }
    for fname in ("_admin_ok", "_wants", "indexnow_endpoint"):
        exec(compile(_extract("routes/indexnow.py", fname), "<h>", "exec"), ns)
    ns["request"] = req          # after exec: these share ns as __globals__
    try:
        return ns["indexnow_endpoint"]()
    except _PastGate:
        return ("__PAST__",)


def test_indexnow_endpoint_submit_modes_are_gated():
    for mode in ("recent", "facilities", "dcpi", "delta"):
        for method in ("POST", "GET"):
            rv = _run_indexnow_endpoint(
                _FakeReq(method=method, args={mode: "1"}))
            assert _rejected(rv), \
                f"{method} ?{mode}=1 was not gated (got {_status(rv)})"


def test_indexnow_endpoint_accepts_admin_key():
    rv = _run_indexnow_endpoint(
        _FakeReq(method="POST", args={"recent": "1"},
                 headers={"X-Admin-Key": _ADMIN}))
    assert rv == ("__PAST__",), "valid X-Admin-Key did not reach the submitter"


def test_indexnow_endpoint_public_read_stays_public():
    """The no-mode GET is a DELIBERATE public read (config + last-submit
    status, no submit). Gating it would break the health dashboard; this
    pins that boundary so the line stays intentional."""
    rv = _run_indexnow_endpoint(_FakeReq(method="GET"))
    assert _status(rv) == 200 and rv != ("__PAST__",)


# ── every route that can reach IndexNow decides auth first (2026-09-11) ──
# The check this replaces keyed on the PATH (/admin/indexnow) and walked
# routes/ only. seo_agent.py, a repo-root module, served POST
# /api/seo/indexnow/ping, /ping-all and /run-cycle with no gate, each calling
# ping_indexnow: an anonymous POST submitted URLs under our key (verified live
# 2026-09-11: a malformed body got the handler's own 400, where the gated
# /api/v1/admin/indexnow answers 401). Neither the path nor the directory
# matched, so nothing looked. This keys on the submit itself, over both
# directories the route surface lives in.

_INDEXNOW_SINKS = frozenset({"submit_to_indexnow", "ping_indexnow",
                             "ping_new_facilities"})
_INDEXNOW_GATES = frozenset({"require_internal_or_admin", "_admin_ok"})
# A decorator with one of these names counts as a gate here. Whether it fails
# CLOSED is tests/test_admin_gate_fail_closed.py's ratchet (marketing_engine's
# _require_admin is on it), not this test's.
_INDEXNOW_GATE_DECORATORS = frozenset({"require_internal_or_admin",
                                       "_require_admin"})
_ROUTE_DECORATORS = frozenset({"route", "get", "post", "put", "patch", "delete"})

# Route handlers that reach IndexNow with no gate: known, and keyed by the exact
# handler so a new one is never exempt. An entry that stops being an offender
# fails the test too, so the fix deletes its line and this set only shrinks.
_KNOWN_UNGATED_INDEXNOW_REACH = frozenset({
    # GET|POST /api/cron/daily starts a thread that calls submit_to_indexnow
    # (and posts to LinkedIn). Railway counted 14 calls in the 7 days to
    # 2026-09-11, all 2xx, from a caller whose credential nobody has checked,
    # so gating it without that caller could stop the daily job.
    "main.py::daily_cron",
})


def _call_name(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)


def _route_paths(fn):
    return [d.args[0].value for d in fn.decorator_list
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and d.func.attr in _ROUTE_DECORATORS and d.args
            and isinstance(d.args[0], ast.Constant)
            and isinstance(d.args[0].value, str)
            and d.args[0].value.startswith("/")]


def _reaches_indexnow(node):
    """A call to an IndexNow submitter, or an IndexNow URL, anywhere under
    `node`. Nested defs count: a thread the handler starts is still reached."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and _call_name(n) in _INDEXNOW_SINKS:
            return True
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value.startswith("http") and "indexnow" in n.value.lower()):
            return True
    return False


def _gate_decides_first(fn):
    """True when auth is decided before anything can reach IndexNow: a gate
    decorator, or a top-level `if` that answers 401/403 when a gate says no
    (`not gate(...)`, or `not x` for `x = gate(...)`), ahead of the first
    statement that reaches a submitter. A gate that runs after the submit, one
    whose answer is ignored or inverted, and one that only a comment or
    docstring names, all leave the handler ungated."""
    for d in fn.decorator_list:
        node = d.func if isinstance(d, ast.Call) else d
        if (getattr(node, "id", None) or getattr(node, "attr", None)) \
                in _INDEXNOW_GATE_DECORATORS:
            return True
    bound = set()
    for stmt in fn.body:
        if (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)
                and _call_name(stmt.value) in _INDEXNOW_GATES):
            bound.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        if isinstance(stmt, ast.If):
            says_no = any(
                isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not)
                and ((isinstance(n.operand, ast.Call)
                      and _call_name(n.operand) in _INDEXNOW_GATES)
                     or (isinstance(n.operand, ast.Name) and n.operand.id in bound))
                for n in ast.walk(stmt.test))
            refuses = any(
                isinstance(r, ast.Return) and any(
                    isinstance(k, ast.Constant) and k.value in (401, 403)
                    for k in ast.walk(r))
                for s in stmt.body for r in ast.walk(s))
            if says_no and refuses:
                return True
        if _reaches_indexnow(stmt):
            return False
    return False


def _indexnow_reach():
    """({"<file>::<handler>": gated}, files parsed) for every route handler in
    a repo-root module or routes/ that can reach IndexNow."""
    reach, parsed = {}, set()
    for path in (sorted(_ROOT.glob("*.py"))
                 + sorted((_ROOT / "routes").glob("*.py"))):
        src = path.read_text(encoding="utf-8")
        # A module that never spells a submitter or the service cannot call one.
        if "indexnow" not in src.lower() \
                and not any(s in src for s in _INDEXNOW_SINKS):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        rel = path.relative_to(_ROOT).as_posix()
        parsed.add(rel)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            paths = _route_paths(fn)
            if paths and (any("indexnow" in p.lower() for p in paths)
                          or _reaches_indexnow(fn)):
                key = f"{rel}::{fn.name}"
                reach[key] = reach.get(key, True) and _gate_decides_first(fn)
    return reach, parsed


def test_every_route_that_reaches_indexnow_decides_auth_first():
    """No route handler in a repo-root module or routes/ may reach an IndexNow
    submitter unless a gate has answered 401/403 first."""
    reach, parsed = _indexnow_reach()
    # A scan that read nothing, or a detector that found nothing, is green about
    # nothing. The files this exists for must have been read, and the routes it
    # judges must be found and judged as they are written.
    assert {"seo_agent.py", "main.py", "routes/indexnow.py"} <= parsed, \
        sorted(parsed)
    assert reach.get("routes/indexnow.py::indexnow_endpoint") is True, reach
    assert reach.get("routes/marketing_engine.py::auto_generate") is True, reach

    offenders = {k for k, gated in reach.items() if not gated}
    new = sorted(offenders - _KNOWN_UNGATED_INDEXNOW_REACH)
    assert not new, (
        f"route handler(s) reach IndexNow with no gate deciding first: {new}. "
        "Answer 401 on `not internal_auth.require_internal_or_admin(request)` "
        "before anything submits, or remove the route.")
    stale = sorted(_KNOWN_UNGATED_INDEXNOW_REACH - offenders)
    assert not stale, (
        f"{stale} no longer reach IndexNow ungated: delete them from "
        "_KNOWN_UNGATED_INDEXNOW_REACH in the same change.")


def test_indexnow_twin_module_stays_deleted():
    """routes/indexnow_route.py was deleted 2026-08-09. Its GET /{KEY}.txt —
    the stated reason it existed — never ran in production: root *.txt is not
    in the frontend's _routes.json 'include', so CF Pages serves the STATIC key
    files and the request never reaches this backend. Re-adding the module
    re-adds a duplicate submit path and a second key default."""
    assert not (_ROOT / "routes/indexnow_route.py").exists()
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "from routes.indexnow_route import" not in main_src
