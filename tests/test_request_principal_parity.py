#!/usr/bin/env python3
"""get_request_principal() is get_request_tier()'s parse; the tier must not move.

NO NETWORK, NO DB. The credential lookups (JWT decode, users.plan, api_keys,
mcp_dev_keys, the internal key) are stubbed at the module boundary and the real
functions run inside a real Flask request context.

WHY THIS EXISTS (2026-09-21). The exact-location meter needs to know WHO is
asking — an email or a key — and get_request_tier() knew only a tier string.
Its parse was moved into get_request_principal() and get_request_tier() became
a projection of it, so the tier and the identity a meter charges can never be
resolved by two different copies of the rule. A refactor like that must not
change what ~40 call sites of get_request_tier() receive for ANY credential.

★ The expected tiers in CASES were produced by running the PRE-refactor
get_request_tier() (origin/main 32fab2483) against these same stubs, before a
line of it moved — not by reading the new code. Several are deliberately odd
and are pinned BECAUSE they are odd:

    * a key whose row has plan NULL resolves to None, not 'free'
      (`info.get('plan', 'free')`); callers turn that into 'anon';
    * ANY raise inside the parse — a validate error, a non-dict JWT payload, a
      plan lookup error — returns 'anon' even when a later credential in the
      chain was valid, because the whole parse sits in one try;
    * an internal key outranks every other credential.
"""
import ast
import hashlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import flask  # noqa: E402

import api_data_protection  # noqa: E402
import api_tier_gating as atg  # noqa: E402

INTERNAL = 'internal-key-for-tests'

KEYS = {
    'dchub_dev': {'user_id': 1, 'email': 'Dev@Example.COM', 'plan': 'developer', 'role': 'user'},
    'dchub_admin': {'user_id': 2, 'email': 'ops@example.com', 'plan': 'free', 'role': 'admin'},
    'dchub_nullplan': {'user_id': 3, 'email': 'np@example.com', 'plan': None, 'role': 'user'},
    'dchub_noplan': {'user_id': 4, 'email': None, 'role': 'user'},
    'dch_trial_abc': {'user_id': 'dch_trial_abc', 'email': None, 'plan': 'identified', 'role': 'trial'},
    'dch_live_free': {'user_id': 'dch_live_free', 'email': ' Mixed@Case.Example ',
                      'plan': 'free', 'role': 'mcp'},
}
MCP_ONLY_KEYS = {'dch_live_paid': 'developer'}      # validate misses, mapper hits
JWTS = {
    'jwt-pro': {'user_id': 10, 'email': 'Pro@Example.com'},
    'jwt-freeuser': {'user_id': 11, 'email': 'free@example.com'},
    'jwt-nomail': {'sub': 'u9'},
    'jwt-str': 'not-a-dict',
    'jwt-planraise': {'user_id': 99, 'email': 'boom@example.com'},
}
PLANS = {10: 'pro', 11: None, 'u9': 'starter'}


def _validate(key):
    if key == 'dchub_raises':
        raise RuntimeError('validate blew up')
    return dict(KEYS[key]) if key in KEYS else None


def _resolve_key_tier(key):
    if key == 'dch_live_rkt_raises':
        raise RuntimeError('mapper blew up')
    return MCP_ONLY_KEYS.get(key)


def _user_plan(user_id=None, email=None):
    if user_id == 99:
        raise RuntimeError('users lookup blew up')
    return PLANS.get(user_id)


@pytest.fixture
def stubs(monkeypatch):
    monkeypatch.setattr(atg, 'is_valid_internal_key', lambda k: k == INTERNAL)
    monkeypatch.setattr(atg, '_get_decode_jwt', lambda: JWTS.get)
    monkeypatch.setattr(atg, 'get_user_plan', _user_plan)
    monkeypatch.setattr(atg, 'validate_api_key', _validate)
    monkeypatch.setattr(api_data_protection, '_resolve_key_tier', _resolve_key_tier)
    return monkeypatch


_APP = flask.Flask(__name__)


def _ctx(headers=None, cookies=None, args=None):
    h = dict(headers or {})
    if cookies:
        h['Cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
    return _APP.test_request_context('/probe', headers=h, query_string=args or {})


# (id, headers, cookies, query args, expected tier)
CASES = [
    ('no-credential', {}, None, None, 'anon'),
    ('internal-key', {'X-Internal-Key': INTERNAL}, None, None, 'admin'),
    ('wrong-internal-key', {'X-Internal-Key': 'nope'}, None, None, 'anon'),
    ('internal-outranks-api-key', {'X-Internal-Key': INTERNAL, 'X-API-Key': 'dchub_dev'},
     None, None, 'admin'),
    ('bearer-jwt', {'Authorization': 'Bearer jwt-pro'}, None, None, 'pro'),
    ('bearer-jwt-plan-none-is-free', {'Authorization': 'Bearer jwt-freeuser'}, None, None, 'free'),
    ('bearer-jwt-sub-only', {'Authorization': 'Bearer jwt-nomail'}, None, None, 'starter'),
    ('bearer-jwt-non-dict-payload-is-anon',
     {'Authorization': 'Bearer jwt-str', 'X-API-Key': 'dchub_dev'}, None, None, 'anon'),
    ('bearer-jwt-plan-lookup-raises-is-anon',
     {'Authorization': 'Bearer jwt-planraise'}, None, None, 'anon'),
    ('bearer-garbage-no-key', {'Authorization': 'Bearer garbage'}, None, None, 'anon'),
    ('bearer-garbage-then-api-key',
     {'Authorization': 'Bearer garbage', 'X-API-Key': 'dchub_dev'}, None, None, 'developer'),
    ('x-api-key', {'X-API-Key': 'dchub_dev'}, None, None, 'developer'),
    ('query-api-key', {}, None, {'api_key': 'dchub_dev'}, 'developer'),
    ('empty-header-falls-to-query', {'X-API-Key': ''}, None, {'api_key': 'dchub_dev'}, 'developer'),
    ('bearer-dchub-key', {'Authorization': 'Bearer dchub_dev'}, None, None, 'developer'),
    ('api-key-admin-role', {'X-API-Key': 'dchub_admin'}, None, None, 'admin'),
    ('api-key-null-plan-is-None', {'X-API-Key': 'dchub_nullplan'}, None, None, None),
    ('api-key-missing-plan-is-free', {'X-API-Key': 'dchub_noplan'}, None, None, 'free'),
    ('trial-key', {'X-API-Key': 'dch_trial_abc'}, None, None, 'identified'),
    ('mcp-dev-key-free', {'X-API-Key': 'dch_live_free'}, None, None, 'free'),
    ('mcp-dev-key-via-mapper', {'X-API-Key': 'dch_live_paid'}, None, None, 'developer'),
    ('unknown-key-then-cookie', {'X-API-Key': 'dchub_unknown'},
     {'dchub_token': 'jwt-pro'}, None, 'pro'),
    ('unknown-key-no-cookie', {'X-API-Key': 'dchub_unknown'}, None, None, 'anon'),
    ('validate-raises-is-anon-despite-cookie', {'X-API-Key': 'dchub_raises'},
     {'dchub_token': 'jwt-pro'}, None, 'anon'),
    ('mapper-raises-falls-to-cookie', {'X-API-Key': 'dch_live_rkt_raises'},
     {'dchub_token': 'jwt-pro'}, None, 'pro'),
    ('cookie-session-token', {}, {'session_token': 'jwt-pro'}, None, 'pro'),
    ('cookie-dchub-token-plan-none', {}, {'dchub_token': 'jwt-freeuser'}, None, 'free'),
    ('cookie-token', {}, {'token': 'jwt-pro'}, None, 'pro'),
    ('attestation-cookie-only', {}, {'dchub_browser': '1788000000|72.208|aabb'}, None, 'anon'),
    ('attestation-then-real-jwt', {},
     {'dchub_session': '1788000000|72.208|aabb', 'dchub_token': 'jwt-pro'}, None, 'pro'),
    ('bearer-jwt-beats-cookie', {'Authorization': 'Bearer jwt-freeuser'},
     {'dchub_token': 'jwt-pro'}, None, 'free'),
]


@pytest.mark.parametrize('case_id,headers,cookies,args,expected', CASES,
                         ids=[c[0] for c in CASES])
def test_get_request_tier_is_unchanged_for_every_credential(
        stubs, case_id, headers, cookies, args, expected):
    with _ctx(headers, cookies, args):
        assert atg.get_request_tier() == expected
        assert atg.get_request_principal()['tier'] == expected


def test_no_decoder_means_no_jwt_but_keys_still_resolve(stubs):
    stubs.setattr(atg, '_get_decode_jwt', lambda: None)
    with _ctx({'Authorization': 'Bearer jwt-pro'}):
        assert atg.get_request_tier() == 'anon'
    with _ctx({'Authorization': 'Bearer jwt-pro', 'X-API-Key': 'dchub_dev'}):
        assert atg.get_request_tier() == 'developer'


def test_outside_a_request_is_anonymous(stubs):
    assert atg.get_request_tier() == 'anon'
    p = atg.get_request_principal()
    assert p['tier'] == 'anon' and p['email'] is None and p['api_key_fingerprint'] is None


# ── the identity half ───────────────────────────────────────────────────────

def _fp(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def test_a_jwt_principal_carries_the_lowercased_email_and_no_key(stubs):
    for headers, cookies, via in (({'Authorization': 'Bearer jwt-pro'}, None, 'jwt'),
                                  ({}, {'dchub_token': 'jwt-pro'}, 'cookie')):
        with _ctx(headers, cookies):
            p = atg.get_request_principal()
        assert p['email'] == 'pro@example.com'
        assert p['api_key_fingerprint'] is None
        assert p['credential'] == via


def test_an_api_key_principal_carries_email_and_fingerprint_never_the_key(stubs):
    with _ctx({'X-API-Key': 'dch_live_free'}):
        p = atg.get_request_principal()
    assert p['email'] == 'mixed@case.example'
    assert p['api_key_fingerprint'] == _fp('dch_live_free')
    assert len(p['api_key_fingerprint']) == 16
    assert p['credential'] == 'api_key'
    assert 'dch_live_free' not in repr(p), "the raw key leaked into the principal"


def test_a_key_with_no_email_is_identified_by_fingerprint_only(stubs):
    with _ctx({'X-API-Key': 'dch_trial_abc'}):
        p = atg.get_request_principal()
    assert (p['tier'], p['email'], p['api_key_fingerprint']) == (
        'identified', None, _fp('dch_trial_abc'))


def test_the_mcp_dev_key_mapper_exposes_no_email(stubs):
    with _ctx({'X-API-Key': 'dch_live_paid'}):
        p = atg.get_request_principal()
    assert (p['tier'], p['email'], p['credential']) == ('developer', None, 'mcp_dev_key')
    assert p['api_key_fingerprint'] == _fp('dch_live_paid')


def test_an_internal_key_is_admin_with_no_account(stubs):
    with _ctx({'X-Internal-Key': INTERNAL, 'X-API-Key': 'dchub_dev'}):
        p = atg.get_request_principal()
    assert p == {'tier': 'admin', 'email': None, 'api_key_fingerprint': None,
                 'credential': 'internal_key', 'internal_key': True}


def test_transport_mode_resolves_the_forwarded_key_behind_an_internal_key(stubs):
    """The MCP worker sends X-Internal-Key on every call and the caller's key as
    X-API-Key. A metering surface asks with honor_internal_key=False and must
    get the CALLER, never 'admin'."""
    with _ctx({'X-Internal-Key': INTERNAL, 'X-API-Key': 'dch_live_free'}):
        p = atg.get_request_principal(honor_internal_key=False)
        assert atg.get_request_tier() == 'admin', "the default must not change"
    assert (p['tier'], p['credential'], p['internal_key']) == ('free', 'api_key', True)
    assert p['email'] == 'mixed@case.example'
    with _ctx({'X-Internal-Key': INTERNAL}):
        p = atg.get_request_principal(honor_internal_key=False)
    assert (p['tier'], p['credential'], p['internal_key']) == ('anon', None, True)


def test_a_raise_mid_parse_leaves_no_half_resolved_identity(stubs):
    with _ctx({'X-API-Key': 'dchub_raises'}, {'dchub_token': 'jwt-pro'}):
        p = atg.get_request_principal()
    assert p == {'tier': 'anon', 'email': None, 'api_key_fingerprint': None,
                 'credential': None, 'internal_key': False}


def test_a_malformed_email_claim_costs_the_account_not_the_tier(stubs):
    stubs.setitem(JWTS, 'jwt-intmail', {'user_id': 10, 'email': 12345})
    with _ctx({'Authorization': 'Bearer jwt-intmail'}):
        p = atg.get_request_principal()
        assert atg.get_request_tier() == 'pro'
    assert p['email'] is None and p['tier'] == 'pro'


def test_request_api_key_is_the_one_extraction_rule(stubs):
    with _ctx({'X-API-Key': 'k1'}, args={'api_key': 'k2'}):
        assert atg.request_api_key() == 'k1'
    with _ctx({}, args={'api_key': 'k2'}):
        assert atg.request_api_key() == 'k2'
    with _ctx({'Authorization': 'Bearer dchub_k3 '}):
        assert atg.request_api_key() == 'dchub_k3'
    with _ctx({'Authorization': 'Bearer eyJhbGciOi.jwt.sig'}):
        assert atg.request_api_key() is None


# ── one copy of the rule ────────────────────────────────────────────────────

def _func(name):
    tree = ast.parse((ROOT / 'api_tier_gating.py').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f'{name} not found in api_tier_gating.py')


def test_get_request_tier_parses_nothing_itself():
    """A second parse in get_request_tier() is how the tier and the identity a
    meter charges would start to disagree. It may only call the principal."""
    node = _func('get_request_tier')
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    calls = {n.func.id for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert calls == {'get_request_principal'}, calls
    assert not ({'request', 'validate_api_key', 'get_user_plan', '_get_decode_jwt',
                 'is_valid_internal_key'} & (names | attrs)), names | attrs


def test_the_principal_reads_the_key_through_the_one_extraction_rule():
    node = _func('get_request_principal')
    calls = {n.func.id for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert 'request_api_key' in calls
    literals = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant)}
    assert 'X-API-Key' not in literals, (
        "get_request_principal reads X-API-Key itself — a second extraction rule")
