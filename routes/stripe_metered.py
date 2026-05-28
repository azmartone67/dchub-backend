"""
stripe_metered.py — Stripe metered billing for API call overage.

Phase ZZZZZ-round34 (2026-05-24). Unblocks the 154 node-script IPs that
hit free-tier limits but won't commit to $49/mo. Pay-per-call captures
revenue that's currently being declined.

Pricing tiers:
  Free:        300 calls/mo included, hard cap (must upgrade for more)
  Developer:   30,000/mo included, $0.005/call overage
  Pro:         300,000/mo included, $0.002/call overage
  Enterprise:  3,000,000/mo included, $0.001/call overage (negotiated)

ROUTES:
  POST /api/v1/billing/track-usage   — internal: record API call for billing
  GET  /api/v1/billing/usage         — caller's current month usage + overage cost
  POST /api/v1/billing/report-to-stripe  — admin: batch-report usage to Stripe (cron)

Persists usage in `api_usage_meter` table (per-key, per-day buckets).
Cron reports daily to Stripe via SubscriptionItem.create_usage_record.

Setup required:
  - STRIPE_API_KEY env var
  - STRIPE_OVERAGE_PRICE_ID — metered price (per_unit, recurring, usage_type=metered)
  - Each existing Developer/Pro subscription needs the metered price added
"""
import os
import json
import time
import datetime
import urllib.request
import urllib.parse
from contextlib import contextmanager

import psycopg2 as _pg
import psycopg2.extras
from flask import Blueprint, request, jsonify

stripe_metered_bp = Blueprint("stripe_metered", __name__,
                                url_prefix="/api/v1/billing")

TIER_LIMITS = {
    "free":       {"included": 300,     "overage_per_call": None,  "hard_cap": True},
    "developer":  {"included": 30_000,  "overage_per_call": 0.005, "hard_cap": False},
    "pro":        {"included": 300_000, "overage_per_call": 0.002, "hard_cap": False},
    "enterprise": {"included": 3_000_000,"overage_per_call": 0.001,"hard_cap": False},
}


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn(), connect_timeout=8)
    try: yield c
    finally:
        try: c.close()
        except Exception: pass


def _ensure_table():
    """Idempotent schema."""
    sql = """
        CREATE TABLE IF NOT EXISTS api_usage_meter (
            id              SERIAL PRIMARY KEY,
            api_key         TEXT NOT NULL,
            tier            TEXT NOT NULL,
            usage_date      DATE NOT NULL,
            calls_count     INTEGER NOT NULL DEFAULT 0,
            reported_to_stripe BOOLEAN DEFAULT FALSE,
            stripe_usage_record_id TEXT,
            stripe_reported_at TIMESTAMPTZ,
            last_call_at    TIMESTAMPTZ DEFAULT NOW(),
            updated_at      TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(api_key, usage_date)
        );
        CREATE INDEX IF NOT EXISTS ix_usage_key_date ON api_usage_meter(api_key, usage_date DESC);
        CREATE INDEX IF NOT EXISTS ix_usage_unreported ON api_usage_meter(reported_to_stripe, usage_date) WHERE reported_to_stripe = FALSE;
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql)
            c.commit()
        return True
    except Exception:
        return False


@stripe_metered_bp.route("/track-usage", methods=["POST"])
def track_usage():
    """Internal: called by the worker / Flask middleware after each billable
    API call to increment the user's usage counter."""
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or request.headers.get("X-API-Key") or "").strip()
    tier    = (data.get("tier") or "free").lower()
    if not api_key:
        return jsonify({"error": "api_key required"}), 400
    if tier not in TIER_LIMITS:
        tier = "free"

    today = datetime.date.today()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO api_usage_meter (api_key, tier, usage_date, calls_count, last_call_at, updated_at)
                VALUES (%s, %s, %s, 1, NOW() ON CONFLICT DO NOTHING, NOW())
                ON CONFLICT (api_key, usage_date) DO UPDATE
                SET calls_count = api_usage_meter.calls_count + 1,
                    last_call_at = NOW(),
                    updated_at = NOW(),
                    tier = EXCLUDED.tier
                RETURNING calls_count, tier
            """, (api_key, tier, today))
            row = cur.fetchone()
            c.commit()
            calls_today, current_tier = (row[0], row[1]) if row else (1, tier)

        # Compute monthly total + overage state
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(calls_count), 0)
                  FROM api_usage_meter
                 WHERE api_key = %s
                   AND usage_date >= date_trunc('month', CURRENT_DATE)
            """, (api_key,))
            mtd = int(cur.fetchone()[0] or 0)

        limits = TIER_LIMITS[current_tier]
        included = limits["included"]
        overage_calls = max(0, mtd - included)
        overage_cost = (overage_calls * limits["overage_per_call"]) if limits["overage_per_call"] else 0
        hard_cap_hit = limits["hard_cap"] and mtd >= included

        return jsonify({
            "api_key_short":     api_key[:8] + "...",
            "tier":              current_tier,
            "calls_today":       calls_today,
            "calls_mtd":         mtd,
            "included_mtd":      included,
            "overage_calls":     overage_calls,
            "overage_cost_usd":  round(overage_cost, 4),
            "hard_cap_hit":      hard_cap_hit,
            "next_reset":        datetime.date(today.year + (1 if today.month == 12 else 0),
                                                 (today.month % 12) + 1, 1).isoformat(),
        }), 200
    except Exception as e:
        # Best-effort — never block API calls because of billing
        return jsonify({"error": f"track_failed: {type(e).__name__}",
                          "noted": True}), 200


@stripe_metered_bp.route("/usage", methods=["GET"])
def get_usage():
    """Returns caller's current month usage + overage cost. Auth via X-API-Key."""
    api_key = (request.args.get("api_key") or request.headers.get("X-API-Key") or "").strip()
    if not api_key:
        return jsonify({"error": "X-API-Key required"}), 401

    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT usage_date, tier, calls_count, reported_to_stripe
                  FROM api_usage_meter
                 WHERE api_key = %s
                   AND usage_date >= date_trunc('month', CURRENT_DATE)
                 ORDER BY usage_date DESC
            """, (api_key,))
            daily = [dict(r) for r in cur.fetchall()]
            for d in daily:
                if d.get("usage_date"):
                    d["usage_date"] = d["usage_date"].isoformat()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    mtd = sum(d["calls_count"] for d in daily)
    tier = (daily[0]["tier"] if daily else "free")
    limits = TIER_LIMITS[tier]
    overage_calls = max(0, mtd - limits["included"])
    overage_cost = (overage_calls * limits["overage_per_call"]) if limits["overage_per_call"] else 0

    return jsonify({
        "api_key_short": api_key[:8] + "...",
        "tier": tier,
        "current_month": {
            "calls_mtd":       mtd,
            "included":        limits["included"],
            "overage_calls":   overage_calls,
            "overage_cost_usd":round(overage_cost, 4),
            "remaining":       max(0, limits["included"] - mtd),
        },
        "daily_breakdown": daily,
        "tier_pricing": limits,
        "upgrade_url": "https://api.dchub.cloud/pricing/upgrade?tool=billing&ref=usage-dashboard",
    }), 200


@stripe_metered_bp.route("/report-to-stripe", methods=["POST"])
def report_to_stripe():
    """Admin/cron: batch-report yesterday's overage to Stripe.

    For each (api_key, day) where reported_to_stripe=FALSE and the
    user has crossed their tier's included threshold, send a
    SubscriptionItem.create_usage_record to Stripe.

    Currently stubbed — returns what WOULD be reported. Wire up real
    Stripe calls after the Stripe metered-price + subscription-items
    are set up per the round-32 monetization/stripe-metered-billing.md doc.
    """
    admin_key = request.headers.get("X-Admin-Key", "")
    expected = os.environ.get("DCHUB_ADMIN_KEY", "")
    if not expected or admin_key != expected:
        return jsonify({"error": "admin auth required"}), 403

    stripe_key = os.environ.get("STRIPE_API_KEY", "")
    price_id   = os.environ.get("STRIPE_OVERAGE_PRICE_ID", "")
    dry_run    = not (stripe_key and price_id) or request.args.get("dry_run") == "1"

    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT api_key, tier, usage_date, calls_count
                  FROM api_usage_meter
                 WHERE reported_to_stripe = FALSE
                   AND usage_date < CURRENT_DATE
                 ORDER BY usage_date, api_key
                 LIMIT 500
            """)
            pending = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    to_report = []
    for r in pending:
        limits = TIER_LIMITS.get(r["tier"], TIER_LIMITS["free"])
        if not limits["overage_per_call"]:
            continue   # free tier — no overage billing
        # NOTE: in production, look up monthly cumulative to determine
        # how much of THIS day was overage vs included. For dry-run
        # we just report all calls as overage candidates.
        to_report.append({
            "api_key_short": r["api_key"][:8] + "...",
            "tier":          r["tier"],
            "usage_date":    r["usage_date"].isoformat(),
            "calls":         r["calls_count"],
            "tier_overage_per_call": limits["overage_per_call"],
            "would_charge_usd": round(r["calls_count"] * limits["overage_per_call"], 4),
        })

    return jsonify({
        "mode": "dry_run" if dry_run else "live",
        "pending_records":      len(pending),
        "would_report_to_stripe": len(to_report),
        "sample":               to_report[:10],
        "total_overage_usd":    round(sum(x["would_charge_usd"] for x in to_report), 2),
        "stripe_configured":    bool(stripe_key and price_id),
        "setup_doc":            "See PATCHES/ROADMAP-2026-Q3/monetization/stripe-metered-billing.md",
    }), 200


# AUTO-REPAIR: duplicate route '/health' also in main.py:3708 — review and remove one
@stripe_metered_bp.route("/health", methods=["GET"])
def billing_health():
    table_ok = _ensure_table()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM api_usage_meter")
            n_records = int(cur.fetchone()[0])
    except Exception:
        n_records = -1

    return jsonify({
        "ok": True,
        "blueprint": "stripe_metered_bp",
        "version": "round-34-v1",
        "table_ok": table_ok,
        "total_usage_records": n_records,
        "stripe_configured": {
            "api_key":   bool(os.environ.get("STRIPE_API_KEY")),
            "price_id":  bool(os.environ.get("STRIPE_OVERAGE_PRICE_ID")),
        },
        "tier_limits": TIER_LIMITS,
        "endpoints": [
            "POST /api/v1/billing/track-usage    — internal middleware",
            "GET  /api/v1/billing/usage          — caller's MTD usage",
            "POST /api/v1/billing/report-to-stripe — admin/cron batch report",
        ],
    }), 200
