"""A paying self-serve key keeps its plan on every tease helper, in every shape.

NO NETWORK. The database is faked at psycopg2.connect: the fake answers only the
two lookups it recognises by their SQL (mcp_dev_keys by api_key, users by id or
email), so the REAL api_tier_gating.validate_api_key (its dch_live_ tier map),
get_user_plan, require_plan and get_request_principal run as written. The JWT
decoder is a dict (main.py, which owns the real one, cannot be imported here).

Measured on origin/main 4d4a47682 (2026-09-22), with this matrix. The side-door
teases (be#5173/5174/5177/5179/5191) serve the preview to any caller below
Developer. A paid dch_live_ key sent as X-API-Key or ?api_key= resolved to its
real plan on all four helpers. Three shapes did not, each a paying caller served
the preview on routes that answered everyone in full the day before:

  Authorization: Bearer dch_live_<paid>   TEASE on all four: only dchub_ Bearer
                                           tokens were read as keys
  paid key + free-plan login cookie        TEASE on numeric_tease, plan_tease:
                                           require_plan stops at the cookie
  Pro login cookie + free dch_live_ key    TEASE on paid_numeric_gate,
                                           rest_tease: the principal takes the key
                                           (the map mints one per visitor)

The fix gives a caller with several verified credentials the highest plan among
them (api_tier_gating.request_plan_ceiling) and reads a Bearer dch_live_/
dch_trial_ as a key. The free and keyless rows are the controls: nothing here
may open for a caller who has paid for nothing.
"""
import pathlib
import sys

import pytest

flask = pytest.importorskip("flask")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEV = "dch_live_" + "d" * 32
PRO_PAID = "dch_live_" + "p" * 32      # the Stripe webhook writes tier 'paid'
PRO = "dch_live_" + "q" * 32
FREE = "dch_live_" + "f" * 32

MCP_KEYS = {  # api_key -> (email, mcp_dev_keys.tier, status)
    DEV: ("dev@example.com", "developer", "active"),
    PRO_PAID: ("paid@example.com", "paid", "active"),
    PRO: ("pro@example.com", "pro", "active"),
    FREE: (None, "free", "active"),
}
USERS = {  # users.email -> (plan, subscription_status, role, demoted_at)
    "free@example.com": ("free", "", "", None),
    "webpro@example.com": ("pro", "active", "", None),
}
JWTS = {  # token -> payload
    "jwt-free-user": {"user_id": "free@example.com", "email": "free@example.com"},
    "jwt-pro-user": {"user_id": "webpro@example.com", "email": "webpro@example.com"},
}
FULL_MW = 123.4


class _Cur:
    def __init__(self):
        self.row = None
        self.rowcount = 0

    def execute(self, sql, params=()):
        s = " ".join(str(sql).split())
        self.row = None
        if "FROM mcp_dev_keys WHERE api_key = %s" in s and params:
            self.row = MCP_KEYS.get(params[0])
        elif "FROM users WHERE" in s and params:
            self.row = USERS.get(str(params[0]))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    autocommit = True

    def cursor(self, *a, **k):
        return _Cur()

    def close(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def world(monkeypatch):
    import psycopg2
    import api_tier_gating as atg
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://fake/fake")
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "paid-live-keys-test-internal")
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn())
    monkeypatch.setattr(atg, "_decode_jwt_fn", JWTS.get)


def _views():
    from flask import jsonify
    from util import numeric_tease, paid_numeric_gate, plan_tease, rest_tease

    def full():
        return jsonify({"rows": [{"name": "X", "capacity_mw": FULL_MW}]})

    def tease_body():
        return jsonify({"rows": [{"name": "X", "capacity_mw": None}], "_gated": True})

    def _mask(payload):
        for r in payload.get("rows", []):
            r["capacity_mw"] = None
        return payload, 1

    @paid_numeric_gate.tease_numerics(_mask, locked=["capacity_mw"])
    def v_paid_numeric_gate():
        return full()

    return {
        "paid_numeric_gate": v_paid_numeric_gate,
        "rest_tease": lambda: rest_tease.serve("developer", full, tease_body),
        "numeric_tease": lambda: numeric_tease.serve_full_or_tease(full, tease_body),
        "plan_tease": lambda: plan_tease.gate_or_tease(
            "developer", full,
            lambda: ({"rows": [{"name": "X", "capacity_mw": None}]}, ["capacity_mw"], 1)),
    }


def _answer(app, view, headers, cookie, query):
    path = "/r" + ("?api_key=" + query if query else "")
    env = {"HTTP_COOKIE": "dchub_token=" + cookie} if cookie else {}
    with app.test_request_context(path, headers=headers, environ_base=env):
        resp = app.make_response(view())
    body = resp.get_json(silent=True) or {}
    rows = body.get("rows") or []
    if resp.status_code == 200 and rows and rows[0].get("capacity_mw") == FULL_MW:
        return "FULL"
    if resp.status_code == 200 and rows and rows[0].get("capacity_mw") is None:
        return "TEASE"
    return "%s %s" % (resp.status_code, body.get("error"))


# (id, headers, login cookie, ?api_key=, expected)
SHAPES = [
    ("developer-key", {"X-API-Key": DEV}, None, None, "FULL"),
    ("pro-key-tier-paid", {"X-API-Key": PRO_PAID}, None, None, "FULL"),
    ("pro-key", {"X-API-Key": PRO}, None, None, "FULL"),
    ("free-key", {"X-API-Key": FREE}, None, None, "TEASE"),
    ("keyless", {}, None, None, "TEASE"),
    ("developer-key-in-query", {}, None, DEV, "FULL"),
    ("developer-key-as-bearer", {"Authorization": "Bearer " + DEV}, None, None, "FULL"),
    ("pro-key-as-bearer", {"Authorization": "Bearer " + PRO_PAID}, None, None, "FULL"),
    ("free-key-as-bearer", {"Authorization": "Bearer " + FREE}, None, None, "TEASE"),
    ("developer-key-beside-a-free-cookie", {"X-API-Key": DEV}, "jwt-free-user", None, "FULL"),
    ("pro-cookie-beside-a-free-key", {"X-API-Key": FREE}, "jwt-pro-user", None, "FULL"),
    ("pro-cookie-alone", {}, "jwt-pro-user", None, "FULL"),
    ("free-cookie-beside-a-free-key", {"X-API-Key": FREE}, "jwt-free-user", None, "TEASE"),
]
HELPERS = ["paid_numeric_gate", "rest_tease", "numeric_tease", "plan_tease"]


@pytest.mark.parametrize("helper", HELPERS)
@pytest.mark.parametrize("case", SHAPES, ids=[s[0] for s in SHAPES])
def test_every_helper_gives_the_caller_the_plan_it_paid_for(helper, case):
    _id, headers, cookie, query, expected = case
    app = flask.Flask("paid-live-keys")
    got = _answer(app, _views()[helper], headers, cookie, query)
    assert got == expected, "%s / %s: %s, expected %s" % (helper, _id, got, expected)


def test_the_ceiling_only_ever_raises():
    """Two credentials, either order: the higher plan, never the lower."""
    import api_tier_gating as atg
    app = flask.Flask("ceiling")
    for headers, cookie, want in (({"X-API-Key": FREE}, "jwt-pro-user", "pro"),
                                  ({"X-API-Key": DEV}, "jwt-free-user", "developer"),
                                  ({"X-API-Key": FREE}, "jwt-free-user", "free"),
                                  ({}, None, "anon")):
        env = {"HTTP_COOKIE": "dchub_token=" + cookie} if cookie else {}
        with app.test_request_context("/r", headers=headers, environ_base=env):
            assert atg.request_plan_ceiling() == want, (headers, cookie)


def test_the_principal_contract_is_unchanged():
    """The ceiling is the entitlement only. WHO is asking still follows
    get_request_principal's pinned order (tests/test_request_principal_parity)."""
    import api_tier_gating as atg
    app = flask.Flask("principal")
    env = {"HTTP_COOKIE": "dchub_token=jwt-pro-user"}
    with app.test_request_context("/r", headers={"X-API-Key": FREE}, environ_base=env):
        assert atg.get_request_principal()["tier"] == "free"
        assert atg.request_plan_ceiling() == "pro"
