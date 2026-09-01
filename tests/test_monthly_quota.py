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

    def fetchall(self):
        # Serves the next canned row VERBATIM — queue a list of tuples for
        # multi-row results (wall_stats' by-tier breakdown).
        return self._rows.pop(0) if self._rows else []


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


# ── The wall-activity rollup (r-wall-metrics, 2026-08-10) ──
# The wall firing is the conversion event MONTHLY_QUOTA_ENFORCE was flipped
# for; these lock down the rollup that makes it visible on the funnel.

def test_record_wall_hit_is_a_noop_unless_the_decision_hit_the_wall():
    # Every decision can be passed through; only would_block ones write.
    for decision in ({"allowed": True, "reason": "under_quota"},
                     {"allowed": True, "reason": "exempt"},
                     {}, None):
        cur = _FakeCursor()
        assert mq.record_wall_hit(cur, "k1", decision) is False
        assert cur.calls == []


def test_record_wall_hit_upserts_on_the_composite_pk_and_counts_blocks():
    ts = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    # to_regclass says the table exists → no CREATE, straight to the upsert.
    cur = _FakeCursor(rows=[("mcp_quota_wall_hits",)])
    decision = {"would_block": True, "blocked": True,
                "quota_tier": "starter", "quota": 6000}
    assert mq.record_wall_hit(cur, "dch_live_k", decision, ts) is True
    assert len(cur.calls) == 2  # to_regclass + upsert, nothing else
    sql, params = cur.calls[-1]
    # Plain composite-PK conflict target (the known partial-index trap).
    assert "ON CONFLICT (api_key, month)" in sql
    assert "hits = mcp_quota_wall_hits.hits + 1" in sql
    # first_hit_at is insert-only: the conflict UPDATE must never touch it.
    assert "first_hit_at = " not in sql.split("DO UPDATE SET", 1)[1]
    assert params == ("dch_live_k", date(2026, 8, 1), "starter", 6000, 1, ts, ts)


def test_record_wall_hit_log_only_hit_is_not_counted_as_blocked():
    # Switch off → would_block without blocked. The hit is recorded (the
    # log-only review window wants it) but the blocked increment is 0, so
    # blocked_hits stays an honest count of allowed=false actually served.
    ts = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    cur = _FakeCursor(rows=[("mcp_quota_wall_hits",)])
    decision = {"would_block": True, "blocked": False,
                "reason": "over_quota_log_only",
                "quota_tier": "identified", "quota": 1500}
    assert mq.record_wall_hit(cur, "dch_trial_k", decision, ts) is True
    _, params = cur.calls[-1]
    assert params == ("dch_trial_k", date(2026, 8, 1), "identified", 1500, 0, ts, ts)


def test_record_wall_hit_lazily_creates_the_rollup_table():
    ts = datetime(2026, 8, 10, tzinfo=timezone.utc)
    cur = _FakeCursor(rows=[(None,)])  # to_regclass: table missing
    decision = {"would_block": True, "blocked": True,
                "quota_tier": "free", "quota": 300}
    assert mq.record_wall_hit(cur, "k1", decision, ts) is True
    assert len(cur.calls) == 3  # to_regclass + CREATE + upsert
    create_sql = cur.calls[1][0]
    assert "CREATE TABLE IF NOT EXISTS mcp_quota_wall_hits" in create_sql
    assert "PRIMARY KEY (api_key, month)" in create_sql


def test_wall_stats_missing_table_reads_as_zeros_not_an_error():
    # Zeros are ACCURATE before the first hit — the dashboard must show an
    # explicit 0, not a hole, before the wall starts firing.
    cur = _FakeCursor(rows=[(None,), (None,)])
    st = mq.wall_stats(cur)
    assert st["table_exists"] is False
    assert st["hits_month"] == 0 and st["blocked_month"] == 0
    assert st["keys_month"] == 0 and st["keys_first_hit_7d"] == 0
    assert st["by_tier_month"] == [] and st["last_hit_at"] is None
    # No WALL aggregate may run when the wall table is absent. (Asserted on the
    # SQL rather than on len(cur.calls): r-wall-scope added a headroom probe on
    # this path, and a bare call-count would have made this test fail for a
    # reason that has nothing to do with what it guards.)
    wall_sql = [c for c, _ in cur.calls if "mcp_quota_wall_hits" in c]
    assert len(wall_sql) == 1 and "to_regclass" in wall_sql[0]
    assert not [c for c, _ in cur.calls if "SUM(" in c or "COUNT(*)" in c
                and "mcp_quota_wall_hits" in c]


def test_wall_stats_aggregates_and_casts_decimals(monkeypatch):
    # SUM() comes back Decimal from psycopg — the payload must carry ints
    # (json serializes Decimal as a string in some encoders; the funnel
    # must never depend on which).
    from decimal import Decimal
    monkeypatch.setenv("MONTHLY_QUOTA_ENFORCE", "1")
    ts = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    last = datetime(2026, 8, 9, 23, 0, tzinfo=timezone.utc)
    cur = _FakeCursor(rows=[
        ("mcp_quota_wall_hits",),                    # to_regclass
        (Decimal(7), Decimal(5), 3, last),           # month totals
        (2,),                                        # first-hit 7d
        [("identified", 1500, 2, Decimal(5), Decimal(4)),   # by-tier rows
         ("free", 300, 1, Decimal(2), Decimal(1))],
    ])
    st = mq.wall_stats(cur, ts)
    assert st["enforce"] is True
    assert st["month"] == "2026-08-01"
    assert st["hits_month"] == 7 and isinstance(st["hits_month"], int)
    assert st["blocked_month"] == 5 and isinstance(st["blocked_month"], int)
    assert st["keys_month"] == 3
    assert st["keys_first_hit_7d"] == 2
    assert st["last_hit_at"] == last.isoformat()
    assert st["by_tier_month"] == [
        {"tier": "identified", "quota": 1500, "keys": 2, "hits": 5, "blocked": 4},
        {"tier": "free", "quota": 300, "keys": 1, "hits": 2, "blocked": 1},
    ]
    # The month totals query must be scoped to the current month bucket.
    _, params = cur.calls[1]
    assert params == (date(2026, 8, 1),)


def test_decision_endpoint_records_wall_hits_guarded_and_fail_soft():
    # Same source-assertion convention as the track-path guard above: the
    # endpoint cannot be imported (DB pools + blueprints), so pin the wiring
    # in the source. The write must be (a) gated on would_block and (b)
    # wrapped so a metrics failure can never break the decision served.
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    i = src.index("record_wall_hit(cur")
    window = src[max(0, i - 400):i]
    assert 'decision.get("would_block")' in window, (
        "record_wall_hit is no longer gated on would_block — it would write "
        "a wall hit for every decision")
    assert "try:" in window, (
        "record_wall_hit is no longer fail-soft guarded — a metrics write "
        "failure would break the decision endpoint")


def test_funnel_endpoint_emits_the_quota_wall_block():
    # The funnel payload is what the dashboard renders next to "Upgrade
    # signals" — the block must exist and must come from wall_stats.
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    assert 'out["quota_wall"]' in src
    i = src.index('out["quota_wall"] = _wall_stats(cur)')
    window = src[max(0, i - 300):i]
    assert "from monthly_quota import wall_stats" in window


# ── the false positive this telemetry caused (2026-08-10) ────────────────────

def test_absent_wall_table_ships_its_own_interpretation():
    """brain L15 filed #2542 at HIGH confidence off a bare `table_exists: false`,
    concluding the quota-wall migration had never run and 114 free keys were
    hammering paid tools unchecked. Both halves were wrong:
    mcp_quota_wall_hits is created LAZILY on the first wall hit (there is no
    migration), and the heaviest keys that month were tier `paid` -> `pro` at
    2,563 calls against a 60,000/month quota.

    A flag whose plain reading inverts its meaning must carry the meaning with
    it, or the next reader — human or detector — repeats the same mistake.
    """
    import monthly_quota as mq

    class _Cur:
        def execute(self, sql, args=None):
            self._sql = sql
        def fetchone(self):
            return (None,)          # to_regclass -> table absent

    out = mq.wall_stats(_Cur())
    assert out["table_exists"] is False
    assert out["lazily_created"] is True, \
        "absence must be labelled as lazy-creation, not a missing migration"
    text = out.get("interpretation", "").lower()
    assert "migration" in text and "no key has ever reached" in text, \
        f"interpretation must say what the flag MEANS, got: {out.get('interpretation')!r}"


def test_paid_tier_quota_is_not_the_free_ceiling():
    """The #2542 chain assumed heavy callers were free-tier. `paid` resolves to
    `pro`, and conflating the two is what made 0 wall hits look like a bug."""
    import monthly_quota as mq
    from tier_registry import MCP_DAYS_PER_MONTH, TIER_LIMITS

    assert mq.resolve_quota_tier("paid") == "pro"
    pro_month = TIER_LIMITS["pro"]["mcp_daily"] * MCP_DAYS_PER_MONTH
    free_month = TIER_LIMITS["free"]["mcp_daily"] * MCP_DAYS_PER_MONTH
    assert pro_month > free_month * 10, (
        f"pro={pro_month}/mo vs free={free_month}/mo — the observed 2,563-call "
        f"month is 4% of pro and 854% of free; which tier applies decides "
        f"whether zero wall hits is a bug or correct")


# ── r-wall-scope (2026-09-01): the block must say WHICH wall it is ──────────
# `quota_wall` renders beside the upgrade-signal counters as "Quota wall hits
# (this month) · enforce ON". A standing 0 there was read as "the paywall
# works". It is the MONTHLY quota (free 300/mo) measured against 5.03
# calls/key/30d, so it cannot fire; the gate that actually bites is the
# mcp-server per-day full-answer cap, whose hits never appear here.


def test_wall_block_names_the_wall_it_measures_on_both_paths():
    # BOTH return paths, because the live one today is the missing-table path.
    missing = mq.wall_stats(_FakeCursor(rows=[(None,), (None,)]))
    from decimal import Decimal
    present = mq.wall_stats(_FakeCursor(rows=[
        ("mcp_quota_wall_hits",), (Decimal(1), Decimal(1), 1, None), (0,), [],
        (None,),
    ]))
    for st in (missing, present):
        assert "MONTHLY" in st["measures"]
        assert "monthly_quota.py" in st["measures"]
        assert "300/mo" in st["measures"]        # computed, not hardcoded prose
        assert "per-DAY full-answer cap" in st["not_measured_here"]
        assert "dchub-mcp-server#294" in st["not_measured_here"]


def test_not_measured_here_refuses_both_misreadings():
    st = mq.wall_stats(_FakeCursor(rows=[(None,), (None,)]))
    # The string must forbid reading 0 as EITHER verdict. A caveat that only
    # blocked the optimistic misreading would invite the pessimistic one --
    # which is exactly the false positive brain L15 filed as #2542.
    assert "the paywall works" in st["not_measured_here"]
    assert "the paywall is broken" in st["not_measured_here"]


def test_wall_headroom_answers_can_it_fire_with_a_number():
    # heaviest key well under the SMALLEST monthly quota -> provably no key of
    # any tier reached its wall.
    h = mq.wall_headroom(_FakeCursor(rows=[("mcp_monthly_usage",), (97, 12)]))
    assert h["heaviest_key_calls_month"] == 97
    assert h["smallest_monthly_quota"] == 300
    assert h["keys_with_usage_month"] == 12
    assert h["could_any_key_have_hit"] is False


def test_wall_headroom_is_one_way_and_says_so():
    h = mq.wall_headroom(_FakeCursor(rows=[("mcp_monthly_usage",), (5000, 3)]))
    assert h["could_any_key_have_hit"] is True
    # True must NOT be published as "a key hit its wall" -- every tier above
    # free has a larger quota, so passing 300 proves nothing about that key.
    assert "ONE-WAY" in h["basis"]
    assert "NOT that any key hit its own" in h["basis"]


def test_wall_headroom_is_fail_soft():
    # A diagnostic on a dashboard must never take the payload down.
    assert mq.wall_headroom(_FakeCursor(rows=[(None,)])) is None   # no table
    assert mq.wall_headroom(_ExplodingCursor()) is None            # dead pool


def test_headroom_failure_cannot_cost_the_wall_counts():
    # wall_headroom runs LAST on the populated path. If mcp_monthly_usage is
    # missing, the counts above it must still be intact -- the ordering is the
    # guarantee, so assert the outcome that ordering buys.
    from decimal import Decimal
    st = mq.wall_stats(_FakeCursor(rows=[
        ("mcp_quota_wall_hits",),
        (Decimal(9), Decimal(4), 2, None),
        (1,),
        [("free", 300, 1, Decimal(9), Decimal(4))],
        (None,),                      # headroom: mcp_monthly_usage absent
    ]))
    assert st["hits_month"] == 9 and st["blocked_month"] == 4
    assert st["keys_month"] == 2 and st["keys_first_hit_7d"] == 1
    assert st["by_tier_month"][0]["tier"] == "free"
    assert st["headroom"] is None
