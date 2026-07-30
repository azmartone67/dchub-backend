"""monthly_quota.py — the log-only monthly-quota counting rail.

The module is import-safe by design (pure functions over tier_registry +
caller-supplied cursors), so these tests import it directly — no main.py,
no DB. SQL-shape tests run against a recording fake cursor.
"""

from datetime import date, datetime, timezone

import monthly_quota as mq
from tier_registry import TIER_LIMITS


class _FakeCursor:
    """Records execute() calls; serves canned fetchone rows."""

    def __init__(self, rows=None):
        self.calls = []
        self._rows = list(rows or [])

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


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


def test_quota_snapshot_is_log_only():
    ts = datetime(2026, 7, 15, tzinfo=timezone.utc)
    snap = mq.quota_snapshot(_FakeCursor(rows=[(150,)]), "k1", "starter", ts)
    assert snap == {
        "month": "2026-07-01",
        "used": 150,
        "quota": 6000,
        "remaining": 5850,
        "tier": "starter",
        "enforce": False,  # phase 1: nothing may block on this snapshot
    }
    # Over-quota stays log-only and clamps remaining at 0.
    over = mq.quota_snapshot(_FakeCursor(rows=[(7000,)]), "k1", "starter", ts)
    assert over["remaining"] == 0
    assert over["enforce"] is False
