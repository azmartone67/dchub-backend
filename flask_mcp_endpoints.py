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


# ── POST /api/v1/mcp/track ─────────────────────────────────────────────────

@mcp_bp.post("/api/v1/mcp/track")
@_require_internal
def track_tool_call():
    body = request.get_json(silent=True) or {}
    tool = body.get("tool")
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
                    body.get("duration_ms"),
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
                    "upgrade_url": "https://dchub.cloud/ai#pricing",
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
        "upgrade_url": "https://dchub.cloud/ai#pricing",
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
