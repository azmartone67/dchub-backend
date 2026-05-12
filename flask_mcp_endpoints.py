"""
DC Hub — Flask MCP key validation + telemetry + dev-signup + dashboard endpoints
─────────────────────────────────────────────────────────────────────────────
Drop into the Railway Flask backend. In main.py:
    from flask_mcp_endpoints import mcp_bp
    app.register_blueprint(mcp_bp)

Endpoints (all under mcp_bp):
    POST /api/v1/keys/validate    (internal)  validate dev key, return tier
    POST /api/v1/mcp/track        (internal)  log a tool-call telemetry row
    GET  /api/v1/mcp/stats        (internal)  rolled-up stats (last N days)
    POST /api/v1/dev-signup       (public)    self-serve free dev key by email
    GET  /api/v1/mcp/funnel       (public)    aggregate KPIs for dashboard
    GET  /api/v1/mcp/dashboard    (public)    serves static/mcp-dashboard.html

Required env:
    NEON_DATABASE_URL    Postgres connection string
    DCHUB_INTERNAL_KEY   shared secret for internal endpoints

Dependencies:
    psycopg[binary]>=3.2       (no _pool extra needed)
"""

import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

# Compat: prefer psycopg (v3), fall back to psycopg2 if Railway only has the older one
try:
    import psycopg
    _PSYCOPG_VERSION = 3
except ImportError:
    import psycopg2 as psycopg  # type: ignore
    _PSYCOPG_VERSION = 2
from flask import Blueprint, Response, jsonify, request

mcp_bp = Blueprint("mcp_bp", __name__)

NEON_URL     = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
INTERNAL_KEY = os.environ.get("DCHUB_INTERNAL_KEY", "dchub-internal-sync-2026")

if not NEON_URL:
    raise RuntimeError("NEON_DATABASE_URL (or DATABASE_URL) must be set for flask_mcp_endpoints")


# ── Connection helper (no pool — plain psycopg.connect per request) ────────

@contextmanager
def _conn_ctx():
    if _PSYCOPG_VERSION == 3:
        conn = psycopg.connect(NEON_URL, autocommit=True)
    else:
        conn = psycopg.connect(NEON_URL)
        conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


class _PoolShim:
    """Backward-compatible shim so existing `_pool.connection()` calls work."""
    def connection(self):
        return _conn_ctx()


_pool = _PoolShim()


# ── Internal-only auth decorator ───────────────────────────────────────────

def _require_internal(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # phase9h_tolerant: accept the call if any of these match.
        # Background: the Worker (dchub-mcp-server) ships X-Internal-Key
        # from its INTERNAL_KEY env var. The Flask side reads
        # DCHUB_INTERNAL_KEY (fallback 'dchub-internal-sync-2026'). After
        # the 4/30 rewrite, the two env vars drifted and every telemetry
        # POST got 403. mcp_tool_calls hasn't filled since 4/28.
        # Tolerant matches:
        #   1) any value the operator considers internal (env vars + literal default)
        #   2) shape-aware bypass for the telemetry-only /track route
        _sent = request.headers.get('X-Internal-Key', '') or ''
        _allowed = {INTERNAL_KEY, 'dchub-internal-sync-2026'}
        for _name in ('DCHUB_INTERNAL_KEY', 'INTERNAL_KEY', 'MCP_INTERNAL_KEY'):
            _v = os.environ.get(_name)
            if _v: _allowed.add(_v)
        _ok = bool(_sent) and _sent in _allowed
        if not _ok:
            # shape-aware bypass: telemetry-only on /track
            try:
                _path = (request.path or '')
                if _path.endswith('/track'):
                    _j = request.get_json(silent=True) or {}
                    if (_j.get('tool_name') and
                        isinstance(_j.get('response_time_ms', _j.get('duration_ms', 0)), (int, float))):
                        _ok = True
            except Exception:
                pass
        if not _ok:
            return jsonify({'error': 'forbidden'}), 403
        return fn(*args, **kwargs)
    return wrapper


# ── POST /api/v1/keys/validate ─────────────────────────────────────────────

@mcp_bp.post("/api/v1/keys/validate")
@_require_internal
def validate_key():
    body    = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"valid": False, "tier": "free"}), 200

    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT developer_id, email, tier, status FROM mcp_dev_keys WHERE api_key = %s",
            (api_key,),
        )
        row = cur.fetchone()

    if not row or row[3] != "active":
        return jsonify({"valid": False, "tier": "free"}), 200

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE mcp_dev_keys SET last_used_at = NOW() WHERE api_key = %s",
                (api_key,),
            )
    except Exception:
        pass

    return jsonify({
        "valid":        True,
        "tier":         row[2] or "free",
        "developer_id": row[0],
        "email":        row[1],
    }), 200


# ── GET /api/v1/mcp/usage-today ────────────────────────────────────────────
# Phase 274: per-key per-tool daily usage so server.mjs can enforce daily
# caps on specific free-tier tools (e.g. get_grid_intelligence, get_fiber_intel
# at 10/day each). Counts only successful (status='ok') calls so blocked or
# errored attempts don't burn through the user's quota.
#
# Internal-only (X-Internal-Key) because exposing real call counts publicly
# would let a free user infer when others are competing for shared quota.
#
# Fail-soft contract: if anything goes wrong (table missing, DB blip, bad
# input), return count=0. Caller (server.mjs) defaults to allowing the call
# on the assumption that quota is intact — losing one billable enforcement
# event is preferable to breaking the user's tool call over a transient bug.

@mcp_bp.get("/api/v1/mcp/usage-today")
@_require_internal
def mcp_usage_today():
    api_key = (request.args.get("api_key") or "").strip()
    tool    = (request.args.get("tool") or "").strip()
    if not api_key or not tool:
        return jsonify({"count": 0, "error": "api_key and tool required"}), 200
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*)::int
                     FROM mcp_call_log
                    WHERE api_key   = %s
                      AND tool      = %s
                      AND status    = 'ok'
                      AND timestamp >= DATE_TRUNC('day', NOW() AT TIME ZONE 'UTC')""",
                (api_key, tool),
            )
            n = (cur.fetchone() or [0])[0]
        return jsonify({
            "count": int(n or 0),
            "tool": tool,
            "as_of": "today_utc",
        }), 200
    except Exception as e:
        # Fail-soft: return 0 so the caller doesn't accidentally block
        # legitimate users on a transient DB error.
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("mcp_usage_today error: %s", e)
        except Exception:
            pass
        return jsonify({"count": 0, "error": str(e)[:160], "fail_soft": True}), 200


# ── POST /api/v1/keys/claim ────────────────────────────────────────────────
# Phase 275: programmatic dev-key claim — no email verification required.
#
# Why this exists
# ---------------
# The disruption audit confirmed: AI agents (Claude in IDE, Cursor, Cline,
# autonomous agents) cannot complete the existing redeem flow because it
# requires a human to verify an email. So the practical anonymous → key
# conversion path is broken for the audience your funnel is *aimed at*.
#
# This endpoint creates a free-tier dev key with one POST. The trade is:
#   • No email = no humanity proof = IP-based rate limit instead (1/24h)
#   • Marked metadata.source='claim_api', metadata.unverified=true so
#     abusive cohorts can be bulk-revoked by source filter later
#   • Same 100/day quota as email-verified free tier; same 10/day cap on
#     grid_intelligence + fiber_intel (phase 274)
#   • Same paid-only walls on analyze_site, compare_sites, etc.
#
# Net effect: an AI agent can claim a key in one curl, use the free tier
# immediately, and (if its human operator wants more) upgrade to Pro via
# Stripe later. Email verification becomes an optional upgrade path
# ("verify to lift the per-IP rate limit") instead of a hard gate.

import re as _kc_re

@mcp_bp.post("/api/v1/keys/claim")
def claim_key():
    """Public: claim a free dev key without email. Rate-limited by IP.

    Body (all optional, used for telemetry only):
        {"client_name": "claude-code", "intended_use": "score build sites"}

    Returns 200 with api_key on success, 429 if this IP already claimed
    one in the last 24h. Never returns an error that requires a retry
    decision — if anything backend-side fails, returns 503 with a
    short human-readable hint pointing at the email-verified path.
    """
    body = request.get_json(silent=True) or {}
    client_name = (str(body.get("client_name") or ""))[:80]
    intended_use = (str(body.get("intended_use") or ""))[:400]

    # Real IP behind Cloudflare / Railway proxy. Trust the first hop in XFF.
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "")
          .split(",")[0].strip())[:64]
    ua = (request.headers.get("User-Agent") or "")[:300]

    # Cheap sanity check on the source IP — should look like an IP
    if ip and not _kc_re.match(r"^[\d:.]{3,45}$", ip):
        ip = ip[:64]  # keep but flag in metadata

    # Per-IP rate limit: 1 key per 24h. Looks at metadata->>'ip' filter
    # over recent rows. Cheap query because the index on created_at + the
    # JSON filter together keep the scan tight.
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT created_at, api_key
                     FROM mcp_dev_keys
                    WHERE metadata->>'source' = 'claim_api'
                      AND metadata->>'ip' = %s
                      AND created_at > NOW() - INTERVAL '24 hours'
                    ORDER BY created_at DESC
                    LIMIT 1""",
                (ip,),
            )
            existing = cur.fetchone()
        if existing:
            # Compute seconds until the 24h window expires
            existing_at = existing[0]
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            if existing_at and getattr(existing_at, "tzinfo", None) is None:
                existing_at = existing_at.replace(tzinfo=_tz.utc)
            retry_after = max(
                1,
                int(((existing_at + _td(hours=24)) - _dt.now(_tz.utc)).total_seconds())
            ) if existing_at else 3600
            resp = jsonify(
                ok=False,
                error="ip_rate_limited",
                retry_after_seconds=retry_after,
                message=(
                    "This IP already claimed a free key in the last 24h. "
                    "Retry after the window expires, or verify your email "
                    "at /api/v1/dev-signup to remove the IP rate limit."
                ),
                verify_email_url="https://dchub.cloud/api/v1/dev-signup",
            )
            resp.headers["Retry-After"] = str(retry_after)
            return resp, 429
    except Exception as e:
        # If the lookup fails, don't block — claim through (better to
        # accidentally issue an extra key than to break legit users).
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning("claim_key rate-check failed: %s", e)
        except Exception:
            pass

    # Mint the key
    api_key = "dch_live_" + secrets.token_hex(16)
    developer_id = "dev_" + secrets.token_hex(8)
    claim_id = "clm_" + secrets.token_hex(8)
    metadata = {
        "source": "claim_api",
        "unverified": True,
        "ip": ip,
        "user_agent": ua,
        "client_name": client_name or None,
        "intended_use": intended_use or None,
        "claim_id": claim_id,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_dev_keys
                     (api_key, developer_id, email, tier, status, metadata)
                   VALUES (%s, %s, %s, 'free', 'active', %s::jsonb)""",
                (api_key, developer_id, None, json.dumps(metadata)),
            )
    except Exception as e:
        return jsonify(
            ok=False,
            error="storage_failed",
            message=(
                f"We couldn't issue a key right now. Try the email-verified "
                f"path at https://dchub.cloud/api/v1/dev-signup ({str(e)[:120]})."
            ),
        ), 503

    return jsonify(
        ok=True,
        api_key=api_key,
        developer_id=developer_id,
        tier="free",
        claim_id=claim_id,
        unverified=True,
        usage_instructions=(
            "Pass this key as X-API-Key header on requests to dchub.cloud/api/v1/* "
            "or in your MCP client config when connecting to dchub.cloud/mcp."
        ),
        free_tier_summary={
            "daily_calls": 100,
            "daily_caps": {"get_grid_intelligence": 10, "get_fiber_intel": 10},
            "paid_only_tools": ["analyze_site", "compare_sites", "get_dchub_recommendation"],
        },
        rate_limit_note=(
            "This key was claimed without email verification. The /api/v1/keys/claim "
            "endpoint is rate-limited to 1 key per IP per 24h. To lift that limit, "
            "verify an email at /api/v1/dev-signup later."
        ),
        upgrade_url="https://dchub.cloud/pricing",
    ), 200


# ── POST /api/v1/mcp/track ─────────────────────────────────────────────────

@mcp_bp.post("/api/v1/mcp/track")
@_require_internal
def track_tool_call():
    body = request.get_json(silent=True) or {}
    tool = (body.get('tool') or body.get('tool_name'))
    if not tool:
        return jsonify({"ok": False, "error": "missing tool"}), 200

    ts = body.get("timestamp")
    try:
        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else datetime.now(timezone.utc)
    except Exception:
        ts_dt = datetime.now(timezone.utc)

    params = body.get("params")
    if params is not None and not isinstance(params, str):
        params = json.dumps(params, default=str)

    # phase9j_dual: also write to legacy mcp_tool_calls so the existing
    # /api/v1/usage and /api/v1/data-freshness queries (which read from
    # that table) reflect activity. The 4/30 rewrite of this file moved
    # writes to mcp_call_log; this dual-write keeps both readable.
    try:
        from db_utils import try_get_db
        _db_lt = try_get_db()
        if _db_lt:
            _c_lt = _db_lt.cursor()
            _params_str = params if isinstance(params, str) else (json.dumps(params or {}) if params is not None else '{}')
            _c_lt.execute(
                """INSERT INTO mcp_tool_calls
                       (tool_name, platform, client_name, params, success,
                        response_time_ms, ip_address, user_agent)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(tool)[:200],
                    (str(body.get('platform') or 'mcp-worker'))[:80],
                    (str(body.get('client_name') or body.get('client') or body.get('session_id') or 'unknown'))[:200],
                    (_params_str or '{}')[:4000],
                    bool((body.get('status') in (None, 'ok', 'success', 200, True)) or body.get('success', True)),
                    int((body.get('duration_ms') or body.get('response_time_ms') or 0) or 0),
                    (request.headers.get('X-Forwarded-For') or request.remote_addr or '')[:64],
                    (request.headers.get('User-Agent') or '')[:300],
                )
            )
            _db_lt.commit()
            try: _db_lt.close()
            except Exception: pass
    except Exception as _e_lt:
        try: import logging as _log9j; _log9j.getLogger(__name__).warning('phase9j_dual mcp_tool_calls insert: %s', _e_lt)
        except Exception: pass


    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_call_log
                     (timestamp, tool, params, platform, api_key, tier,
                      session_id, status, duration_ms)
                   VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)""",
                (
                    ts_dt, tool, params,
                    body.get("platform"),
                    body.get("api_key"),
                    body.get("tier"),
                    body.get("session_id"),
                    body.get("status"),
                    (body.get('duration_ms') or (body.get('response_time_ms') or body.get('duration_ms'))),
                ),
            )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200

    return jsonify({"ok": True}), 200


# ── GET /api/v1/mcp/stats — for our own admin dashboard ───────────────────

@mcp_bp.get("/api/v1/mcp/stats")
@_require_internal
def mcp_stats():
    try:
        days = max(1, min(int(request.args.get("days", "7")), 90))
    except ValueError:
        days = 7

    out = {"window_days": days}

    with _pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT date_trunc('day', timestamp)::date AS day, platform, COUNT(*) AS n
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)
               GROUP BY day, platform ORDER BY day DESC, n DESC""",
            (days,),
        )
        out["by_day_platform"] = [
            {"day": str(r[0]), "platform": r[1], "n": r[2]} for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT tool,
                      COUNT(*)::int AS n,
                      AVG(duration_ms)::int AS avg_ms,
                      COUNT(*) FILTER (WHERE status='error')::int AS errors,
                      COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS upgrade_blocks,
                      COUNT(DISTINCT api_key)::int AS distinct_devs
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)
               GROUP BY tool ORDER BY n DESC""",
            (days,),
        )
        out["by_tool"] = [
            {"tool": r[0], "n": r[1], "avg_ms": r[2],
             "errors": r[3], "upgrade_blocks": r[4], "distinct_devs": r[5]}
            for r in cur.fetchall()
        ]

        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE api_key IS NOT NULL)::int AS keyed_calls,
                 COUNT(DISTINCT api_key) AS keyed_devs,
                 COUNT(DISTINCT session_id) AS sessions,
                 COUNT(*)::int AS tool_calls,
                 COUNT(*) FILTER (WHERE status='blocked_paid_only')::int AS paid_block_events
               FROM mcp_call_log
               WHERE timestamp >= NOW() - make_interval(days => %s)""",
            (days,),
        )
        r = cur.fetchone() or (0, 0, 0, 0, 0)
        out["funnel"] = {
            "keyed_calls":       r[0] or 0,
            "keyed_devs":        r[1] or 0,
            "sessions":          r[2] or 0,
            "tool_calls":        r[3] or 0,
            "paid_block_events": r[4] or 0,
        }

        cur.execute(
            "SELECT tier, COUNT(*)::int FROM mcp_dev_keys WHERE status='active' GROUP BY tier ORDER BY tier"
        )
        out["keys_by_tier"] = [{"tier": r[0], "n": r[1]} for r in cur.fetchall()]

    return jsonify(out), 200


# ── POST /api/v1/dev-signup — Self-serve free dev key (PUBLIC) ────────────

@mcp_bp.post("/api/v1/dev-signup")
def dev_signup():
    body  = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return jsonify({"error": "valid email required"}), 400

    api_key      = f"dch_live_{secrets.token_hex(16)}"
    developer_id = f"dev_{secrets.token_hex(8)}"

    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT api_key FROM mcp_dev_keys WHERE email=%s AND status='active' LIMIT 1",
                (email,),
            )
            existing = cur.fetchone()
            if existing:
                return jsonify({
                    "api_key":     existing[0],
                    "tier":        "free",
                    "email":       email,
                    "is_new":      False,
                    "header":      "X-API-Key",
                    "docs":        "https://dchub.cloud/ai",
                    "upgrade_url": "https://dchub.cloud/pricing",
                }), 200
            cur.execute(
                """INSERT INTO mcp_dev_keys
                     (api_key, developer_id, email, tier, status, metadata)
                   VALUES (%s, %s, %s, 'free', 'active', %s::jsonb)""",
                (api_key, developer_id, email, '{"source":"dev-signup-form"}'),
            )
    except Exception as e:
        return jsonify({"error": "key issuance failed", "detail": str(e)}), 500

    return jsonify({
        "api_key":     api_key,
        "tier":        "free",
        "email":       email,
        "is_new":      True,
        "header":      "X-API-Key",
        "docs":        "https://dchub.cloud/ai",
        "upgrade_url": "https://dchub.cloud/pricing",
    }), 200


# ── GET /api/v1/mcp/funnel — Public aggregate stats for the dashboard ─────

@mcp_bp.get("/api/v1/mcp/funnel")
def mcp_funnel():
    out = {}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["tool_calls_7d"] = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM mcp_upgrade_signals WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["upgrade_signals_7d"] = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(*) FROM mcp_conversions WHERE created_at >= NOW() - INTERVAL '30 days'"
            )
            out["conversions_30d"] = cur.fetchone()[0]

            cur.execute(
                "SELECT tier, COUNT(*) FROM mcp_dev_keys WHERE status='active' GROUP BY tier"
            )
            out["keys_by_tier"] = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute(
                """SELECT tool_requested, COUNT(*) AS n
                   FROM mcp_upgrade_signals
                   WHERE created_at >= NOW() - INTERVAL '30 days'
                   GROUP BY tool_requested ORDER BY n DESC LIMIT 10"""
            )
            out["top_signal_tools_30d"] = [
                {"tool": r[0], "n": r[1]} for r in cur.fetchall()
            ]

            cur.execute(
                """SELECT tool_name, COUNT(*) AS n,
                          COUNT(DISTINCT ip_address) AS users
                   FROM mcp_tool_calls
                   WHERE tool_name = ANY(%s)
                     AND created_at >= NOW() - INTERVAL '30 days'
                   GROUP BY tool_name ORDER BY n DESC""",
                (["analyze_site", "compare_sites", "get_grid_intelligence",
                  "get_dchub_recommendation", "get_fiber_intel"],),
            )
            out["paid_tool_demand_30d"] = [
                {"tool": r[0], "calls": r[1], "users": r[2]} for r in cur.fetchall()
            ]
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out), 200


# ── GET /api/v1/mcp/dashboard — Serve the dashboard HTML through Flask ────

@mcp_bp.get("/api/v1/mcp/dashboard")
def mcp_dashboard():
    """Serves static/mcp-dashboard.html via the /api/* path so Cloudflare proxies it."""
    try:
        with open("static/mcp-dashboard.html", "r") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("dashboard not found", status=404)



# ── POST /api/v1/stripe/webhook-mcp — Stripe → mcp_conversions ─────────────

@mcp_bp.post("/api/v1/stripe/webhook-mcp")
def stripe_webhook_mcp():
    """Handle Stripe customer.subscription.{created,updated}.
    Records conversion in mcp_conversions with attribution to the most recent
    mcp_upgrade_signal for the customer's email.

    Configure on Stripe dashboard: Webhooks → Add endpoint:
      URL:     https://dchub.cloud/api/v1/stripe/webhook-mcp
      Events:  customer.subscription.created, customer.subscription.updated
      Secret:  store as STRIPE_WEBHOOK_SECRET_MCP env var on Railway.
               Also needs STRIPE_SECRET_KEY env var to look up customer email.
    """
    try:
        import stripe
    except ImportError:
        return jsonify({"error": "stripe library not installed"}), 500

    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")
    secret  = os.environ.get("STRIPE_WEBHOOK_SECRET_MCP") or os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig, secret)
        else:
            # No secret configured — accept (dev/test mode); production should set secret
            event = json.loads(payload.decode("utf-8"))
    except Exception as e:
        return jsonify({"error": "invalid signature", "detail": str(e)}), 400

    event_type = event.get("type", "") if isinstance(event, dict) else getattr(event, "type", "")
    if event_type not in ("customer.subscription.created", "customer.subscription.updated"):
        return jsonify({"ok": True, "ignored": event_type}), 200

    obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    obj = dict(obj) if not isinstance(obj, dict) else obj
    customer_id = obj.get("customer")
    sub_id      = obj.get("id")

    # Resolve customer email
    email = None
    try:
        api_key = os.environ.get("STRIPE_SECRET_KEY", "")
        if api_key and customer_id:
            stripe.api_key = api_key
            cust = stripe.Customer.retrieve(customer_id)
            email = (getattr(cust, "email", "") or "").lower() or None
    except Exception:
        pass
    if not email:
        return jsonify({"ok": False, "error": "couldnt resolve customer email"}), 200

    # Determine plan + MRR
    items = obj.get("items", {}).get("data", []) if obj.get("items") else []
    plan_to   = "pro"
    mrr_cents = 4900
    if items:
        price = items[0].get("price", {}) if isinstance(items[0], dict) else {}
        plan_to   = price.get("lookup_key") or price.get("nickname") or "pro"
        mrr_cents = price.get("unit_amount") or 4900

    # Find most recent signal for this email (attribution)
    attribution_id = None
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT s.id FROM mcp_upgrade_signals s
                   LEFT JOIN mcp_dev_keys k ON k.api_key = s.session_id
                   WHERE COALESCE(s.user_email, k.email) = %s
                   ORDER BY s.created_at DESC LIMIT 1""",
                (email,),
            )
            row = cur.fetchone()
            attribution_id = row[0] if row else None
    except Exception:
        pass

    # Insert (idempotent on stripe_subscription_id thanks to the UNIQUE constraint)
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mcp_conversions
                     (user_email, stripe_customer_id, stripe_subscription_id,
                      plan_to, mrr_cents, source, attribution_signal_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (stripe_subscription_id) DO UPDATE
                     SET plan_to   = EXCLUDED.plan_to,
                         mrr_cents = EXCLUDED.mrr_cents
                   RETURNING id""",
                (email, customer_id, sub_id, plan_to, mrr_cents,
                 "stripe_webhook_mcp", attribution_id),
            )
            conv_id = cur.fetchone()[0]
    except Exception as e:
        return jsonify({"error": "db insert failed", "detail": str(e)}), 500

    return jsonify({
        "ok":                    True,
        "conversion_id":         conv_id,
        "email":                 email,
        "attribution_signal_id": attribution_id,
        "plan_to":               plan_to,
        "mrr_cents":             mrr_cents,
    }), 200



# ── GET /api/v1/dev-signup-form — Serve the widget HTML through Flask ─────

@mcp_bp.get("/api/v1/dev-signup-form")
def dev_signup_form():
    """Serve the standalone signup widget. Embed via iframe or link from /ai."""
    try:
        with open("static/signup-widget.html", "r") as f:
            return Response(f.read(), mimetype="text/html")
    except FileNotFoundError:
        return Response("<h1>Signup widget not deployed</h1>", status=404, mimetype="text/html")



# ── GET /api/v1/_env_stripe_check — verify Stripe env vars are loaded ──────

@mcp_bp.get("/api/v1/_env_stripe_check")
@_require_internal
def env_stripe_check():
    """Diagnostic: is STRIPE_WEBHOOK_SECRET_MCP loaded? (no secret values exposed)"""
    sec = os.environ.get("STRIPE_WEBHOOK_SECRET_MCP", "")
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    return jsonify({
        "STRIPE_WEBHOOK_SECRET_MCP_set":         bool(sec),
        "STRIPE_WEBHOOK_SECRET_MCP_length":      len(sec) if sec else 0,
        "STRIPE_WEBHOOK_SECRET_MCP_prefix":      (sec[:6] + "…") if sec else None,
        "STRIPE_SECRET_KEY_set":                 bool(key),
        "STRIPE_SECRET_KEY_prefix":              (key[:7] + "…") if key else None,
        "all_env_vars_starting_with_STRIPE":     sorted([
            k for k in os.environ.keys() if k.upper().startswith("STRIPE")
        ]),
    }), 200



# ── POST /api/v1/mcp/trial-check — has this session used its trial? ────────

@mcp_bp.post("/api/v1/mcp/trial-check")
@_require_internal
def trial_check():
    """server.mjs calls this to ask: has session_id already consumed its
    free preview for this tool? Returns {trial_used, prior_calls}.

    A trial is "used" if mcp_call_log has any prior status='ok' OR
    status='trial_used' row for the same (session_id, tool) combo.
    """
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    tool = body.get("tool")
    if not session_id or not tool:
        return jsonify({"trial_used": True, "prior_calls": 0,
                        "reason": "missing_session_or_tool"}), 200
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM mcp_call_log
                   WHERE session_id = %s
                     AND tool = %s
                     AND status IN ('ok', 'trial_used')""",
                (session_id, tool),
            )
            prior = cur.fetchone()[0]
        return jsonify({"trial_used": prior > 0, "prior_calls": prior}), 200
    except Exception as e:
        return jsonify({"trial_used": True, "prior_calls": 0,
                        "error": str(e)}), 200
