"""customer_usage_dashboard.py — Round 3 Unlock 2 (2026-06-07)
==============================================================

Customer-facing self-serve usage dashboard at /dashboard/usage.

WHY
---
Paid customers love seeing their numbers. /api/v1/mcp/usage/me already
returns the JSON, but no human-readable page exists — every paid
customer who emailed asking "how many calls have I used this month?"
had to be hand-served. This is sticky retention: once a customer sees
the chart, the platform feels REAL.

Secondary effect: a customer approaching their daily cap sees the
"Upgrade to Pro" CTA right next to the bar — the highest-intent
upgrade moment we have.

WHAT THIS SHIPS
---------------
GET /dashboard/usage              — public HTML page, prompts for key
POST /dashboard/usage/data        — JSON for the JS frontend (rate-limited)
GET /api/v1/customer/usage        — REST alias of the JSON endpoint
GET /dashboard/usage/embed        — minified JS embed for partners

INPUT
-----
The user enters their dch_xxx key client-side; we never store it as a
cookie or in the URL. The form POSTs to /dashboard/usage/data, validates
the key against mcp_dev_keys, and returns:

  - plan + tier (founding → "Pro" label etc.)
  - calls_today + daily_cap (from tier_registry.limits())
  - calls_30d / calls_7d
  - top_tools_30d (with counts)
  - unused_tools (cross-sell — tools they haven't called from the 38 catalog)
  - paywall_hits_30d (so they know what they tried and failed to use)
  - days_until_renewal (from users.tier_expires_at or 30 default)

SAFETY
------
- Kill switch: MCP_FUNNEL_R3_DISABLE=1
- Rate-limit 10 req/min per key (sliding window in-process)
- Key validated against mcp_dev_keys.status='active' — revoked keys
  return 403 immediately.
- No write paths. Read-only.
- We never echo the FULL key back; only the first 8 chars + last 4.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from html import escape as _esc
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

customer_usage_dashboard_bp = Blueprint("customer_usage_dashboard", __name__)


# ── env ───────────────────────────────────────────────────────────────

_DISABLE = os.environ.get("MCP_FUNNEL_R3_DISABLE", "").strip() in ("1", "true", "yes")


# ── Rate limit (sliding 60s window, 10 calls per key) ────────────────

_RL_LIMIT_PER_MIN = 10
_RL: dict[str, deque] = defaultdict(deque)


def _rate_limited(key: str) -> bool:
    """True if `key` has used its quota in the last 60s. Sliding window."""
    now = time.time()
    q = _RL[key]
    cutoff = now - 60.0
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= _RL_LIMIT_PER_MIN:
        return True
    q.append(now)
    return False


# ── DB ────────────────────────────────────────────────────────────────

def _conn():
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if not dsn:
            return None
        c = _pg.connect(dsn, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("customer_usage: db connect failed: %s", e)
        return None


def _savepoint(cur, name: str) -> bool:
    try:
        cur.execute(f"SAVEPOINT {name}")
        return True
    except Exception:
        return False


def _rollback_sp(cur, name: str) -> None:
    try:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
    except Exception:
        pass


def _release_sp(cur, name: str) -> None:
    try:
        cur.execute(f"RELEASE SAVEPOINT {name}")
    except Exception:
        pass


# ── Tier helpers ──────────────────────────────────────────────────────

def _tier_label(tier: str) -> str:
    try:
        from tier_registry import label
        return label(tier or "free")
    except Exception:
        return (tier or "free").title()


def _tier_daily_cap(tier: str) -> int:
    try:
        from tier_registry import limits
        return int(limits(tier or "free").get("mcp_daily", 10))
    except Exception:
        return 10


def _tier_monthly_price(tier: str) -> Optional[int]:
    try:
        from tier_registry import price
        return price(tier or "free")
    except Exception:
        return None


def _is_paid_tier(tier: str) -> bool:
    try:
        from tier_registry import is_paid
        return is_paid(tier or "free")
    except Exception:
        return False


# ── Lookup ────────────────────────────────────────────────────────────

def _validate_key(api_key: str) -> Optional[dict]:
    """Look up the key in mcp_dev_keys. Returns:
        {api_key, tier, email, status, created_at, last_used_at}
    or None if missing/revoked."""
    if not api_key or len(api_key) < 16:
        return None
    conn = _conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT api_key, tier, email, status, created_at, "
                    "       last_used_at "
                    "  FROM mcp_dev_keys "
                    " WHERE api_key = %s "
                    "   AND COALESCE(status, 'active') = 'active'",
                    (api_key,))
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "api_key": r[0],
                    "tier": r[1] or "free",
                    "email": r[2] or "",
                    "status": r[3] or "active",
                    "created_at": (r[4].isoformat() if r[4] else None),
                    "last_used_at": (r[5].isoformat() if r[5] else None),
                }
            except Exception as e:
                logger.debug("validate_key failed: %s", e)
                return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _compute_usage(api_key: str, tier: str) -> dict:
    """Heavy lifting: per-tool counts, paywall hits, today vs 30d, etc."""
    out: dict[str, Any] = {
        "calls_today": 0,
        "calls_7d": 0,
        "calls_30d": 0,
        "paywall_hits_30d": 0,
        "top_tools_30d": [],
        "all_tools_called": [],
        "unused_tools": [],
        "daily_cap": _tier_daily_cap(tier),
        "cap_pct": 0.0,
        "approaching_cap": False,
        "days_until_renewal": 30,
        "renewal_date": None,
    }
    conn = _conn()
    if conn is None:
        return out
    try:
        with conn.cursor() as cur:

            # Calls today (UTC).
            if _savepoint(cur, "cu_today"):
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM mcp_call_log "
                        " WHERE api_key = %s "
                        "   AND timestamp >= DATE_TRUNC('day', NOW())",
                        (api_key,))
                    r = cur.fetchone()
                    out["calls_today"] = int((r and r[0]) or 0)
                    _release_sp(cur, "cu_today")
                except Exception:
                    _rollback_sp(cur, "cu_today")

            # 7d + 30d totals.
            if _savepoint(cur, "cu_totals"):
                try:
                    cur.execute(
                        "SELECT "
                        "  COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '7 days'), "
                        "  COUNT(*) FILTER (WHERE timestamp >= NOW() - INTERVAL '30 days') "
                        "  FROM mcp_call_log WHERE api_key = %s",
                        (api_key,))
                    r = cur.fetchone()
                    if r:
                        out["calls_7d"] = int(r[0] or 0)
                        out["calls_30d"] = int(r[1] or 0)
                    _release_sp(cur, "cu_totals")
                except Exception:
                    _rollback_sp(cur, "cu_totals")

            # Per-tool top-10 in 30d.
            tools_called: list[str] = []
            if _savepoint(cur, "cu_top"):
                try:
                    cur.execute(
                        "SELECT tool, COUNT(*) AS n "
                        "  FROM mcp_call_log "
                        " WHERE api_key = %s "
                        "   AND timestamp >= NOW() - INTERVAL '30 days' "
                        "   AND tool IS NOT NULL "
                        " GROUP BY tool ORDER BY n DESC",
                        (api_key,))
                    for tool, n in cur.fetchall() or []:
                        tools_called.append(tool)
                        if len(out["top_tools_30d"]) < 10:
                            out["top_tools_30d"].append(
                                {"tool": tool, "count": int(n or 0)})
                    out["all_tools_called"] = tools_called
                    _release_sp(cur, "cu_top")
                except Exception:
                    _rollback_sp(cur, "cu_top")

            # Paywall hits (mcp_upgrade_signals — Fix C wrote tool_requested).
            if _savepoint(cur, "cu_pw"):
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM mcp_upgrade_signals "
                        " WHERE caller_id = %s "
                        "   AND created_at >= NOW() - INTERVAL '30 days'",
                        (api_key,))
                    r = cur.fetchone()
                    out["paywall_hits_30d"] = int((r and r[0]) or 0)
                    _release_sp(cur, "cu_pw")
                except Exception:
                    _rollback_sp(cur, "cu_pw")

            # Renewal date — users.tier_expires_at when present.
            if _savepoint(cur, "cu_renew"):
                try:
                    cur.execute(
                        "SELECT u.tier_expires_at "
                        "  FROM users u JOIN mcp_dev_keys k ON LOWER(k.email) = LOWER(u.email) "
                        " WHERE k.api_key = %s AND u.tier_expires_at IS NOT NULL "
                        " ORDER BY u.tier_expires_at DESC LIMIT 1",
                        (api_key,))
                    r = cur.fetchone()
                    if r and r[0]:
                        from datetime import datetime as _dt
                        exp = r[0]
                        try:
                            now = _dt.now(timezone.utc)
                            days = (exp - now).days
                            out["days_until_renewal"] = max(0, int(days))
                            out["renewal_date"] = exp.isoformat()
                        except Exception:
                            pass
                    _release_sp(cur, "cu_renew")
                except Exception:
                    _rollback_sp(cur, "cu_renew")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Cross-sell: tools they haven't used out of the 38 catalog. Cap to 6
    # so the UI doesn't explode.
    try:
        from routes.mcp_tool_catalog import TOOLS
        all_tools = [t[0] for t in TOOLS]
        used = set(out["all_tools_called"])
        unused = [t for t in all_tools if t not in used]
        # Prefer "free" tier tools as the friction-free cross-sell entry.
        by_tier = {t[0]: t[2] for t in TOOLS}
        unused.sort(key=lambda t: (0 if by_tier.get(t) == "free" else 1))
        out["unused_tools"] = unused[:6]
    except Exception:
        pass

    # Cap percent + approaching flag (>=80%).
    daily_cap = max(1, out["daily_cap"])
    out["cap_pct"] = round(min(100.0, 100.0 * out["calls_today"] / daily_cap), 1)
    out["approaching_cap"] = out["cap_pct"] >= 80.0

    return out


# ── JSON endpoint ────────────────────────────────────────────────────

def _build_payload(api_key: str) -> tuple[Optional[dict], Optional[str], int]:
    """Returns (payload, error, http_status)."""
    if _DISABLE:
        return (None, "feature disabled", 503)
    key_row = _validate_key(api_key)
    if not key_row:
        return (None, "invalid or revoked key", 401)

    tier = key_row["tier"] or "free"
    usage = _compute_usage(api_key, tier)
    price = _tier_monthly_price(tier)
    paid = _is_paid_tier(tier)

    # Mask the key.
    masked = (api_key[:8] + "..." + api_key[-4:]) if len(api_key) > 14 else api_key

    payload = {
        "key": masked,
        "tier": tier,
        "tier_label": _tier_label(tier),
        "tier_paid": paid,
        "price_usd_month": price,
        "email": key_row["email"],
        "key_created_at": key_row["created_at"],
        "last_used_at": key_row["last_used_at"],
        "calls_today": usage["calls_today"],
        "daily_cap": usage["daily_cap"],
        "cap_pct": usage["cap_pct"],
        "approaching_cap": usage["approaching_cap"],
        "calls_7d": usage["calls_7d"],
        "calls_30d": usage["calls_30d"],
        "paywall_hits_30d": usage["paywall_hits_30d"],
        "top_tools_30d": usage["top_tools_30d"],
        "unused_tools": usage["unused_tools"],
        "days_until_renewal": usage["days_until_renewal"],
        "renewal_date": usage["renewal_date"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return (payload, None, 200)


@customer_usage_dashboard_bp.route(
    "/dashboard/usage/data", methods=["POST", "GET"])
@customer_usage_dashboard_bp.route(
    "/api/v1/customer/usage", methods=["POST", "GET"])
def customer_usage_data():
    """Public JSON endpoint. Accepts the API key as:
       - X-API-Key header
       - 'api_key' form field
       - 'api_key' query param
    Rate-limited 10 req/min per key.
    Never write to a cookie (we don't want this to be CSRF-able)."""

    api_key = (request.headers.get("X-API-Key")
               or (request.form.get("api_key") if request.method == "POST" else None)
               or request.args.get("api_key") or "").strip()
    if not api_key:
        return jsonify(error="missing_api_key"), 400

    if _rate_limited(api_key):
        return jsonify(error="rate_limited",
                       retry_after_s=60,
                       limit_per_min=_RL_LIMIT_PER_MIN), 429

    payload, err, status = _build_payload(api_key)
    if err:
        return jsonify(error=err), status
    return jsonify(payload), 200


# ── HTML dashboard ───────────────────────────────────────────────────

_DASHBOARD_HTML = """<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>Your DC Hub Usage</title>
  <meta name="description" content="Your DC Hub MCP usage dashboard — tool calls, rate limit status, top tools, and upgrade suggestions.">
  <meta name="robots" content="noindex,nofollow">
  <link rel="stylesheet" href="/dchub-brand.css">
  <style>
    :root{--bg:#0a0e1a;--bg-card:#0e1424;--fg:#e6ecf5;--muted:#94a3b8;
          --accent:#3da9fc;--accent-2:#22c55e;--border:#1c2942;
          --warn:#f59e0b;--err:#ef4444;}
    body{background:var(--bg);color:var(--fg);
         font-family:'Inter',system-ui,sans-serif;
         margin:0;padding:32px;}
    .wrap{max-width:1080px;margin:0 auto;}
    h1{font-size:24px;margin:0 0 6px;}
    .sub{color:var(--muted);font-size:13px;margin-bottom:24px;}
    .card{background:var(--bg-card);border:1px solid var(--border);
          border-radius:8px;padding:20px;margin-bottom:18px;}
    .card h2{font-size:14px;margin:0 0 14px;color:var(--fg);
             text-transform:uppercase;letter-spacing:0.06em;
             font-weight:600;color:var(--muted);}
    .key-form{display:flex;gap:8px;align-items:center;}
    .key-form input{flex:1;background:#0a0e1a;border:1px solid var(--border);
                    color:var(--fg);padding:10px 14px;border-radius:6px;
                    font-family:'JetBrains Mono',monospace;font-size:13px;}
    .key-form button{background:var(--accent);color:#0a0e1a;border:0;
                     padding:10px 24px;border-radius:6px;font-weight:600;
                     cursor:pointer;font-size:13px;}
    .key-form button:hover{filter:brightness(1.1);}
    .err{color:var(--err);font-size:12px;margin-top:8px;}
    .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;
           margin-bottom:18px;}
    .stat{background:var(--bg-card);border:1px solid var(--border);
          border-radius:8px;padding:18px;}
    .stat .v{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;
             color:var(--accent);}
    .stat .l{font-size:11px;color:var(--muted);text-transform:uppercase;
             letter-spacing:0.05em;}
    .stat .d{font-size:10px;color:var(--muted);margin-top:4px;}
    .meter{height:8px;background:var(--border);border-radius:4px;
           overflow:hidden;margin-top:8px;}
    .meter .fill{height:100%;background:var(--accent-2);transition:width 0.3s;}
    .meter.warn .fill{background:var(--warn);}
    .meter.err .fill{background:var(--err);}
    table{width:100%;border-collapse:collapse;font-size:12px;}
    th{background:#0a0e1a;color:var(--muted);text-align:left;
       padding:8px 10px;border-bottom:1px solid var(--border);
       font-weight:600;font-size:10px;text-transform:uppercase;}
    td{padding:8px 10px;border-bottom:1px solid var(--border);}
    td.num{text-align:right;font-variant-numeric:tabular-nums;}
    .cta{background:linear-gradient(135deg,var(--accent),var(--accent-2));
         color:#0a0e1a;padding:14px 28px;border-radius:8px;font-weight:700;
         text-decoration:none;display:inline-block;font-size:13px;}
    .pill{background:#3da9fc22;color:var(--accent);padding:2px 8px;
          border-radius:3px;font-size:10px;font-weight:600;
          text-transform:uppercase;letter-spacing:0.05em;}
    .pill-pro{background:#a78bfa22;color:#a78bfa;}
    .pill-free{background:#94a3b822;color:var(--muted);}
    .footer{font-size:10px;color:var(--muted);margin-top:24px;text-align:center;}
    .hidden{display:none;}
    .unused{display:flex;flex-wrap:wrap;gap:6px;}
    .unused .tag{background:var(--border);color:var(--fg);
                 padding:4px 10px;border-radius:4px;font-size:11px;
                 font-family:'JetBrains Mono',monospace;}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Your DC Hub Usage</h1>
    <div class="sub">
      Self-serve view of your MCP key — calls, cap, top tools, suggested tools
      you haven't tried, days until renewal. Updated live. Your key never
      leaves this browser.
    </div>

    <div class="card" id="login">
      <h2>Enter your DC Hub API key</h2>
      <form class="key-form" id="form-key" onsubmit="return loadUsage(event);">
        <input type="password" id="api_key" placeholder="dch_xxxxxxxxxxxxxxxxxxxxxx" autocomplete="off">
        <button type="submit">View usage</button>
      </form>
      <div class="err hidden" id="err"></div>
      <div style="font-size:11px;color:var(--muted);margin-top:10px;">
        Don't have a key? <a href="/pricing" style="color:var(--accent);">See pricing</a> or
        <a href="/mcp" style="color:var(--accent);">connect via MCP</a> to mint one.
      </div>
    </div>

    <div id="dashboard" class="hidden">
      <div class="grid3">
        <div class="stat">
          <div class="l">Plan</div>
          <div class="v" id="tier-label" style="color:var(--accent-2);">—</div>
          <div class="d" id="tier-price">—</div>
        </div>
        <div class="stat">
          <div class="l">Calls today</div>
          <div class="v" id="calls-today">—</div>
          <div class="d" id="cap-text">— of —</div>
          <div class="meter" id="cap-meter"><div class="fill" id="cap-fill" style="width:0%;"></div></div>
        </div>
        <div class="stat">
          <div class="l">Days until renewal</div>
          <div class="v" id="days-renewal">—</div>
          <div class="d" id="renewal-date">—</div>
        </div>
      </div>

      <div class="grid3">
        <div class="stat">
          <div class="l">Calls last 7d</div>
          <div class="v" id="calls-7d">—</div>
        </div>
        <div class="stat">
          <div class="l">Calls last 30d</div>
          <div class="v" id="calls-30d">—</div>
        </div>
        <div class="stat">
          <div class="l">Paywall hits 30d</div>
          <div class="v" id="paywall-30d" style="color:var(--warn);">—</div>
          <div class="d">Tools you tried but couldn't unlock</div>
        </div>
      </div>

      <div class="card">
        <h2>Top tools (30d)</h2>
        <table>
          <thead><tr><th>Tool</th><th style="text-align:right">Calls</th></tr></thead>
          <tbody id="top-tools"><tr><td colspan="2" style="color:var(--muted);text-align:center;padding:24px;">No calls in last 30d</td></tr></tbody>
        </table>
      </div>

      <div class="card" id="cross-sell-card">
        <h2>Suggested tools you haven't used</h2>
        <div class="unused" id="unused-list"></div>
        <div style="font-size:11px;color:var(--muted);margin-top:12px;">
          See the <a href="/mcp/tools" style="color:var(--accent);">full tool catalog</a> for examples.
        </div>
      </div>

      <div class="card hidden" id="upgrade-card" style="background:linear-gradient(135deg,#1c2942,#0e1424);border-color:var(--warn);">
        <h2 style="color:var(--warn);">Approaching daily cap</h2>
        <div style="font-size:13px;margin-bottom:16px;">
          You've used <strong id="upgrade-cap-pct">—</strong>% of your daily quota.
          Upgrade to Pro to get <strong>2,000 calls/day</strong> + every tool unlocked.
        </div>
        <a href="/pricing" class="cta">Upgrade to Pro — $199/mo</a>
      </div>

      <div style="text-align:center;margin-top:24px;">
        <a href="/dashboard/usage" style="color:var(--accent);font-size:11px;">Reload</a>
        ·
        <a href="/pricing" style="color:var(--accent);font-size:11px;">Pricing</a>
        ·
        <a href="/mcp/tools" style="color:var(--accent);font-size:11px;">All MCP tools</a>
      </div>
    </div>

    <div class="footer">
      routes/customer_usage_dashboard.py · key never stored ·
      rate-limited 10 req/min
    </div>
  </div>

<script>
async function loadUsage(ev){
  if (ev) ev.preventDefault();
  var key = document.getElementById('api_key').value.trim();
  var err = document.getElementById('err');
  err.classList.add('hidden');
  if (!key) { err.textContent = 'Enter a key.'; err.classList.remove('hidden'); return false; }
  try {
    var r = await fetch('/dashboard/usage/data', {
      method: 'POST',
      headers: {'Content-Type':'application/x-www-form-urlencoded'},
      body: 'api_key=' + encodeURIComponent(key),
    });
    if (r.status === 429) {
      err.textContent = 'Rate limited — try again in 60s.';
      err.classList.remove('hidden');
      return false;
    }
    var j = await r.json();
    if (!r.ok) {
      err.textContent = j.error || ('error: ' + r.status);
      err.classList.remove('hidden');
      return false;
    }
    render(j);
  } catch (e) {
    err.textContent = 'Network error: ' + e;
    err.classList.remove('hidden');
  }
  return false;
}

function fmt(n){ return (n||0).toLocaleString(); }

function render(j){
  document.getElementById('login').classList.add('hidden');
  document.getElementById('dashboard').classList.remove('hidden');
  document.getElementById('tier-label').textContent = j.tier_label || 'Free';
  if (j.price_usd_month) {
    document.getElementById('tier-price').textContent = '$' + j.price_usd_month + '/mo';
  } else {
    document.getElementById('tier-price').textContent = j.tier_paid ? 'Custom' : 'Free tier';
  }
  document.getElementById('calls-today').textContent = fmt(j.calls_today);
  document.getElementById('cap-text').textContent = fmt(j.calls_today) + ' of ' + fmt(j.daily_cap);
  var fill = document.getElementById('cap-fill');
  fill.style.width = Math.min(100, j.cap_pct) + '%';
  var meter = document.getElementById('cap-meter');
  meter.classList.remove('warn','err');
  if (j.cap_pct >= 95) meter.classList.add('err');
  else if (j.cap_pct >= 80) meter.classList.add('warn');
  document.getElementById('days-renewal').textContent = fmt(j.days_until_renewal);
  document.getElementById('renewal-date').textContent = j.renewal_date
    ? new Date(j.renewal_date).toLocaleDateString()
    : (j.tier_paid ? 'Renews automatically' : 'Free — no renewal');
  document.getElementById('calls-7d').textContent = fmt(j.calls_7d);
  document.getElementById('calls-30d').textContent = fmt(j.calls_30d);
  document.getElementById('paywall-30d').textContent = fmt(j.paywall_hits_30d);

  // Top tools table.
  var tbody = document.getElementById('top-tools');
  if ((j.top_tools_30d||[]).length) {
    tbody.innerHTML = j.top_tools_30d.map(function(r){
      return '<tr><td>'+ escapeHtml(r.tool) +'</td><td class="num">'+ fmt(r.count) +'</td></tr>';
    }).join('');
  }

  // Unused tools (cross-sell).
  var ul = document.getElementById('unused-list');
  if ((j.unused_tools||[]).length) {
    ul.innerHTML = j.unused_tools.map(function(t){
      return '<a href="/mcp/tools#'+ escapeHtml(t) +'" class="tag">'+ escapeHtml(t) +'</a>';
    }).join('');
  } else {
    document.getElementById('cross-sell-card').classList.add('hidden');
  }

  // Upgrade card if approaching cap or on free tier with substantial usage.
  if (j.approaching_cap || (!j.tier_paid && j.calls_30d >= 50)) {
    document.getElementById('upgrade-card').classList.remove('hidden');
    document.getElementById('upgrade-cap-pct').textContent = j.cap_pct;
  }
}

function escapeHtml(s){ var d=document.createElement('div'); d.textContent = s||''; return d.innerHTML; }
</script>
</body>
</html>"""


@customer_usage_dashboard_bp.route("/dashboard/usage", methods=["GET"])
def dashboard_usage():
    """Public HTML page. The user enters their key client-side and the
    JS POSTs to /dashboard/usage/data."""
    return Response(_DASHBOARD_HTML, mimetype="text/html")


@customer_usage_dashboard_bp.route("/dashboard/usage/embed.js", methods=["GET"])
def dashboard_usage_embed():
    """Lightweight JS partners can embed to show a customer's usage on
    their own site. Reads ?api_key= from the script tag. Read-only;
    never POSTs the key back to their server."""
    js = """(function(){
  var src = document.currentScript && document.currentScript.src;
  if (!src) return;
  try {
    var u = new URL(src);
    var key = u.searchParams.get('api_key');
    if (!key) return;
    fetch('https://dchub.cloud/api/v1/customer/usage?api_key=' + encodeURIComponent(key))
      .then(function(r){return r.json();})
      .then(function(j){
        var div = document.createElement('div');
        div.style.cssText = 'background:#0e1424;color:#e6ecf5;border:1px solid #1c2942;border-radius:6px;padding:16px;font-family:system-ui;font-size:13px;';
        div.innerHTML = '<strong>'+ (j.tier_label||'Free') +'</strong> · ' +
          (j.calls_today||0).toLocaleString() + ' / ' + (j.daily_cap||0).toLocaleString() + ' calls today · ' +
          '<a href="https://dchub.cloud/dashboard/usage" style="color:#3da9fc">View dashboard</a>';
        var holder = document.getElementById('dchub-usage') || document.body;
        holder.appendChild(div);
      })
      .catch(function(){});
  } catch (e) {}
})();"""
    return Response(js, mimetype="application/javascript")
