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


# ── indexnow_route.indexnow_submit (2026-08-09) ───────────────────────
# routes/indexnow_route.py POST /api/v1/admin/indexnow/submit had a docstring
# that said "Admin-only" and NO gate at all — an anonymous POST went straight
# to request.get_json() (verified live: 400 "missing 'urls' array", i.e. a
# response from INSIDE the handler, not a 401). It submits under our published
# IndexNow key, so any caller could burn the 10,000 URL/day per-host quota.

def test_indexnow_submit_gated():
    # authed + empty body → the handler's own 400 (past the gate, no network).
    _assert_gate(
        "routes/indexnow_route.py", "indexnow_submit",
        lambda: {"_key": lambda: "k", "json": None, "urllib": None},
        {"method": "POST", "json": {}}, {"method": "POST", "json": {}})


def test_indexnow_submit_rejects_before_reading_body():
    """The gate must run BEFORE request.get_json() — a rejected caller must not
    even reach body parsing (that ordering is what the live 400 disproved)."""
    seg = _extract("routes/indexnow_route.py", "indexnow_submit")

    class _Exploding(_FakeReq):
        def get_json(self, *a, **k):
            raise _PastGate("body parsed before the gate ran")

    with _env(DCHUB_INTERNAL_KEY=_KEY, DCHUB_ADMIN_KEY=_ADMIN):
        rv = _run(seg, "indexnow_submit",
                  {"_key": lambda: "k", "json": None, "urllib": None},
                  _Exploding(method="POST"))
    assert _rejected(rv), "unauthenticated POST reached the request body"


def test_indexnow_blueprints_agree_on_one_key():
    """Both IndexNow blueprints must resolve the SAME key. routes/indexnow.py
    owns it; indexnow_route.py used to carry its own hardcoded default, so with
    DCHUB_INDEXNOW_KEY unset (as in production) they disagreed — one submitted
    under 97b69fe3…, the other served /a9d2f7….txt."""
    src = (_ROOT / "routes/indexnow_route.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports_canonical = any(
        isinstance(n, ast.ImportFrom) and n.module == "routes.indexnow"
        and any(a.name == "KEY" for a in n.names)
        for n in ast.walk(tree))
    assert imports_canonical, \
        "indexnow_route.py must import KEY from routes.indexnow, not redefine it"
    # and no independent key literal may creep back in
    hexish = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)
              and len(n.value) >= 32
              and all(c in "0123456789abcdefABCDEF" for c in n.value)]
    assert not hexish, f"second IndexNow key literal reintroduced: {hexish}"
