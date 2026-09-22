"""Keyless traffic from a partner's DECLARED egress gets its own rate tier.

WHY (2026-09-21). AnythingMCP generates a DC Hub connector from /openapi.json.
12 of its 16 tools are called without a key, and every customer's call leaves
the platform through ONE declared address (partner_egress.py). Both
before_request limiters bucketed keyless /api/ traffic per source address:

    rate_limiter.rate_limit_before     'ip:<addr>'   LIMITS['anonymous']
    main.enforce_tier_rate_limits      'ip_<md5>'    _tier_rate_limits['free']

so every keyless customer of the partner shared one anonymous allowance, which
undercut the "works before signup" property the listing is featured for. Both
limiters now give the declared address the 'partner' tier.

These tests drive the two REAL before_request functions, not the helper:
tests/test_rate_limit_origin_host.py records how a helper test stayed green
while its call site was wrong. rate_limiter is a leaf module and is imported.
main.py cannot be imported here (it boots the whole app), so its limiter is
pulled out of the source with ast and executed: the shipped code, not a copy.

What must NOT move, and is pinned here:
  * an undeclared address: anonymous / free, per IP, exactly as before;
  * a keyed caller from the declared address: its own per-key bucket;
  * a moved egress: the env override moves the lift, and the old address
    falls back to anonymous (fail closed).

Limits are read from the two tables, never typed here, so re-tuning a number is
a one-line change in the limiter and not in this file.
"""
import ast
import builtins
import copy
import hashlib
import hmac
import json
import os
import pathlib
import sys
import threading
import time
import types
from collections import defaultdict

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import partner_egress  # noqa: E402
import rate_limiter  # noqa: E402
from internal_auth import is_valid_internal_key  # noqa: E402

API_PATH = "/api/v1/facilities/search"   # limited by both, not a bypass path
UNDECLARED_IP = "203.0.113.7"            # TEST-NET-3: not loopback, not Railway
NEIGHBOUR_IP = "104.248.242.236"         # one address past the declared /32
MOVED_IP = "198.51.100.23"               # TEST-NET-2: a host move via env
PROXY_ADDR = "100.64.0.9"                # remote_addr as Railway's proxy shows it
UA = "node"                              # matches no bypass or crawler marker

PARTNER, PARTNER_IP = "anythingmcp/", "104.248.242.235"

DCHUB_KEY = "dchub_" + "k" * 30          # resolved by main.py via api_keys
LIVE_KEY = "dch_live_" + "a" * 32        # resolved by main.py via validate
LIVE_KEY_2 = "dch_live_" + "b" * 32

_APP = flask.Flask(__name__)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No override leaks in from the shell, and no bucket survives a test."""
    monkeypatch.delenv("DCHUB_PARTNER_EGRESS", raising=False)
    rate_limiter._buckets.clear()
    rate_limiter._log_budget.clear()
    yield
    rate_limiter._buckets.clear()
    rate_limiter._log_budget.clear()


@pytest.fixture
def clock(monkeypatch):
    """A controllable clock for BOTH limiters, so hourly limits can be reached
    without waiting an hour. Starts at real time so module state set by an
    earlier test (rate_limiter._last_cleanup) reads as the past."""
    now = [time.time()]
    fake = types.SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(rate_limiter, "time", fake)
    return now


def test_the_default_registry_declares_anythingmcp():
    """The declaration this change exists for (Matteo Morelli, 2026-09-21)."""
    assert partner_egress.PARTNER_EGRESS_DEFAULT[PARTNER] == (PARTNER_IP,)
    assert partner_egress.partner_for_ip(PARTNER_IP) == PARTNER


def test_partner_limits_exceed_anonymous_in_both_limiters():
    """The fix is that the partner gets MORE than one visitor would. A partner
    row at or below anonymous would re-create the shared-bucket collapse."""
    main = _main_limiter()
    p, a = rate_limiter.LIMITS["partner"], rate_limiter.LIMITS["anonymous"]
    assert p["rpm"] > a["rpm"] and p["rph"] > a["rph"]
    mp, mf = main["_tier_rate_limits"]["partner"], main["_tier_rate_limits"]["free"]
    assert mp["per_minute"] > mf["per_minute"] and mp["per_hour"] > mf["per_hour"]


# ════════════════════════════════════════════════════════════════════════════
# 1. rate_limiter.rate_limit_before — the token-bucket limiter
# ════════════════════════════════════════════════════════════════════════════

def _rl(ip, headers=None, xff=None):
    """One real rate_limit_before() call.

    Returns (status, limit): status is 429 or None (allowed), limit is the
    X-RateLimit-Limit the request was held to (g._rl_limit), None if bypassed.
    """
    h = {"User-Agent": UA}
    if ip is not None:
        h["CF-Connecting-IP"] = ip
    if xff:
        h["X-Forwarded-For"] = xff
    h.update(headers or {})
    with _APP.test_request_context(API_PATH, headers=h,
                                   environ_base={"REMOTE_ADDR": PROXY_ADDR}):
        resp = rate_limiter.rate_limit_before()
        return (resp.status_code if resp is not None else None,
                getattr(flask.g, "_rl_limit", None))


def _rl_minute(ip, headers=None):
    """Requests allowed in one minute before the first 429, plus the limit the
    first request was held to. Bounded so a missing limit cannot hang."""
    first_limit = None
    for n in range(1, 2000):
        status, limit = _rl(ip, headers)
        if first_limit is None:
            first_limit = limit
        if status == 429:
            return n - 1, first_limit
    raise AssertionError(f"{ip}: no 429 within 2000 requests")


def test_rl_control_an_undeclared_address_is_limited_as_anonymous():
    """Guard-the-guard, and the 'exactly as before' half: without the partner
    branch every address gets this. If this path were bypassed, or the test IP
    were treated as internal, every partner assertion would pass vacuously."""
    rpm = rate_limiter.LIMITS["anonymous"]["rpm"]
    assert _rl_minute(UNDECLARED_IP) == (rpm, rpm)


def test_rl_declared_egress_gets_the_partner_tier_not_the_anonymous_bucket():
    """THE FIX. And a tier, not a bypass: the partner still 429s at its row."""
    rpm = rate_limiter.LIMITS["partner"]["rpm"]
    assert _rl_minute(PARTNER_IP) == (rpm, rpm)


@pytest.mark.parametrize("ip", [NEIGHBOUR_IP, PARTNER_IP + "0", "104.248.242",
                                "::ffff:" + PARTNER_IP],
                         ids=["neighbour", "longer", "prefix", "v4-mapped"])
def test_rl_an_address_that_is_not_exactly_declared_stays_anonymous(ip):
    """Fail closed: a match is the exact declared string, never a range."""
    rpm = rate_limiter.LIMITS["anonymous"]["rpm"]
    assert _rl_minute(ip) == (rpm, rpm)


def test_rl_partner_bucket_is_not_the_anonymous_bucket_of_the_same_address():
    """Anonymous traffic elsewhere does not drain the partner, and the partner
    is not drained into anonymous: two separate buckets."""
    arpm = rate_limiter.LIMITS["anonymous"]["rpm"]
    assert _rl_minute(UNDECLARED_IP)[0] == arpm
    assert _rl(PARTNER_IP)[0] is None


def test_rl_hourly_axis_is_the_partner_row(clock):
    """Past the anonymous hourly cap, and 429 exactly at the partner's."""
    lim = rate_limiter.LIMITS["partner"]
    anon = rate_limiter.LIMITS["anonymous"]
    batch = lim["rpm"] - 1
    sent = 0
    while sent < lim["rph"]:
        for _ in range(min(batch, lim["rph"] - sent)):
            status, _ = _rl(PARTNER_IP)
            assert status is None, f"429 after {sent} requests (anonymous rph={anon['rph']})"
            sent += 1
        clock[0] += 61          # the minute bucket refills; the hour bucket does not
    assert sent > anon["rph"]
    assert _rl(PARTNER_IP)[0] == 429


def test_rl_hourly_axis_is_unchanged_for_an_undeclared_address(clock):
    anon = rate_limiter.LIMITS["anonymous"]
    sent = 0
    while sent < anon["rph"]:
        for _ in range(min(anon["rpm"] - 1, anon["rph"] - sent)):
            assert _rl(UNDECLARED_IP)[0] is None
            sent += 1
        clock[0] += 61
    assert _rl(UNDECLARED_IP)[0] == 429


KEYED = [
    ({"X-API-Key": LIVE_KEY}, {"X-API-Key": LIVE_KEY_2}),
    ({"Authorization": "Bearer " + LIVE_KEY}, {"Authorization": "Bearer " + LIVE_KEY_2}),
    ({"Authorization": "Bearer " + DCHUB_KEY}, {"X-API-Key": LIVE_KEY_2}),
    ({"X-API-Key": DCHUB_KEY}, {"Authorization": "Bearer " + LIVE_KEY_2}),
]
KEYED_IDS = ["x-api-key-dch", "bearer-dch", "bearer-dchub", "x-api-key-dchub"]


@pytest.mark.parametrize("key_a,key_b", KEYED, ids=KEYED_IDS)
def test_rl_keyed_caller_from_the_declared_egress_keeps_its_own_bucket(key_a, key_b):
    """A keyed call from the partner's address behaves exactly as the same key
    from anywhere else: its own bucket at the authenticated row. It neither
    draws on the partner bucket nor is blocked when that bucket is empty."""
    auth = rate_limiter.LIMITS["authenticated"]["rpm"]
    # the same key from an undeclared address — today's behaviour
    assert _rl_minute(UNDECLARED_IP, key_a) == (auth, auth)
    rate_limiter._buckets.clear()

    prpm = rate_limiter.LIMITS["partner"]["rpm"]
    assert _rl_minute(PARTNER_IP) == (prpm, prpm)          # partner bucket spent
    assert _rl_minute(PARTNER_IP, key_a) == (auth, auth)   # key A: own full bucket
    assert _rl(PARTNER_IP, key_b) == (None, auth)          # key B: not A's bucket


def test_rl_env_override_moves_the_lift_and_the_old_address_falls_back(monkeypatch):
    """A declared host move is a config change: DCHUB_PARTNER_EGRESS."""
    monkeypatch.setenv("DCHUB_PARTNER_EGRESS", json.dumps({PARTNER: [MOVED_IP]}))
    prpm = rate_limiter.LIMITS["partner"]["rpm"]
    arpm = rate_limiter.LIMITS["anonymous"]["rpm"]
    assert _rl_minute(MOVED_IP) == (prpm, prpm)
    assert _rl_minute(PARTNER_IP) == (arpm, arpm)


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"104.248.242.235"'],
                         ids=["garbage", "list", "string"])
def test_rl_malformed_override_does_not_widen_the_registry(monkeypatch, raw):
    monkeypatch.setenv("DCHUB_PARTNER_EGRESS", raw)
    prpm = rate_limiter.LIMITS["partner"]["rpm"]
    arpm = rate_limiter.LIMITS["anonymous"]["rpm"]
    assert _rl_minute(PARTNER_IP) == (prpm, prpm)          # the default stands
    assert _rl_minute(MOVED_IP) == (arpm, arpm)


def test_rl_empty_override_switches_the_lift_off(monkeypatch):
    """'{}' declares no partner: the address is one visitor again."""
    monkeypatch.setenv("DCHUB_PARTNER_EGRESS", "{}")
    arpm = rate_limiter.LIMITS["anonymous"]["rpm"]
    assert _rl_minute(PARTNER_IP) == (arpm, arpm)


def test_rl_the_address_read_is_cf_connecting_ip():
    """No new header is trusted. The lift reads the same address the anonymous
    bucket reads, CF-Connecting-IP first; X-Forwarded-For and remote_addr do not
    override it."""
    arpm = rate_limiter.LIMITS["anonymous"]["rpm"]
    assert _rl(UNDECLARED_IP, xff=PARTNER_IP) == (None, arpm)
    prpm = rate_limiter.LIMITS["partner"]["rpm"]
    assert _rl(PARTNER_IP, xff=UNDECLARED_IP) == (None, prpm)


# ════════════════════════════════════════════════════════════════════════════
# 2. main.enforce_tier_rate_limits — the sliding-window limiter
# ════════════════════════════════════════════════════════════════════════════

_MAIN_NAMES = (
    "_tier_rate_limits", "_RATE_LIMIT_BYPASS_PATHS", "_tier_requests",
    "_tier_rate_lock", "_tier_key_cache", "_TIER_KEY_CACHE_TTL",
    "_TIER_KEY_CACHE_MAX", "_tier_key_cache_get", "_tier_key_cache_put",
    "_get_request_tier", "enforce_tier_rate_limits",
)
_MAIN_NODES = None


def _main_nodes():
    """The limiter's definitions, parsed out of main.py once. Exactly one
    module-level definition per name: a second one would win at import (or, for
    a before_request hook, run as well), and this harness would test the loser."""
    global _MAIN_NODES
    if _MAIN_NODES is None:
        tree = ast.parse((ROOT / "main.py").read_text())
        found = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in _MAIN_NAMES:
                found.setdefault(node.name, []).append(node)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in _MAIN_NAMES:
                        found.setdefault(t.id, []).append(node)
        dupes = {k: len(v) for k, v in found.items() if len(v) != 1}
        missing = set(_MAIN_NAMES) - set(found)
        assert not missing and not dupes, (missing, dupes)
        nodes = []
        for name in _MAIN_NAMES:
            node = copy.deepcopy(found[name][0])
            if isinstance(node, ast.FunctionDef):
                node.decorator_list = []        # @app.before_request needs the app
            nodes.append(node)
        _MAIN_NODES = nodes
    return _MAIN_NODES


def _free_names(nodes):
    loads, bound = set(), set()
    for n in nodes:
        for x in ast.walk(n):
            if isinstance(x, ast.Name):
                (loads if isinstance(x.ctx, ast.Load) else bound).add(x.id)
            elif isinstance(x, ast.FunctionDef):
                bound.add(x.name)
            elif isinstance(x, ast.arg):
                bound.add(x.arg)
            elif isinstance(x, ast.alias):
                bound.add((x.asname or x.name).split(".")[0])
            elif isinstance(x, ast.ExceptHandler) and x.name:
                bound.add(x.name)
    return loads - bound - set(dir(builtins))


class _Keys:
    """Credential lookups main.py's limiter makes, answered from a table and
    driven by the arguments it is handed (a key it does not know resolves to
    nothing, exactly like a real miss)."""

    def __init__(self):
        self.api_keys = {hashlib.sha256(DCHUB_KEY.encode()).hexdigest(): ("pro", 42)}
        self.live = {LIVE_KEY: {"user_id": 7, "plan": "developer"},
                     LIVE_KEY_2: {"user_id": 8, "plan": "developer"}}

    def pg_execute(self, sql, params=None, fetch=False):
        row = self.api_keys.get(params[0]) if params else None
        return None, ([row] if row else [])

    def validate_api_key(self, key):
        return self.live.get(key)


def _main_limiter(keys=None, clock=None):
    """A fresh namespace running main.py's real limiter code. Every free name it
    reads is supplied; the assert fails loudly if the shipped code starts to
    read one more, because several reads sit inside `except Exception: pass`
    where a NameError would silently change the path taken."""
    keys = keys or _Keys()
    ns = {
        "request": flask.request,
        "jsonify": flask.jsonify,
        "os": os,
        "hmac": hmac,
        "hashlib": hashlib,
        "threading": threading,
        "defaultdict": defaultdict,
        "time": types.SimpleNamespace(time=lambda: clock[0]) if clock else time,
        "is_valid_internal_key": is_valid_internal_key,
        "partner_for_ip": partner_egress.partner_for_ip,   # the real one
        "partner_rate_limited": partner_egress.partner_rate_limited,   # the real one
        "get_ai_wars_key_info": lambda: None,             # no AI Wars key
        "_pg_execute": keys.pg_execute,
        "JWT_SECRET": "unused-no-jwt-is-sent",
    }
    nodes = _main_nodes()
    missing = _free_names(nodes) - set(ns)
    assert not missing, f"main.py's limiter now reads {sorted(missing)}; supply it"
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    return ns


@pytest.fixture
def live_keys(monkeypatch):
    """_get_request_tier imports validate_api_key from api_tier_gating at call
    time; answer it from the same table."""
    import api_tier_gating
    keys = _Keys()
    monkeypatch.setattr(api_tier_gating, "validate_api_key", keys.validate_api_key)
    return keys


def _main(ns, ip, headers=None, xff=None):
    """One real enforce_tier_rate_limits() call. Returns None if allowed, else
    the 429 body (it names the tier and the limit that was applied)."""
    h = {"User-Agent": UA}
    if ip is not None:
        h["CF-Connecting-IP"] = ip
    if xff:
        h["X-Forwarded-For"] = xff
    h.update(headers or {})
    with _APP.test_request_context(API_PATH, headers=h,
                                   environ_base={"REMOTE_ADDR": PROXY_ADDR}):
        out = ns["enforce_tier_rate_limits"]()
        if out is None:
            return None
        body, status = out
        assert status == 429, status
        return body.get_json()


def _main_minute(ns, ip, headers=None):
    """(requests allowed before the first 429, tier named, per-minute limit)."""
    for n in range(1, 3000):
        body = _main(ns, ip, headers)
        if body is not None:
            return n - 1, body["tier"], body.get("limit_per_minute")
    raise AssertionError(f"{ip}: no 429 within 3000 requests")


def test_main_control_an_undeclared_address_is_limited_as_free():
    """Guard-the-guard, and 'exactly as before' for an undeclared address."""
    ns = _main_limiter()
    free = ns["_tier_rate_limits"]["free"]["per_minute"]
    assert _main_minute(ns, UNDECLARED_IP) == (free, "free", free)


def test_main_declared_egress_gets_the_partner_tier():
    ns = _main_limiter()
    p = ns["_tier_rate_limits"]["partner"]["per_minute"]
    assert _main_minute(ns, PARTNER_IP) == (p, "partner", p)


@pytest.mark.parametrize("ip", [NEIGHBOUR_IP, PARTNER_IP + "0", "::ffff:" + PARTNER_IP],
                         ids=["neighbour", "longer", "v4-mapped"])
def test_main_an_address_that_is_not_exactly_declared_stays_free(ip):
    ns = _main_limiter()
    free = ns["_tier_rate_limits"]["free"]["per_minute"]
    assert _main_minute(ns, ip) == (free, "free", free)


def _main_hour(ns, now, ip, per_minute, per_hour):
    """Send per_hour requests under the per-minute cap, advancing the clock;
    returns the 429 body for the next one (None if it was allowed)."""
    sent = 0
    while sent < per_hour:
        for _ in range(min(per_minute - 1, per_hour - sent)):
            body = _main(ns, ip)
            assert body is None, f"429 after {sent}: {body}"
            sent += 1
        now[0] += 61
    return _main(ns, ip)


def test_main_hourly_axis_is_the_partner_row():
    now = [time.time()]
    ns = _main_limiter(clock=now)
    lim, free = ns["_tier_rate_limits"]["partner"], ns["_tier_rate_limits"]["free"]
    assert lim["per_hour"] > free["per_hour"]
    body = _main_hour(ns, now, PARTNER_IP, lim["per_minute"], lim["per_hour"])
    assert body is not None and body["tier"] == "partner"
    assert body["limit_per_hour"] == lim["per_hour"]


def test_main_hourly_axis_is_unchanged_for_an_undeclared_address():
    now = [time.time()]
    ns = _main_limiter(clock=now)
    free = ns["_tier_rate_limits"]["free"]
    body = _main_hour(ns, now, UNDECLARED_IP, free["per_minute"], free["per_hour"])
    assert body is not None and body["tier"] == "free"
    assert body["limit_per_hour"] == free["per_hour"]


MAIN_KEYED = [
    ({"X-API-Key": DCHUB_KEY}, "pro"),
    ({"Authorization": "Bearer " + DCHUB_KEY}, "pro"),
    ({"X-API-Key": LIVE_KEY}, "developer"),
    ({"Authorization": "Bearer " + LIVE_KEY}, "developer"),
]


@pytest.mark.parametrize("headers,plan", MAIN_KEYED,
                         ids=["x-api-key-dchub", "bearer-dchub", "x-api-key-live", "bearer-live"])
def test_main_keyed_caller_from_the_declared_egress_keeps_its_own_bucket(
        live_keys, headers, plan):
    """The same key, from an undeclared address and from the partner's, is held
    to its own plan's row in its own bucket, and is not blocked when the partner
    bucket is spent."""
    ns = _main_limiter(live_keys)
    per_min = ns["_tier_rate_limits"][plan]["per_minute"]
    assert _main_minute(ns, UNDECLARED_IP, headers) == (per_min, plan, per_min)

    ns = _main_limiter(live_keys)
    p = ns["_tier_rate_limits"]["partner"]["per_minute"]
    assert _main_minute(ns, PARTNER_IP) == (p, "partner", p)          # spent
    assert _main_minute(ns, PARTNER_IP, headers) == (per_min, plan, per_min)


def test_main_two_keys_from_the_declared_egress_do_not_share(live_keys):
    ns = _main_limiter(live_keys)
    dev = ns["_tier_rate_limits"]["developer"]["per_minute"]
    assert _main_minute(ns, PARTNER_IP, {"X-API-Key": LIVE_KEY})[0] == dev
    assert _main(ns, PARTNER_IP, {"X-API-Key": LIVE_KEY_2}) is None


def test_main_env_override_moves_the_lift_and_the_old_address_falls_back(monkeypatch):
    monkeypatch.setenv("DCHUB_PARTNER_EGRESS", json.dumps({PARTNER: [MOVED_IP]}))
    ns = _main_limiter()
    t = ns["_tier_rate_limits"]
    assert _main_minute(ns, MOVED_IP)[1:] == ("partner", t["partner"]["per_minute"])
    assert _main_minute(ns, PARTNER_IP)[1:] == ("free", t["free"]["per_minute"])


def test_main_the_address_read_is_cf_connecting_ip():
    ns = _main_limiter()
    free = ns["_tier_rate_limits"]["free"]["per_minute"]
    for _ in range(free):
        assert _main(ns, UNDECLARED_IP, xff=PARTNER_IP) is None
    assert _main(ns, UNDECLARED_IP, xff=PARTNER_IP)["tier"] == "free"


# ════════════════════════════════════════════════════════════════════════════
# 3. The 429 body (2026-09-21). The partner bucket is shared by every keyless
#    customer of the partner, so its 429 says so and names the free key, which
#    both limiters bucket per key. No other tier's body moves: the anonymous and
#    keyed 429s are pinned here to what they were before, field for field and
#    byte for byte.
# ════════════════════════════════════════════════════════════════════════════

CLAIM = "https://dchub.cloud/api/v1/keys/claim"
CONNECT = "https://dchub.cloud/connect"
PARTNER_ONLY_FIELDS = {"shared_allowance", "free_key_url", "connect_url"}


def _rl_first_429(ip, headers=None):
    """(body, raw bytes) of the first 429 rate_limit_before returns this caller."""
    h = {"User-Agent": UA, "CF-Connecting-IP": ip}
    h.update(headers or {})
    for _ in range(2000):
        with _APP.test_request_context(API_PATH, headers=h,
                                       environ_base={"REMOTE_ADDR": PROXY_ADDR}):
            resp = rate_limiter.rate_limit_before()
            if resp is not None:
                assert resp.status_code == 429
                return resp.get_json(), resp.get_data()
    raise AssertionError(f"{ip}: no 429 within 2000 requests")


def _rl_body_before(retry_after):
    """rate_limiter's 429 body as origin/main built it before 2026-09-21, for
    every tier alike."""
    from routes.error_envelope import merge_error_mitigation
    body = {
        'error': 'rate_limit_exceeded',
        'message': f'Too many requests. Retry after {retry_after}s.',
        'retry_after': retry_after,
    }
    merge_error_mitigation(
        body, 'rate_limit_exceeded', 'transient_backoff',
        f'Per-window request cap exhausted; wait {retry_after}s '
        '(Retry-After) and retry the same request.')
    return body


def _bytes(body):
    with _APP.app_context():
        return flask.jsonify(body).get_data()


@pytest.mark.parametrize("ip,headers", [
    (UNDECLARED_IP, None),                             # anonymous
    (NEIGHBOUR_IP, None),                              # anonymous, next door
    (PARTNER_IP, {"X-API-Key": LIVE_KEY}),             # keyed, from the egress
    (UNDECLARED_IP, {"Authorization": "Bearer " + DCHUB_KEY}),   # keyed
], ids=["anonymous", "neighbour", "keyed-from-egress", "keyed-bearer"])
def test_rl_every_other_429_body_is_byte_identical_to_before(ip, headers):
    body, raw = _rl_first_429(ip, headers)
    before = _rl_body_before(body["retry_after"])
    assert body == before
    assert raw == _bytes(before)


def test_rl_partner_429_names_the_shared_allowance_and_the_free_key():
    body, _ = _rl_first_429(PARTNER_IP)
    lim = rate_limiter.LIMITS["partner"]
    # everything the 429 said before is still there, unchanged
    before = _rl_body_before(body["retry_after"])
    assert {k: body[k] for k in before} == before
    assert set(body) - set(before) == PARTNER_ONLY_FIELDS
    note = body["shared_allowance"]
    assert f"{lim['rpm']:,} requests per minute, {lim['rph']:,} per hour" in note
    assert "without an API key" in note and "that key's own limit" in note
    assert CLAIM in note and CONNECT in note
    assert body["free_key_url"] == CLAIM and body["connect_url"] == CONNECT


def test_rl_partner_hourly_429_carries_the_note_too(clock):
    lim = rate_limiter.LIMITS["partner"]
    sent = 0
    while sent < lim["rph"]:
        for _ in range(min(lim["rpm"], lim["rph"] - sent)):
            assert _rl(PARTNER_IP)[0] is None, f"429 after {sent}"
            sent += 1
        clock[0] += 61
    body, _ = _rl_first_429(PARTNER_IP)
    assert body["retry_after"] > 60                      # the hourly axis
    assert PARTNER_ONLY_FIELDS <= set(body)


def _main_first_429(ns, ip, headers=None):
    for _ in range(3000):
        body = _main(ns, ip, headers)
        if body is not None:
            return body
    raise AssertionError(f"{ip}: no 429 within 3000 requests")


def _main_body_before(tier, window, limit):
    """main.py's 429 body as it was before 2026-09-21, for every tier alike."""
    if window == "hour":
        return {'success': False, 'error': 'rate_limited',
                'message': f"Hourly limit exceeded ({limit}/hr for {tier} tier). "
                           "Upgrade your plan for higher limits.",
                'tier': tier, 'limit_per_hour': limit, 'retry_after_seconds': 3600,
                'upgrade_url': 'https://dchub.cloud/pricing'}
    return {'success': False, 'error': 'rate_limited',
            'message': f"Rate limit exceeded ({limit}/min for {tier} tier). "
                       "Upgrade your plan for higher limits.",
            'tier': tier, 'limit_per_minute': limit, 'retry_after_seconds': 60,
            'upgrade_url': 'https://dchub.cloud/pricing'}


@pytest.mark.parametrize("ip,headers,tier", [
    (UNDECLARED_IP, None, "free"),
    (NEIGHBOUR_IP, None, "free"),
    (PARTNER_IP, {"X-API-Key": LIVE_KEY}, "developer"),
    (PARTNER_IP, {"X-API-Key": DCHUB_KEY}, "pro"),
], ids=["anonymous", "neighbour", "live-key-from-egress", "dchub-key-from-egress"])
def test_main_every_other_minute_429_body_is_unchanged(live_keys, ip, headers, tier):
    ns = _main_limiter(live_keys)
    limit = ns["_tier_rate_limits"][tier]["per_minute"]
    body = _main_first_429(ns, ip, headers)
    assert body == _main_body_before(tier, "minute", limit)
    assert _bytes(body) == _bytes(_main_body_before(tier, "minute", limit))


def test_main_the_hourly_429_body_is_unchanged_for_an_undeclared_address():
    now = [time.time()]
    ns = _main_limiter(clock=now)
    free = ns["_tier_rate_limits"]["free"]
    body = _main_hour(ns, now, UNDECLARED_IP, free["per_minute"], free["per_hour"])
    assert body == _main_body_before("free", "hour", free["per_hour"])


def _assert_main_partner_body(body, window, t):
    p = t["partner"]
    assert body["tier"] == "partner"
    assert "upgrade_url" not in body
    assert "Upgrade" not in body["message"] and "pricing" not in json.dumps(body)
    limit = p["per_hour"] if window == "hour" else p["per_minute"]
    assert body["message"] == (
        f"Hourly limit exceeded ({limit}/hr for partner tier)." if window == "hour"
        else f"Rate limit exceeded ({limit}/min for partner tier).")
    assert f"{p['per_minute']:,} requests per minute, {p['per_hour']:,} per hour" \
        in body["shared_allowance"]
    assert body["free_key_url"] == CLAIM and body["connect_url"] == CONNECT


def test_main_partner_minute_429_is_the_shared_allowance_not_an_upgrade():
    ns = _main_limiter()
    body = _main_first_429(ns, PARTNER_IP)
    _assert_main_partner_body(body, "minute", ns["_tier_rate_limits"])
    assert body["limit_per_minute"] == ns["_tier_rate_limits"]["partner"]["per_minute"]
    assert body["retry_after_seconds"] == 60


def test_main_partner_hourly_429_is_the_shared_allowance_not_an_upgrade():
    now = [time.time()]
    ns = _main_limiter(clock=now)
    lim = ns["_tier_rate_limits"]["partner"]
    body = _main_hour(ns, now, PARTNER_IP, lim["per_minute"], lim["per_hour"])
    _assert_main_partner_body(body, "hour", ns["_tier_rate_limits"])
    assert body["limit_per_hour"] == lim["per_hour"]
    assert body["retry_after_seconds"] == 3600


def test_the_partner_line_is_relay_safe():
    """The partner relays our error bodies verbatim to its customers' agents:
    the line states facts. No price, and none of the imperatives the paywall
    test forbids (tests/test_paywall_does_not_instruct_the_model.py)."""
    import re
    from tests.test_paywall_does_not_instruct_the_model import _DIRECTIVES
    lim = rate_limiter.LIMITS["partner"]
    text = json.dumps(partner_egress.shared_allowance_429(lim["rpm"], lim["rph"]))
    assert "$" not in text and "/pricing" not in text
    for pattern in _DIRECTIVES + (r"\b(?:tell|ask|show) (?:your|the) (?:user|human)\b",
                                  r"\byou (?:must|should)\b"):
        assert not re.search(pattern, text, re.I), pattern


# The 4xx hint middleware (routes/paywall_hint_middleware.py) appends an
# `_upgrade_hint` to small 401/403/429 JSON bodies. On the partner's 429 that
# would put back what the line above keeps out, so it skips exactly that 429.

@pytest.fixture
def stack(monkeypatch):
    """The real limiter and the real hint middleware on one app. The
    middleware's two database reads are answered locally; its A/B log is
    recorded instead of written."""
    from routes import paywall_hint_middleware as phm
    events = []
    monkeypatch.setattr(phm, "_log_ab_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(phm, "_personal_hit_pitch", lambda *a, **k: "")
    app = flask.Flask("partner-429-stack")
    app.add_url_rule(API_PATH, "search", lambda: "ok")
    app.before_request(rate_limiter.rate_limit_before)
    app.after_request(rate_limiter.rate_limit_after)
    phm.register_paywall_hint_middleware(app)
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = PROXY_ADDR
    return client, events


def _stack_first_429(client, ip, headers=None):
    h = {"User-Agent": UA, "CF-Connecting-IP": ip}
    h.update(headers or {})
    for _ in range(2000):
        r = client.get(API_PATH, headers=h)
        if r.status_code == 429:
            return r.get_json()
    raise AssertionError(f"{ip}: no 429 within 2000 requests")


def test_the_partner_429_goes_out_without_the_hint(stack):
    client, events = stack
    body = _stack_first_429(client, PARTNER_IP)
    assert PARTNER_ONLY_FIELDS <= set(body)
    assert "_upgrade_hint" not in body
    assert events == []                       # no variant shown, none logged


@pytest.mark.parametrize("ip,headers", [
    (UNDECLARED_IP, None), (PARTNER_IP, {"X-API-Key": LIVE_KEY}),
], ids=["anonymous", "keyed-from-egress"])
def test_every_other_429_is_still_enriched_as_before(stack, ip, headers):
    client, events = stack
    body = _stack_first_429(client, ip, headers)
    assert "_upgrade_hint" in body and not PARTNER_ONLY_FIELDS & set(body)
    assert len(events) == 1 and events[0][1] == 429
