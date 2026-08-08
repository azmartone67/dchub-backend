#!/usr/bin/env python3
"""
Unit tests for resolve_tier()'s api_keys cross-table fallback (2026-06-12).

Regression guard for the tier-table gap: a web-signup founding/pro/enterprise
customer's DASHBOARD key (dchub_…) lives in api_keys + users.plan with NO
mcp_dev_keys row. Before the fix, resolve_tier() only checked mcp_dev_keys and
fell through to ANONYMOUS — soft-gating a PAYING customer on every soft_gate
route (e.g. get_tax_incentives returned _detail_gated to an enterprise key).

Pure unit test: monkeypatches tier_gate._conn (no DB) and passes a fake request
(resolve_tier accepts req=) so it needs no Flask app context. Runs standalone
(`python3 test_resolve_tier_apikey_fallback.py`) AND under pytest.
"""
import util.tier_gate as tg
from util.tier_gate import resolve_tier, Tier


# ── fakes ────────────────────────────────────────────────────────────────────
class _Dict:
    def __init__(self, d): self._d = d
    def get(self, k, default=None): return self._d.get(k, default)

class FakeReq:
    def __init__(self, headers=None, args=None):
        self.headers = _Dict(headers or {})
        self.args = _Dict(args or {})

class _FakeCursor:
    def __init__(self, router): self._router = router; self._last = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self._last = (sql, params)
    def fetchone(self): return self._router(self._last[0], self._last[1])

class _FakeConn:
    def __init__(self, router): self._router = router
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return _FakeCursor(self._router)

def _conn_factory(router):
    return lambda: _FakeConn(router)


def _with_conn(router, fn):
    """Swap tg._conn for the test body, always restore."""
    orig = tg._conn
    tg._conn = _conn_factory(router)
    try:
        return fn()
    finally:
        tg._conn = orig


# ── tests ────────────────────────────────────────────────────────────────────
def test_api_keys_fallback_promotes_enterprise():
    """mcp_dev_keys MISS + api_keys enterprise HIT → ENTERPRISE (the bug fix)."""
    def router(sql, params):
        if "mcp_dev_keys" in sql:
            return None
        if "api_keys" in sql:
            # rate_limit_tier, ak.plan, u.plan, u.email, ak.user_id
            return ("enterprise", None, None, "ent@example.com", "u-1")
        return None
    tier, ctx = _with_conn(router, lambda: resolve_tier(
        FakeReq(headers={"X-API-Key": "dchub_live_08f4f_demo"})))
    assert tier == Tier.ENTERPRISE, tier
    assert ctx["source"] == "api_keys_no_mcp_row", ctx
    assert ctx["email"] == "ent@example.com"


def test_api_keys_fallback_takes_highest_plan():
    """Highest across rate_limit_tier / ak.plan / u.plan wins (users.plan=pro)."""
    def router(sql, params):
        if "mcp_dev_keys" in sql:
            return None
        if "api_keys" in sql:
            return ("free", "starter", "founding", "f@example.com", "u-2")  # founding→PRO
        return None
    tier, ctx = _with_conn(router, lambda: resolve_tier(
        FakeReq(headers={"X-API-Key": "dchub_live_x"})))
    assert tier == Tier.PRO, tier
    assert ctx["source"] == "api_keys_no_mcp_row"


def test_api_keys_fallback_matches_raw_stored_key_hash():
    """Partner/admin keys store key_hash = RAW api_key (not sha256). The query
    must pass BOTH the hash and the raw key so the dual-match (key_hash IN
    (%s,%s)) finds the owner's own enterprise key. Router returns the row ONLY
    when the raw key is in params — proving the raw value is passed."""
    raw = "dchub_live_08f4fb4_owner"  # secretscan:allow — synthetic fixture
    def router(sql, params):
        if "mcp_dev_keys" in sql:
            return None
        if "api_keys" in sql:
            assert raw in (params or ()), f"raw key not passed to api_keys query: {params}"
            return ("free", None, "enterprise", "azmartone@example.com", "admin001")
        return None
    tier, ctx = _with_conn(router, lambda: resolve_tier(
        FakeReq(headers={"X-API-Key": raw})))
    assert tier == Tier.ENTERPRISE, tier
    assert ctx["source"] == "api_keys_no_mcp_row"


def test_mcp_dev_keys_still_wins_first():
    """An mcp_dev_keys hit returns immediately; the api_keys fallback never runs."""
    def router(sql, params):
        if "mcp_dev_keys" in sql:
            return ("pro", "p@example.com", "u-3")  # tier, email, user_id
        raise AssertionError("api_keys fallback must NOT run when mcp_dev_keys hits")
    tier, ctx = _with_conn(router, lambda: resolve_tier(
        FakeReq(headers={"X-API-Key": "k"})))
    assert tier == Tier.PRO, tier
    assert ctx["source"] == "api_key"


def test_free_api_key_not_promoted():
    """api_keys row exists but every plan is free → stays ANONYMOUS (no false grant)."""
    def router(sql, params):
        if "mcp_dev_keys" in sql:
            return None
        if "api_keys" in sql:
            return ("free", "free", "free", "x@example.com", "u-4")
        return None
    tier, ctx = _with_conn(router, lambda: resolve_tier(
        FakeReq(headers={"X-API-Key": "dchub_live_free"})))
    assert tier == Tier.ANONYMOUS, tier


def test_unknown_key_is_anonymous():
    """No row anywhere → ANONYMOUS."""
    tier, ctx = _with_conn(lambda sql, params: None, lambda: resolve_tier(
        FakeReq(headers={"X-API-Key": "dchub_live_nope"})))
    assert tier == Tier.ANONYMOUS, tier


def test_no_key_is_anonymous():
    """No api_key header at all → ANONYMOUS without touching the DB."""
    def router(sql, params):
        raise AssertionError("DB must not be queried when there is no api_key")
    tier, ctx = _with_conn(router, lambda: resolve_tier(FakeReq()))
    assert tier == Tier.ANONYMOUS, tier


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t(); passed += 1; print(f"  ✅ {t.__name__}")
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} passed")
    raise SystemExit(0 if passed == len(tests) else 1)
