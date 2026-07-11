"""My Agent (2026-07-11) — self-serve "what did my agent do" dashboard.

Why: the MCP onboarding funnel dies at email-bind (2,131 minted keys →
445 activated → ~6 bound, 0.28%). Operators have no reason to come back
to the site after connecting — the agent does everything in-chat. This
page gives the OPERATOR a reason: paste your key, see your agent's real
query log, tool mix, and gate hits. The bind CTA sits right where an
unbound trial holder is looking at their own usage (the retention hook),
and blocked-call counts sit next to the upgrade link (the conversion
hook). Suggested by Gemini's /connect copy review ("are you planning a
dashboard where users can see their own agent's query logs?").

  GET /api/v1/my/usage   key via X-API-Key header (or Authorization:
                         Bearer). Returns ONLY that key's own usage:
                         plan, bind status, calls today/7d/total, first/
                         last call, top tools 30d, gate hits, recent 50
                         calls. Key never appears in a URL; response is
                         Cache-Control: private, no-store (CF Rule-3
                         cache-leak lesson — never let an edge cache a
                         keyed response).
  GET /my-agent          the page (static/my-agent.html), same pattern
                         as /connect.

Key stores consulted (union — see flask_mcp_endpoints.py validate_key,
which this mirrors read-only; that endpoint is internal-gated so the
page can't call it): auto_trial_keys (dch_trial_ mints), mcp_dev_keys,
api_keys (key_hash = sha256(key) OR the raw-string convention partner/
admin keys use), metered_keys (badge only).
"""

from __future__ import annotations

import os
import hashlib
import datetime
from flask import Blueprint, jsonify, request, send_from_directory

my_agent_bp = Blueprint("my_agent", __name__)


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


# Mirrors flask_mcp_endpoints._node_tier_max / _ENT_PLANS / _PAID_PLANS —
# duplicated (10 lines) rather than imported so this module never pulls in
# that file's module-level connection pool.
_ENT_PLANS = {"enterprise", "research_seed", "admin"}
_PAID_PLANS = {"paid", "pro", "founding", "team", "metered"}


def _tier_norm(plans):
    norm = set()
    for p in plans:
        p = (p or "").strip().lower()
        if p in _ENT_PLANS:
            norm.add("enterprise")
        elif p in _PAID_PLANS:
            norm.add("paid")
        elif p:
            norm.add(p)
    if "enterprise" in norm:
        return "enterprise"
    if "paid" in norm:
        return "paid"
    for t in ("identified", "starter", "developer"):
        if t in norm:
            return t
    return "free"


def _mask_email(e):
    if not e or "@" not in e:
        return None
    name, dom = e.split("@", 1)
    return (name[:2] + "…@" + dom) if len(name) > 2 else ("…@" + dom)


def _resolve_key(cur, api_key):
    """Look the key up across every key store. Returns a dict (never the
    raw key or a full email) or None if no store knows it."""
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. auto-trial mints (dch_trial_…) — the claim_free_key path
    cur.execute(
        """SELECT minted_at, expires_at, signed_up_email, operator_email,
                  COALESCE(call_count, 0), COALESCE(daily_count, 0), daily_date
             FROM auto_trial_keys WHERE api_key = %s""",
        (api_key,),
    )
    r = cur.fetchone()
    if r:
        bound = bool(r[2]) or bool(r[3])
        try:
            from routes.auto_trial import (TRIAL_DAILY_CALLS,
                                           TRIAL_DAILY_UNBOUND,
                                           TRIAL_FREE_CALLS_UNBOUND)
        except Exception:
            TRIAL_DAILY_CALLS, TRIAL_DAILY_UNBOUND, TRIAL_FREE_CALLS_UNBOUND = 50, 15, 10
        used_today = r[5] if r[6] == now.date() else 0
        return {
            "source": "free_key",
            "plan": "free",
            "email_bound": bound,
            "email_masked": _mask_email(r[2] or r[3]),
            "created_at": r[0].isoformat() if r[0] else None,
            "expired": bool(r[1] and r[1] < now),
            "daily_limit": TRIAL_DAILY_CALLS if bound else TRIAL_DAILY_UNBOUND,
            "calls_today_gate": used_today,
            "bind_gate_calls": None if bound else TRIAL_FREE_CALLS_UNBOUND,
            "bind_gate_hit": (not bound) and int(r[4] or 0) >= TRIAL_FREE_CALLS_UNBOUND,
        }

    # 2. developer keys
    cur.execute(
        "SELECT tier, status, email, created_at FROM mcp_dev_keys WHERE api_key = %s",
        (api_key,),
    )
    r = cur.fetchone()
    if r:
        return {
            "source": "developer_key",
            "plan": _tier_norm([r[0]]),
            "email_bound": bool(r[2]),
            "email_masked": _mask_email(r[2]),
            "created_at": str(r[3]) if r[3] else None,
            "expired": (r[1] or "").lower() not in ("active", ""),
        }

    # 3. dashboard/web-signup keys — BOTH key_hash conventions (sha256 for
    # standard keys, raw string for partner/admin keys), and BOTH active
    # columns (is_active is INTEGER, is_active_bool is BOOLEAN — comparing
    # the int column to TRUE throws, the validate_key lesson).
    kh = hashlib.sha256(api_key.encode()).hexdigest()
    cur.execute(
        """SELECT plan, rate_limit_tier, COALESCE(calls_today, 0),
                  COALESCE(calls_total, 0), created_at, user_id,
                  COALESCE(is_active, 1), COALESCE(is_active_bool, TRUE)
             FROM api_keys WHERE key_hash IN (%s, %s) LIMIT 1""",
        (kh, api_key),
    )
    r = cur.fetchone()
    if r:
        plans = [r[0], r[1]]
        email = None
        if r[5]:
            try:
                cur.execute("SELECT plan, email FROM users WHERE id = %s", (str(r[5]),))
                u = cur.fetchone()
                if u:
                    plans.append(u[0])  # Stripe sets users.plan, NOT the key row
                    email = u[1]
            except Exception:
                pass
        return {
            "source": "account_key",
            "plan": _tier_norm(plans),
            "email_bound": bool(email),
            "email_masked": _mask_email(email),
            "created_at": str(r[4]) if r[4] else None,
            "expired": not (int(r[6] or 0) and bool(r[7])),
            "calls_today_gate": int(r[2]),
            "calls_total_gate": int(r[3]),
        }

    # 4. metered (usage-based billing) keys
    cur.execute("SELECT active FROM metered_keys WHERE api_key = %s", (api_key,))
    r = cur.fetchone()
    if r:
        return {"source": "metered_key", "plan": "paid", "email_bound": True,
                "expired": not bool(r[0])}

    return None


# Statuses that mean "your call hit a gate" — the honest upgrade signal.
_GATE_STATUSES = ("blocked_paid_only", "paywall_block", "rate_limited",
                  "anon_daily_cap", "mpp_challenge", "daily_cap",
                  "daily_cap_unbound", "bind_email_required")


@my_agent_bp.get("/api/v1/my/usage")
def my_usage():
    api_key = (request.headers.get("X-API-Key")
               or (request.headers.get("Authorization") or "").replace("Bearer ", "")).strip()
    if not api_key or len(api_key) < 8:
        return _nostore(jsonify({
            "ok": False, "error": "missing_key",
            "hint": ("Send your DC Hub key in the X-API-Key header. No key yet? "
                     "Tell your connected agent: \"call claim_free_key and save the key\". "
                     "Lost it? Ask your agent to call recover_my_key with your email."),
        }), 401)

    c = _conn()
    if c is None:
        return _nostore(jsonify({"ok": False, "error": "db_unavailable"}), 503)
    try:
        with c.cursor() as cur:
            ident = _resolve_key(cur, api_key)
            if ident is None:
                return _nostore(jsonify({
                    "ok": False, "error": "unknown_key",
                    "hint": ("This key isn't in any DC Hub key store. Keys look like "
                             "dch_trial_… or dchub_live_…. Mint a fresh one by telling "
                             "your connected agent: \"call claim_free_key and save the "
                             "key\" — or recover a bound key with recover_my_key."),
                }), 404)

            # ── usage aggregates (single indexed scan each; idx_mcp_log_apikey) ──
            cur.execute(
                """SELECT COUNT(*), MIN(timestamp), MAX(timestamp),
                          COUNT(*) FILTER (WHERE timestamp >= date_trunc('day', now())),
                          COUNT(*) FILTER (WHERE timestamp >= now() - interval '7 days'),
                          COUNT(*) FILTER (WHERE timestamp >= now() - interval '7 days'
                                             AND status = ANY(%s)),
                          COUNT(DISTINCT timestamp::date)
                             FILTER (WHERE timestamp >= now() - interval '30 days')
                     FROM mcp_call_log WHERE api_key = %s""",
                (list(_GATE_STATUSES), api_key),
            )
            total, first, last, today, d7, gated7, days30 = cur.fetchone()

            cur.execute(
                """SELECT tool, COUNT(*) FROM mcp_call_log
                    WHERE api_key = %s AND tool IS NOT NULL AND tool != ''
                      AND timestamp >= now() - interval '30 days'
                    GROUP BY tool ORDER BY 2 DESC LIMIT 8""",
                (api_key,),
            )
            top_tools = [{"tool": t, "calls": int(n)} for t, n in cur.fetchall()]

            cur.execute(
                """SELECT timestamp, tool, status, duration_ms, platform
                     FROM mcp_call_log WHERE api_key = %s
                    ORDER BY id DESC LIMIT 50""",
                (api_key,),
            )
            recent = [{
                "ts": r[0].isoformat() if r[0] else None,
                "tool": r[1],
                "status": r[2],
                "gated": r[2] in _GATE_STATUSES,
                "ms": r[3],
                "platform": r[4],
            } for r in cur.fetchall()]

        return _nostore(jsonify({
            "ok": True,
            "key_masked": api_key[:12] + "…",
            "identity": ident,
            "usage": {
                "calls_total": int(total or 0),
                "calls_today": int(today or 0),
                "calls_7d": int(d7 or 0),
                "gated_7d": int(gated7 or 0),
                "active_days_30d": int(days30 or 0),
                "first_call": first.isoformat() if first else None,
                "last_call": last.isoformat() if last else None,
                "top_tools_30d": top_tools,
                "recent": recent,
            },
        }), 200)
    except Exception as e:
        return _nostore(jsonify({"ok": False, "error": str(e)[:200]}), 500)
    finally:
        try:
            c.close()
        except Exception:
            pass


def _nostore(resp, code):
    """Keyed responses must NEVER be edge-cached (CF Rule-3 leak lesson)."""
    resp.headers["Cache-Control"] = "private, no-store"
    return resp, code


@my_agent_bp.get("/my-agent")
def my_agent_page():
    return send_from_directory("static", "my-agent.html")
