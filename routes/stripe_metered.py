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
                VALUES (%s, %s, %s, 1, NOW(), NOW())
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


# ── r59-conv (2026-06-01): NEW Stripe Meters API ───────────────────────────
# The owner built a usage Meter (event_name `dchub_api_call`) + a usage-based
# price (price_1TdNixJ9ey2ATcQldRAdlc7z). This replaces the legacy stub +
# SubscriptionItem.create_usage_record path. We bill from mcp_call_log (the
# real, populated per-key call source — /track-usage was never wired, so
# api_usage_meter is empty) and report to the Meter per metered customer.
# metered_keys maps api_key → stripe_customer_id (sales-assisted onboarding).
_METER_EVENT      = os.environ.get("DCHUB_STRIPE_METER_EVENT", "dchub_api_call")
_METERED_PRICE_ID = os.environ.get("DCHUB_METERED_PRICE_ID",
                                   "price_1TdNixJ9ey2ATcQldRAdlc7z")


def _ensure_metered_keys():
    """Idempotent: api_key → Stripe customer mapping for usage-based billing."""
    sql = """
        CREATE TABLE IF NOT EXISTS metered_keys (
            api_key            TEXT PRIMARY KEY,
            stripe_customer_id TEXT NOT NULL,
            subscription_id    TEXT,
            active             BOOLEAN NOT NULL DEFAULT TRUE,
            last_reported_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            linked_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(sql); c.commit()
        return True
    except Exception:
        return False


def _stripe_meter_event(stripe_key, value, customer_id):
    """POST a billing meter event (new Stripe Meters API). Returns (ok, detail)."""
    import urllib.error
    data = urllib.parse.urlencode({
        "event_name": _METER_EVENT,
        "payload[value]": str(int(value)),
        "payload[stripe_customer_id]": customer_id,
    }).encode()
    req = urllib.request.Request(
        "https://api.stripe.com/v1/billing/meter_events", data=data,
        headers={"Authorization": "Bearer " + stripe_key,
                 "Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return (r.status < 300), r.read().decode("utf-8", "replace")[:160]
    except urllib.error.HTTPError as e:
        try: body = e.read().decode("utf-8", "replace")[:200]
        except Exception: body = ""
        return False, "http_%s:%s" % (e.code, body)
    except Exception as e:
        return False, "%s:%s" % (type(e).__name__, str(e)[:160])


# r62-conv (2026-06-01): self-serve auto-key for the usage-based payment link.
# The link (buy.stripe.com/9B69AU…) is a QUANTITY subscription on product
# prod_UccyUrO1iq7LrN ($0.01/unit/mo, customer picks 1-10000 units). On
# checkout.session.completed the main.py webhook calls handle_usage_based_checkout
# (fail-soft) → auto-issue a dch_live_ key sized to the purchased quantity,
# link it to the Stripe customer, and email it. No usage-reporting needed for
# this product (it's a fixed-quantity sub, not the pure-metered price).
_USAGE_PRODUCT_ID = os.environ.get("DCHUB_USAGE_PRODUCT_ID", "prod_UccyUrO1iq7LrN")


def _stripe_get(path, stripe_key):
    req = urllib.request.Request("https://api.stripe.com" + path,
                                 headers={"Authorization": "Bearer " + stripe_key}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _qty_to_tier(qty):
    """Map purchased units → nearest existing paid tier (existing gate caps;
    no per-key-cap change needed)."""
    if qty <= 500:   return "starter"
    if qty <= 1000:  return "developer"
    if qty <= 10000: return "pro"
    return "enterprise"


def handle_usage_based_checkout(session):
    """Fail-soft. Auto-issue + email a key for a usage-based (prod_UccyUrO1iq7LrN)
    subscription checkout. No-op for any other checkout. Idempotent per customer."""
    import secrets as _secrets
    try:
        stripe_key = (os.environ.get("STRIPE_SECRET_KEY", "")
                      or os.environ.get("STRIPE_API_KEY", ""))
        if not stripe_key or not isinstance(session, dict):
            return {"ok": False, "skip": "no_key_or_session"}
        sid = session.get("id")
        email = (((session.get("customer_details") or {}).get("email"))
                 or session.get("customer_email") or "").strip().lower()
        customer = session.get("customer") or ""
        li = (_stripe_get(
            "/v1/checkout/sessions/%s/line_items?limit=10&expand[]=data.price.product" % sid,
            stripe_key) if sid else None)
        qty = None
        for it in (li or {}).get("data", []):
            prod = (it.get("price") or {}).get("product")
            prod_id = prod.get("id") if isinstance(prod, dict) else prod
            prod_nm = prod.get("name", "") if isinstance(prod, dict) else ""
            if prod_id == _USAGE_PRODUCT_ID or "Usage-Based" in str(prod_nm):
                qty = int(it.get("quantity") or 100)
                break
        if qty is None:
            return {"ok": False, "skip": "not_usage_based"}
        tier = _qty_to_tier(qty)
        _ensure_metered_keys()
        api_key = None
        with _conn() as c, c.cursor() as cur:
            if customer:
                cur.execute("SELECT api_key FROM metered_keys WHERE stripe_customer_id = %s LIMIT 1", (customer,))
                row = cur.fetchone()
                if row:
                    api_key = row[0]
            if not api_key:
                api_key = "dch_live_" + _secrets.token_hex(16)
                cur.execute("""
                    INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, status, metadata)
                    VALUES (%s, %s, %s, %s, 'active', %s::jsonb)
                    ON CONFLICT (api_key) DO NOTHING
                """, (api_key, "dev_" + _secrets.token_hex(8), (email or None), tier,
                      json.dumps({"source": "usage_based_checkout", "qty": qty,
                                  "stripe_customer_id": customer})))
                if customer:
                    cur.execute("""
                        INSERT INTO metered_keys (api_key, stripe_customer_id, subscription_id, active, last_reported_at, linked_at)
                        VALUES (%s, %s, %s, TRUE, NOW(), NOW())
                        ON CONFLICT (api_key) DO UPDATE SET active = TRUE, stripe_customer_id = EXCLUDED.stripe_customer_id
                    """, (api_key, customer, session.get("subscription")))
            c.commit()
        email_ok, email_detail = (False, "no_email_address")
        if email and api_key:
            try:
                from routes.redeem_routes import _p99_send_email
                email_ok, email_detail = _p99_send_email(email, api_key, [])
            except Exception as _ee:
                email_ok, email_detail = False, f"{type(_ee).__name__}: {str(_ee)[:120]}"
        # honest status: emailed == the send actually succeeded (Resend/SMTP 2xx),
        # NOT merely "an address existed". The webhook print surfaces email_detail
        # so a silent delivery failure is diagnosable in the Railway log.
        return {"ok": True, "tier": tier, "qty": qty, "emailed": email_ok,
                "email_detail": str(email_detail)[:200],
                "key_short": (api_key or "")[:12] + "…"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


@stripe_metered_bp.route("/link-metered-key", methods=["POST"])
def link_metered_key():
    """Admin: link a dchub api_key to a Stripe customer on the usage-based
    plan (sales-assisted onboarding). Body: {api_key, stripe_customer_id,
    subscription_id?}. Also flips the key's mcp_dev_keys tier to 'metered'."""
    admin_key = (request.headers.get("X-Admin-Key", "")
                 or request.headers.get("X-Internal-Key", ""))
    expected = (os.environ.get("DCHUB_ADMIN_KEY", "")
                or os.environ.get("DCHUB_INTERNAL_KEY", ""))
    if not expected or admin_key != expected:
        return jsonify({"error": "admin auth required"}), 403
    d = request.get_json(silent=True) or {}
    api_key = (d.get("api_key") or "").strip()
    cust    = (d.get("stripe_customer_id") or "").strip()
    sub     = (d.get("subscription_id") or "").strip() or None
    if not api_key or not cust.startswith("cus_"):
        return jsonify({"error": "api_key + valid stripe_customer_id (cus_...) required"}), 400
    _ensure_metered_keys()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO metered_keys
                    (api_key, stripe_customer_id, subscription_id, active, last_reported_at, linked_at)
                VALUES (%s, %s, %s, TRUE, NOW(), NOW())
                ON CONFLICT (api_key) DO UPDATE
                  SET stripe_customer_id = EXCLUDED.stripe_customer_id,
                      subscription_id    = EXCLUDED.subscription_id,
                      active             = TRUE
            """, (api_key, cust, sub))
            try:
                # Access tier = 'enterprise' so the gate unlocks everything at
                # an uncapped daily limit (metered customers pay per call, not
                # capped). The gate (applyTierGate) only honors 'paid'/
                # 'enterprise' — 'metered' would be unrecognized → blocked. The
                # metered_keys row is the source of truth that this is usage-billed.
                cur.execute("UPDATE mcp_dev_keys SET tier = 'enterprise' WHERE api_key = %s", (api_key,))
            except Exception:
                pass
            c.commit()
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"ok": True, "api_key_short": api_key[:10] + "...",
                    "stripe_customer_id": cust, "access_tier": "enterprise",
                    "billing": "metered", "meter_event": _METER_EVENT}), 200


@stripe_metered_bp.route("/recover-usage-key", methods=["POST"])
def recover_usage_key():
    """Admin diagnostic + recovery for a usage-based (prod_UccyUrO1iq7LrN)
    subscription that PAID but never got its key (the live checkout.session.completed
    path failed). Two jobs:
      1. DIAGNOSE delivery — reports pending_webhooks for recent
         checkout.session.completed events. >0 ⇒ Stripe could NOT get a 2xx from
         our endpoint (signature mismatch / proxy altered the raw body / timeout).
         =0 ⇒ delivered & acknowledged (so the gap is downstream: handler or email).
      2. RECOVER — fetch the sub/session from Stripe, confirm it's the usage
         product, and (dry_run:false) issue + email the dch_live_ key sized to the
         purchased quantity. Idempotent per customer; reuses the live webhook logic.
    Returns only a SHORT key prefix — the full key is delivered solely by email
    (so it never lands in a log). If email fails, email_detail names the cause.
    Gate: X-Internal-Key (dchub-internal-sync-2026) OR X-Admin-Key==DCHUB_ADMIN_KEY.
    Body: {subscription_id?, session_id?, customer_id?, email?, dry_run?:true}
    """
    import secrets as _secrets
    hdr_admin = (request.headers.get("X-Admin-Key", "") or "").strip()
    expected  = (os.environ.get("DCHUB_ADMIN_KEY", "")
                 or os.environ.get("DCHUB_INTERNAL_KEY", "")).strip()
    ok_admin  = bool(expected) and hdr_admin == expected
    ok_intl   = False
    try:
        from internal_auth import is_valid_internal_key
        ok_intl = is_valid_internal_key(request.headers.get("X-Internal-Key", ""))
    except Exception:
        ok_intl = False
    if not (ok_admin or ok_intl):
        return jsonify({"error": "admin auth required (X-Admin-Key or X-Internal-Key)"}), 403

    stripe_key = (os.environ.get("STRIPE_SECRET_KEY", "")
                  or os.environ.get("STRIPE_API_KEY", "")).strip()
    if not stripe_key:
        return jsonify({"error": "no STRIPE_SECRET_KEY on backend"}), 503

    d = request.get_json(silent=True) or {}
    sub_id   = (d.get("subscription_id") or "").strip() or None
    sess_id  = (d.get("session_id") or "").strip() or None
    cust_in  = (d.get("customer_id") or "").strip() or None
    email_ov = ((d.get("email") or "").strip().lower()) or None
    dry_run  = bool(d.get("dry_run", True))

    # --- 1. delivery diagnosis ------------------------------------------
    diag = {}
    try:
        ev = _stripe_get("/v1/events?type=checkout.session.completed&limit=10", stripe_key)
        evs = [{"id": e.get("id"), "created": e.get("created"),
                "pending_webhooks": e.get("pending_webhooks")}
               for e in (ev or {}).get("data", [])]
        diag = {
            "recent_checkout_events": len(evs),
            "events_with_pending_webhooks": sum(1 for e in evs if (e.get("pending_webhooks") or 0) > 0),
            "sample": evs[:5],
            "interpretation": ("pending_webhooks>0 ⇒ Stripe couldn't get a 2xx (signature/"
                               "proxy/timeout). =0 ⇒ delivered+acked (gap is handler or email)."),
        }
    except Exception as _e:
        diag = {"error": str(_e)[:160]}

    # --- 2. resolve product / qty / customer ----------------------------
    prod_id = prod_nm = None
    qty = None
    customer = cust_in
    sub_status = None
    if sess_id:
        li = _stripe_get("/v1/checkout/sessions/%s/line_items?limit=10&expand[]=data.price.product" % sess_id, stripe_key)
        for it in (li or {}).get("data", []):
            prod = (it.get("price") or {}).get("product")
            prod_id = prod.get("id") if isinstance(prod, dict) else prod
            prod_nm = prod.get("name", "") if isinstance(prod, dict) else ""
            qty = int(it.get("quantity") or 100); break
        sess = _stripe_get("/v1/checkout/sessions/%s" % sess_id, stripe_key) or {}
        customer = customer or sess.get("customer")
        email_ov = email_ov or (((sess.get("customer_details") or {}).get("email")) or sess.get("customer_email"))
        sub_status = "via_session"
    elif sub_id:
        sub = _stripe_get("/v1/subscriptions/%s?expand[]=items.data.price.product" % sub_id, stripe_key)
        if not sub:
            return jsonify({"error": "subscription not found / Stripe fetch failed", "diag": diag}), 404
        sub_status = sub.get("status")
        customer = customer or sub.get("customer")
        for it in ((sub.get("items") or {}).get("data") or []):
            prod = (it.get("price") or {}).get("product")
            prod_id = prod.get("id") if isinstance(prod, dict) else prod
            prod_nm = prod.get("name", "") if isinstance(prod, dict) else ""
            qty = int(it.get("quantity") or 100); break
    else:
        return jsonify({"error": "subscription_id or session_id required", "diag": diag}), 400

    if not email_ov and customer:
        cu = _stripe_get("/v1/customers/%s" % customer, stripe_key) or {}
        email_ov = ((cu.get("email") or "").strip().lower()) or None

    is_usage = (prod_id == _USAGE_PRODUCT_ID) or ("Usage-Based" in str(prod_nm))
    tier = _qty_to_tier(qty or 100)

    _ensure_metered_keys()
    existing = None
    try:
        with _conn() as c, c.cursor() as cur:
            if customer:
                cur.execute("SELECT api_key FROM metered_keys WHERE stripe_customer_id=%s LIMIT 1", (customer,))
                r = cur.fetchone()
                if r:
                    existing = r[0]
    except Exception:
        pass

    base = {"diag": diag, "subscription_id": sub_id, "session_id": sess_id,
            "customer": customer, "subscription_status": sub_status,
            "product_id": prod_id, "product_name": prod_nm,
            "is_usage_product": is_usage, "qty": qty, "tier": tier,
            "email": email_ov,
            "existing_key_short": ((existing or "")[:12] + "…") if existing else None,
            "webhook_ran_already": bool(existing)}

    if not is_usage:
        base["action"] = "skip_not_usage_product"
        return jsonify(base), 200
    if dry_run:
        base["action"] = "dry_run — re-POST with dry_run:false to issue + email the key"
        return jsonify(base), 200

    # --- 3. issue (idempotent) + email (capture real send status) -------
    api_key = existing
    try:
        with _conn() as c, c.cursor() as cur:
            if not api_key:
                api_key = "dch_live_" + _secrets.token_hex(16)
                cur.execute("""INSERT INTO mcp_dev_keys (api_key, developer_id, email, tier, status, metadata)
                    VALUES (%s,%s,%s,%s,'active',%s::jsonb) ON CONFLICT (api_key) DO NOTHING""",
                    (api_key, "dev_" + _secrets.token_hex(8), (email_ov or None), tier,
                     json.dumps({"source": "recover_usage_key", "qty": qty,
                                 "stripe_customer_id": customer})))
                if customer:
                    cur.execute("""INSERT INTO metered_keys (api_key, stripe_customer_id, subscription_id, active, last_reported_at, linked_at)
                        VALUES (%s,%s,%s,TRUE,NOW(),NOW())
                        ON CONFLICT (api_key) DO UPDATE SET active=TRUE, stripe_customer_id=EXCLUDED.stripe_customer_id""",
                        (api_key, customer, sub_id))
            c.commit()
    except Exception as e:
        base["action"] = "db_error"; base["error"] = str(e)[:200]
        return jsonify(base), 500

    email_ok, email_detail = (False, "no_email_address")
    if email_ov and api_key:
        try:
            from routes.redeem_routes import _p99_send_email
            email_ok, email_detail = _p99_send_email(email_ov, api_key, [])
        except Exception as _ee:
            email_ok, email_detail = False, f"{type(_ee).__name__}: {str(_ee)[:160]}"

    base["action"] = "issued"
    base["api_key_short"] = (api_key or "")[:12] + "…"
    base["reused_existing"] = bool(existing)
    base["email_ok"] = email_ok
    base["email_detail"] = str(email_detail)[:300]
    return jsonify(base), 200


@stripe_metered_bp.route("/report-to-stripe", methods=["POST"])
def report_to_stripe():
    """Admin/cron: report each metered customer's API-call usage since their
    last report to the Stripe Meter (new billing/meter_events API). Bills from
    mcp_call_log (the real per-key call source). DRY-RUN by default — goes live
    only with a Stripe key set AND ?dry_run=0. Safe no-op with no metered keys.
    """
    admin_key = (request.headers.get("X-Admin-Key", "")
                 or request.headers.get("X-Internal-Key", ""))
    expected = (os.environ.get("DCHUB_ADMIN_KEY", "")
                or os.environ.get("DCHUB_INTERNAL_KEY", ""))
    if not expected or admin_key != expected:
        return jsonify({"error": "admin auth required"}), 403
    stripe_key = (os.environ.get("STRIPE_API_KEY", "")
                  or os.environ.get("STRIPE_SECRET_KEY", ""))
    dry_run = (not stripe_key) or request.args.get("dry_run", "1") == "1"
    _ensure_metered_keys()
    results = []
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT api_key, stripe_customer_id, last_reported_at "
                        "FROM metered_keys WHERE active = TRUE")
            keys = [dict(r) for r in cur.fetchall()]
            for k in keys:
                cur.execute("""
                    SELECT COUNT(*) AS n FROM mcp_call_log
                     WHERE api_key = %s AND timestamp > %s AND timestamp < NOW()
                """, (k["api_key"], k["last_reported_at"]))
                n = int((cur.fetchone() or {}).get("n") or 0)
                rec = {"api_key_short": k["api_key"][:10] + "...",
                       "customer": k["stripe_customer_id"], "calls": n}
                if n <= 0:
                    rec["status"] = "no_new_usage"; results.append(rec); continue
                if dry_run:
                    rec["status"] = "dry_run"; results.append(rec); continue
                ok, detail = _stripe_meter_event(stripe_key, n, k["stripe_customer_id"])
                rec["status"] = "reported" if ok else "error"
                rec["detail"] = detail
                if ok:
                    cur.execute("UPDATE metered_keys SET last_reported_at = NOW() "
                                "WHERE api_key = %s", (k["api_key"],))
                    c.commit()
                results.append(rec)
    except Exception as e:
        return jsonify({"error": str(e)[:200], "results": results}), 500
    return jsonify({
        "mode":         "dry_run" if dry_run else "live",
        "meter_event":  _METER_EVENT,
        "metered_price": _METERED_PRICE_ID,
        "metered_keys": len(results),
        "results":      results[:25],
    }), 200


@stripe_metered_bp.route("/health", methods=["GET"])
def billing_health():
    table_ok = _ensure_table()
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM api_usage_meter")
            n_records = int(cur.fetchone()[0])
    except Exception:
        n_records = -1

    # r61-conv: metered go-live readiness — lets the owner verify the two
    # confirms (STRIPE_SECRET_KEY present + the meter event_name the code
    # sends) without exposing any secret value.
    metered_linked = -1
    try:
        _ensure_metered_keys()
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM metered_keys WHERE active = TRUE")
            metered_linked = int(cur.fetchone()[0])
    except Exception:
        pass
    _stripe_present = bool(os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_API_KEY"))

    return jsonify({
        "ok": True,
        "blueprint": "stripe_metered_bp",
        "version": "round-61-meters-api",
        "table_ok": table_ok,
        "total_usage_records": n_records,
        "stripe_configured": {
            "api_key":    bool(os.environ.get("STRIPE_API_KEY")),
            "secret_key": bool(os.environ.get("STRIPE_SECRET_KEY")),
            "price_id":   bool(os.environ.get("STRIPE_OVERAGE_PRICE_ID")),
        },
        "metered_readiness": {
            "meter_event":         _METER_EVENT,
            "metered_price":       _METERED_PRICE_ID,
            "stripe_key_present":  _stripe_present,
            "metered_keys_linked": metered_linked,
            "live_ready":          _stripe_present,
        },
        "tier_limits": TIER_LIMITS,
        "endpoints": [
            "POST /api/v1/billing/track-usage    — internal middleware",
            "GET  /api/v1/billing/usage          — caller's MTD usage",
            "POST /api/v1/billing/report-to-stripe — admin/cron batch report",
        ],
    }), 200
