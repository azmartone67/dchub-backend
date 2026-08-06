"""monthly_quota.py — the monthly-quota counting rail AND its phase-2 gate.

The module is import-safe by design (pure functions over tier_registry +
caller-supplied cursors), so these tests import it directly — no main.py,
no DB. SQL-shape tests run against a recording fake cursor.

★ EVERY test here runs WITHOUT a database, deliberately. DB-backed tests
SKIP in CI (no DATABASE_URL), and a skipped test proves nothing — this
suite is the only gate standing between the phase-2 enforcement branch and
a wall in front of a paying customer, so it must actually execute there.

The phase-2 landmine these lock down: /api/v1/keys/validate emits the NODE
gate's vocabulary (enterprise|paid|identified|starter|developer|free), and
'paid' is what founding/pro/team/metered collapse to. There is no 'paid'
key in TIER_LIMITS, so a naive monthly_quota_for('paid') falls back to free
and caps every founding/pro/team customer at 300 calls/month.
"""

import ast
import os
import sys
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import monthly_quota as mq
from tier_registry import TIERS, TIER_LIMITS


class _FakeCursor:
    """Records execute() calls; serves canned fetchone rows."""

    def __init__(self, rows=None):
        self.calls = []
        self._rows = list(rows or [])

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _ExplodingCursor:
    """Every DB failure mode the gate must survive: missing table, dead pool."""

    def __init__(self, exc=None):
        self.exc = exc or RuntimeError('relation "mcp_monthly_usage" does not exist')

    def execute(self, sql, params=None):
        raise self.exc

    def fetchone(self):
        raise self.exc


def _load_from_endpoints(*names):
    """Pull real symbols out of flask_mcp_endpoints.py source.

    Importing that module would open DB pools and register blueprints, so
    the repo convention is to parse the source and exec only the nodes we
    need against a clean namespace. If _node_tier_max is edited, this
    picks the edit up — that is the point.
    """
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    wanted = set(names)
    ns = {}
    for node in tree.body:
        hit = False
        if isinstance(node, ast.Assign):
            hit = any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            hit = node.name in wanted
        if hit:
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         "<flask_mcp_endpoints>", "exec"), ns)
    missing = wanted - set(ns)
    assert not missing, f"not found in flask_mcp_endpoints.py: {sorted(missing)}"
    return ns


def test_monthly_quota_is_thirty_times_the_canonical_daily():
    # Derived, not copied: a repriced tier can never drift from its quota.
    for tier, limits in TIER_LIMITS.items():
        assert mq.monthly_quota_for(tier) == limits["mcp_daily"] * 30
    # The numbers the pricing conversation is actually about:
    assert mq.monthly_quota_for("starter") == 6000
    assert mq.monthly_quota_for("developer") == 15000
    assert mq.monthly_quota_for("pro") == 60000


def test_unknown_or_blank_tier_falls_back_to_free():
    free_monthly = TIER_LIMITS["free"]["mcp_daily"] * 30
    assert mq.monthly_quota_for("no-such-tier") == free_monthly
    assert mq.monthly_quota_for("") == free_monthly
    assert mq.monthly_quota_for(None) == free_monthly
    # Case/whitespace-insensitive like every other tier resolver.
    assert mq.monthly_quota_for("  Starter ") == 6000


def test_month_bucket_is_first_of_month_utc():
    assert mq.month_bucket(datetime(2026, 7, 30, 23, 59, tzinfo=timezone.utc)) \
        == date(2026, 7, 1)
    assert mq.month_bucket(datetime(2026, 12, 1, 0, 0, tzinfo=timezone.utc)) \
        == date(2026, 12, 1)
    # No-arg form buckets "now" — just prove it returns a first-of-month.
    assert mq.month_bucket().day == 1


def test_record_monthly_call_upserts_on_the_composite_pk():
    cur = _FakeCursor()
    ts = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    mq.record_monthly_call(cur, "dch_live_test", ts)
    assert len(cur.calls) == 1
    sql, params = cur.calls[0]
    # Conflict target must be the plain composite PK columns — a partial
    # index target would silently never match (known repo trap).
    assert "ON CONFLICT (api_key, month)" in sql
    assert "calls = mcp_monthly_usage.calls + 1" in sql
    assert params == ("dch_live_test", date(2026, 7, 1), ts)


def test_month_usage_reads_current_bucket_and_defaults_zero():
    ts = datetime(2026, 7, 15, tzinfo=timezone.utc)
    cur = _FakeCursor(rows=[(42,)])
    assert mq.month_usage(cur, "k1", ts) == 42
    _, params = cur.calls[0]
    assert params == ("k1", date(2026, 7, 1))
    # Missing row (or missing table handled by caller) reads as 0.
    assert mq.month_usage(_FakeCursor(), "k1", ts) == 0


def test_quota_snapshot_reports_the_real_enforce_flag(monkeypatch):
    # Phase 1 hard-coded enforce=False, so a consumer could not tell an
    # enforcing deploy from a counting one. It now mirrors the env switch.
    monkeypatch.delenv("MONTHLY_QUOTA_ENFORCE", raising=False)
    ts = datetime(2026, 7, 15, tzinfo=timezone.utc)
    snap = mq.quota_snapshot(_FakeCursor(rows=[(150,)]), "k1", "starter", ts)
    assert snap == {
        "month": "2026-07-01",
        "used": 150,
        "quota": 6000,
        "remaining": 5850,
        "tier": "starter",
        "enforce": False,  # default OFF
    }
    # Over-quota clamps remaining at 0 either way.
    over = mq.quota_snapshot(_FakeCursor(rows=[(7000,)]), "k1", "starter", ts)
    assert over["remaining"] == 0

    monkeypatch.setenv("MONTHLY_QUOTA_ENFORCE", "1")
    on = mq.quota_snapshot(_FakeCursor(rows=[(150,)]), "k1", "starter", ts)
    assert on["enforce"] is True


# ═════════════════════════════════════════════════════════════════════
# Phase 2 — enforcement
# ═════════════════════════════════════════════════════════════════════

TS = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _decide(used, tier, key="dch_live_x", enforce=True, monkeypatch=None):
    monkeypatch.setenv("MONTHLY_QUOTA_ENFORCE", "1" if enforce else "0")
    return mq.quota_decision(_FakeCursor(rows=[(used,)]), key, tier, TS)


# ── LANDMINE 1: the Node vocabulary must never reach the free fallback ──

def test_paid_maps_to_pro_not_free():
    # 'paid' is exactly what validate_key emits for founding/pro/team/metered.
    assert mq.resolve_quota_tier("paid") == "pro"
    assert mq.monthly_quota_for(mq.resolve_quota_tier("paid")) == 60000
    # The bug this exists to prevent: the raw name hits the free fallback.
    assert mq.monthly_quota_for("paid") == 300


def test_every_node_tier_max_output_resolves_to_a_non_free_quota_except_free():
    ns = _load_from_endpoints("_ENT_PLANS", "_PAID_PLANS", "_node_tier_max",
                              "NODE_TIER_VOCABULARY")
    node_tier_max = ns["_node_tier_max"]

    # Every plan string that can reach _node_tier_max from the three tier
    # tables: the registry's own names, plus the extras validate_key's
    # membership sets know about.
    plan_names = set(TIERS) | ns["_ENT_PLANS"] | ns["_PAID_PLANS"]
    outputs = {node_tier_max([p]) for p in plan_names}

    # The declared vocabulary must actually cover what the function emits —
    # otherwise monthly_quota's map is being checked against a stale list.
    assert outputs <= set(ns["NODE_TIER_VOCABULARY"])

    free_monthly = TIER_LIMITS["free"]["mcp_daily"] * 30
    for out in sorted(outputs | set(ns["NODE_TIER_VOCABULARY"])):
        resolved = mq.resolve_quota_tier(out)
        assert resolved is not None, f"{out!r} is unmapped — would fall back to free"
        assert resolved in TIER_LIMITS
        quota = mq.monthly_quota_for(resolved)
        if out == "free":
            assert quota == free_monthly
        else:
            # ★ The whole point: no paid/identified Node tier may land on
            # the free 300/month.
            assert quota > free_monthly, f"{out!r} resolved to the free quota"


def test_paid_tiers_never_resolve_below_their_registry_quota():
    for name in sorted(mq.PAID_QUOTA_TIERS):
        assert mq.resolve_quota_tier(name) == name
        assert mq.monthly_quota_for(name) == TIER_LIMITS[name]["mcp_daily"] * 30


def test_founding_customer_at_july_volume_is_not_blocked(monkeypatch):
    # Real regression: a founding customer did 1,201 calls in July. Fine
    # against pro's 60,000; blocked at the free fallback's 300.
    d = _decide(1201, "paid", monkeypatch=monkeypatch)
    assert d["allowed"] is True
    assert d["blocked"] is False
    assert d["quota_tier"] == "pro"
    assert d["quota"] == 60000


def test_unmapped_tier_fails_open_instead_of_returning_free(monkeypatch):
    assert mq.resolve_quota_tier("metered_v2_someday") is None
    d = _decide(999999, "metered_v2_someday", monkeypatch=monkeypatch)
    assert d["allowed"] is True
    assert d["blocked"] is False
    assert d["reason"] == "unresolved_tier"
    assert d["quota"] is None       # no guessed quota published


def test_blank_tier_fails_open(monkeypatch):
    for blank in ("", None, "   "):
        d = _decide(999999, blank, monkeypatch=monkeypatch)
        assert d["allowed"] is True, blank
        assert d["reason"] == "unresolved_tier"


# ── LANDMINE 2: keys absent from mcp_dev_keys must not be blocked ──

def test_default_exempt_prefixes_cover_the_three_known_off_registry_key_shapes(monkeypatch):
    monkeypatch.delenv("MONTHLY_QUOTA_EXEMPT", raising=False)
    for key in ("dchub_live_50b", "dchub_enterprise_comp_1", "dchub_qamcp"):
        assert mq.is_exempt(key), key
    # Ordinary keys are NOT exempt — the allowlist must stay narrow.
    for key in ("dch_live_3af44", "dch_trial_abc", "mcp_live_zz", ""):
        assert not mq.is_exempt(key), key


def test_exempt_key_is_never_blocked_even_far_over_quota(monkeypatch):
    monkeypatch.delenv("MONTHLY_QUOTA_EXEMPT", raising=False)
    d = _decide(10 ** 7, "free", key="dchub_qamcp", monkeypatch=monkeypatch)
    assert d["allowed"] is True
    assert d["blocked"] is False
    assert d["reason"] == "exempt"


def test_exempt_prefixes_are_env_overridable(monkeypatch):
    monkeypatch.setenv("MONTHLY_QUOTA_EXEMPT", "zz_, yy_")
    assert mq.exempt_prefixes() == ("zz_", "yy_")
    assert mq.is_exempt("zz_key")
    assert not mq.is_exempt("dchub_qamcp")   # replaced, not appended
    # Set-but-empty switches the allowlist off deliberately.
    monkeypatch.setenv("MONTHLY_QUOTA_EXEMPT", "")
    assert mq.exempt_prefixes() == ()
    assert not mq.is_exempt("dchub_qamcp")


# ── The switch: OFF by default ──

def test_enforcement_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MONTHLY_QUOTA_ENFORCE", raising=False)
    assert mq.enforcement_enabled() is False
    # Even a wildly over-quota free key is only logged while the switch is off.
    d = mq.quota_decision(_FakeCursor(rows=[(50000,)]), "dch_trial_rider", "free", TS)
    assert d["allowed"] is True
    assert d["blocked"] is False
    assert d["reason"] == "over_quota_log_only"
    assert d["would_block"] is True          # the log-only window reads this
    assert "message" not in d                # no wall copy served while dark


def test_only_the_literal_1_enables_enforcement(monkeypatch):
    for val in ("0", "", "true", "yes", "on", "2"):
        monkeypatch.setenv("MONTHLY_QUOTA_ENFORCE", val)
        assert mq.enforcement_enabled() is False, val
    monkeypatch.setenv("MONTHLY_QUOTA_ENFORCE", "1")
    assert mq.enforcement_enabled() is True


# ── The block itself ──

def test_free_rider_over_quota_is_blocked_when_enabled(monkeypatch):
    # The July over-quota actors: dch_trial_* free riders at 327-399 calls
    # against free's 300/month.
    d = _decide(399, "free", key="dch_trial_rider", monkeypatch=monkeypatch)
    assert d["allowed"] is False
    assert d["blocked"] is True
    assert d["reason"] == "over_monthly_quota"
    assert d["quota"] == 300
    assert d["remaining"] == 0


def test_the_wall_serves_an_upgrade_payload_not_a_bare_429(monkeypatch):
    # Design decision (a): the wall is a conversion moment, and on the MCP
    # path content is delivered through the error channel — so the offer has
    # to be IN the message, matching mcp_upgrade_gate.gate_tool_call's shape.
    from routes._stripe_links import STRIPE_LINKS
    d = _decide(6000, "starter", monkeypatch=monkeypatch)
    assert d["blocked"] is True
    assert set(("allowed", "message", "upgrade_url")) <= set(d)
    # The link is READ from _stripe_links, never hardcoded here.
    assert d["upgrade_url"] == STRIPE_LINKS["developer"]
    assert d["upgrade_url"] in d["message"]
    assert "6,000" in d["message"]           # honest, specific numbers


def test_no_stripe_url_is_hardcoded_in_monthly_quota():
    src = open(os.path.join(ROOT, "monthly_quota.py"), encoding="utf-8").read()
    assert "buy.stripe.com" not in src


def test_exact_quota_boundary_blocks(monkeypatch):
    assert _decide(5999, "starter", monkeypatch=monkeypatch)["blocked"] is False
    assert _decide(6000, "starter", monkeypatch=monkeypatch)["blocked"] is True


# ── Fail open on infrastructure (design decision (c)) ──

def test_db_error_fails_open(monkeypatch):
    monkeypatch.setenv("MONTHLY_QUOTA_ENFORCE", "1")
    for exc in (RuntimeError('relation "mcp_monthly_usage" does not exist'),
                OSError("connection pool exhausted")):
        d = mq.quota_decision(_ExplodingCursor(exc), "dch_live_x", "free", TS)
        assert d["allowed"] is True
        assert d["blocked"] is False
        assert d["reason"] == "db_error"


# ── Design decision (b): non-ok calls must not burn quota ──

def test_only_ok_calls_burn_quota():
    assert mq.counts_toward_quota("ok") is True
    assert mq.counts_toward_quota(" OK ") is True
    for status in ("error", "blocked_paid_only", "trial_used", "", None, "OKAY"):
        assert mq.counts_toward_quota(status) is False, status


def test_track_path_guards_the_rollup_on_status():
    # The rollup write lives in flask_mcp_endpoints.track_tool_call, which
    # cannot be imported here (DB pools + ~200 blueprints). Assert on the
    # source that the guard is actually wired — an unguarded
    # record_monthly_call would let a paywalled block eat a paying month.
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    assert "counts_toward_quota" in src
    i = src.index("record_monthly_call(_mq_cur")
    window = src[max(0, i - 400):i]
    assert "counts_toward_quota(body.get(\"status\"))" in window, (
        "record_monthly_call is no longer guarded by counts_toward_quota")


# ── The endpoint's tier resolution ──

def test_endpoint_picks_the_higher_quota_of_gateway_and_server_tiers(monkeypatch):
    ns = _load_from_endpoints("_best_quota_tier")
    best = ns["_best_quota_tier"]
    ns["monthly_quota"] = mq

    # Stub the server-side highest-of-3 lookup; the function under test
    # imports monthly_quota itself, so only this one needs injecting.
    def _server_tier(_key):
        return _server_tier.value
    ns["resolve_effective_node_tier"] = _server_tier

    # Gateway says free, the key is really paid in mcp_dev_keys → pro wins.
    _server_tier.value = "paid"
    assert best("free", "dch_live_x") == "pro"
    # Gateway says paid, server knows nothing (edge-minted key) → still pro.
    _server_tier.value = None
    assert best("paid", "dchub_live_50b") == "pro"
    # Neither source knows → None, and the caller fails open.
    assert best("", "dchub_live_50b") is None
    # Both agree on free → free (the free riders stay enforceable).
    _server_tier.value = "free"
    assert best("free", "dch_trial_rider") == "free"
