"""
paid_intent_ledger — turn grid/fiber paywall hits into a queryable lead pipeline.

Brain L6 proposal #1264 (2026-06-24). The funnel data: get_grid_intelligence
(~5.3K calls / 201 users) + get_fiber_intel (~4.5K / 182) are the overwhelming
paid-demand drivers, yet ~8 conversions/30d — ~10K "I want this" events from 380+
distinct agents hit the paywall and bounce SILENTLY. This records each such hit —
the caller (hashed key + IP), tier, and the EXACT site context they asked about
(region/ISO/MW/lat-lon) — into `mcp_paid_intent`, so every high-demand 402 becomes
a re-targetable lead with the specific query attached, instead of a lost bounce.

SAFE BY DESIGN: read-only side-effect on the serve path (never alters the response
or gate), fire-and-forget (a daemon thread does the INSERT so the hot path isn't
blocked or pool-starved), defensive (any error is swallowed), and the raw API key
is NEVER stored — only a salted-ish sha256 prefix. Capture is limited to the two
grid/fiber tools the brain flagged. Kill switch: PAID_INTENT_LEDGER_DISABLE=1.

Read the pipeline (admin-gated, DCHUB_ADMIN_KEY — mirrors brain_backlog_admin):
  GET /api/v1/admin/paid-intent          recent leads (with site context)
  GET /api/v1/admin/paid-intent/summary  totals by tool / tier / top regions
"""
import os
import json
import hashlib
import logging
import threading

from flask import Blueprint, request, jsonify
from util.json_column import json_for_column

logger = logging.getLogger("dchub.paid_intent_ledger")

try:
    import psycopg2
    import psycopg2.extras
except Exception:  # pragma: no cover
    psycopg2 = None

# The grid/fiber tools the brain flagged as the paid-demand drivers.
INTENT_TOOLS = {"get_grid_intelligence", "get_fiber_intel"}
# Non-paid tiers whose hits are genuine UNMET demand (a paid caller isn't a lead).
_LEAD_TIERS = {"", "free", "anon", "anonymous", "identified", "trial",
               "trial_taste", "trial_preview", "preview"}

paid_intent_bp = Blueprint("paid_intent_ledger", __name__)


def _disabled() -> bool:
    return (os.environ.get("PAID_INTENT_LEDGER_DISABLE", "0") or "0").lower() in (
        "1", "true", "yes", "on")


def _dsn():
    return os.environ.get("DATABASE_URL")


def _hash(s: str) -> str:
    if not s:
        return ""
    return hashlib.sha256(("dchub_intent::" + s).encode()).hexdigest()[:16]


def _ensure_table(cur):
    # Raw-conn DDL (db_utils get_db() silently SKIPs DDL via SKIP_DDL=1).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mcp_paid_intent (
            id           BIGSERIAL PRIMARY KEY,
            ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            tool         TEXT NOT NULL,
            tier         TEXT,
            api_key_hash TEXT,
            ip_hash      TEXT,
            email        TEXT,
            region       TEXT,
            iso          TEXT,
            mw           DOUBLE PRECISION,
            lat          DOUBLE PRECISION,
            lon          DOUBLE PRECISION,
            raw_args     JSONB,
            user_agent   TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_mcp_paid_intent_ts ON mcp_paid_intent (ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_mcp_paid_intent_tool ON mcp_paid_intent (tool)")


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def _insert(payload: dict):
    """Runs on a daemon thread — never touches the request context."""
    dsn = _dsn()
    if not dsn or psycopg2 is None:
        print("[paid_intent] insert skipped: no DSN / psycopg2", flush=True)
        return
    try:
        conn = psycopg2.connect(dsn, sslmode="require", connect_timeout=6)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _ensure_table(cur)
                cur.execute(
                    "INSERT INTO mcp_paid_intent "
                    "(tool, tier, api_key_hash, ip_hash, email, region, iso, mw, lat, lon, raw_args, user_agent) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (payload["tool"], payload["tier"], payload["api_key_hash"],
                     payload["ip_hash"], payload["email"], payload["region"],
                     payload["iso"], payload["mw"], payload["lat"], payload["lon"],
                     json_for_column(payload["raw_args"], 4000), payload["user_agent"]),
                )
        finally:
            try: conn.close()
            except Exception: pass
        # print() (not logger) so it surfaces in Railway deploy logs reliably.
        print(f"[paid_intent] inserted tool={payload['tool']} tier={payload['tier']} region={payload['region']}", flush=True)
    except Exception as e:
        print(f"[paid_intent] insert FAILED: {str(e)[:200]}", flush=True)


def record_intent(tool_name: str, tier, req) -> None:
    """Fire-and-forget. Called from _gate_mcp_result on the free/identified serve
    path for grid/fiber tools. Extracts caller + site context SYNCHRONOUSLY (the
    Flask request context isn't available off-thread), then threads the INSERT.
    Never raises into the caller."""
    try:
        if _disabled() or tool_name not in INTENT_TOOLS:
            return
        _tier = str(tier or "").lower()
        if _tier not in _LEAD_TIERS:
            return  # a paid caller getting full data is not a lead
        api_key = (req.headers.get("X-API-Key", "")
                   or req.args.get("api_key", "") or "").strip()
        ip = (req.headers.get("CF-Connecting-IP")
              or (req.headers.get("X-Forwarded-For", "") or "").split(",")[0].strip()
              or req.remote_addr or "")
        ua = (req.headers.get("User-Agent", "") or "")[:300]
        # the tool arguments carry the site context (region/iso/MW/lat-lon)
        args = {}
        try:
            body = req.get_json(silent=True) or {}
            args = (body.get("params") or {}).get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
        except Exception:
            args = {}
        # also accept query-string params (REST alias path)
        for k in ("region", "region_id", "iso", "mw", "capacity_mw", "min_mw", "lat", "lon", "email"):
            if k not in args and req.args.get(k) is not None:
                args[k] = req.args.get(k)
        payload = {
            "tool": tool_name,
            "tier": _tier,
            "api_key_hash": _hash(api_key),
            "ip_hash": _hash(ip),
            "email": (args.get("email") or "")[:200] or None,
            "region": (str(args.get("region") or args.get("region_id") or "")[:120] or None),
            "iso": (str(args.get("iso") or "")[:40] or None),
            "mw": _f(args.get("mw") or args.get("capacity_mw") or args.get("min_mw")),
            "lat": _f(args.get("lat")),
            "lon": _f(args.get("lon") or args.get("lng")),
            "raw_args": args if isinstance(args, dict) else {},
            "user_agent": ua,
        }
        threading.Thread(target=_insert, args=(payload,), daemon=True).start()
    except Exception as e:
        logger.warning("[paid_intent] record_intent skipped: %s", str(e)[:160])


# Map the MCP server's signal_type → a tier label for the ledger.
_SIGNAL_TIER = {
    "trial_preview": "free", "anon_preview": "free", "daily_limit_hit": "free",
    "paid_tool_blocked": "identified", "blocked_paid_only": "identified",
}


def record_from_signal(tool, signal_type, api_key=None, email=None, ip=None,
                       user_agent=None, session_id=None, args=None) -> None:
    """Fire-and-forget. Called by the LIVE signal-paywall handler
    (flask_mcp_endpoints.mcp_signal_paywall), which the Node MCP server hits on
    EVERY grid/fiber paywall — so this captures real public traffic the Flask
    _gate_mcp_result path never sees. Plain-value inputs (no request context).
    `args` is the tool's arguments dict (region/ISO/MW/lat-lon) when the MCP
    server forwards it. Filters to grid/fiber; never raises."""
    try:
        tool = (tool or "").strip()
        if _disabled() or tool not in INTENT_TOOLS:
            return
        a = args if isinstance(args, dict) else {}
        payload = {
            "tool": tool,
            "tier": _SIGNAL_TIER.get((signal_type or "").strip(), "free"),
            "api_key_hash": _hash((api_key or "").strip()) or _hash((session_id or "").strip()),
            "ip_hash": _hash((ip or "").strip()),
            "email": ((email or a.get("email") or "") or "")[:200] or None,
            "region": (str(a.get("region") or a.get("region_id") or "")[:120] or None),
            "iso": (str(a.get("iso") or "")[:40] or None),
            "mw": _f(a.get("mw") or a.get("capacity_mw") or a.get("min_mw")),
            "lat": _f(a.get("lat")),
            "lon": _f(a.get("lon") or a.get("lng")),
            "raw_args": a,
            "user_agent": (user_agent or "")[:300],
        }
        threading.Thread(target=_insert, args=(payload,), daemon=True).start()
    except Exception as e:
        logger.warning("[paid_intent] record_from_signal skipped: %s", str(e)[:160])


# ── Admin read surface (the "queryable intent table" the spec asks for) ──────
def _admin_ok() -> bool:
    expected = (os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("ADMIN_KEY") or "").strip()
    if not expected:
        return False
    provided = (request.headers.get("X-Admin-Key") or request.args.get("admin_key") or "").strip()
    if not provided:
        return False
    import hmac
    return hmac.compare_digest(provided, expected)


@paid_intent_bp.route("/api/v1/admin/paid-intent", methods=["GET"])
def paid_intent_list():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_key required"}), 401
    if psycopg2 is None or not _dsn():
        return jsonify({"ok": False, "error": "db unavailable"}), 200
    limit = min(500, max(1, int(request.args.get("limit", "100") or 100)))
    days = min(365, max(1, int(request.args.get("days", "30") or 30)))
    try:
        conn = psycopg2.connect(_dsn(), sslmode="require", connect_timeout=8)
        conn.autocommit = True
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                _ensure_table(cur)
                cur.execute(
                    "SELECT id, ts, tool, tier, api_key_hash, ip_hash, email, region, iso, "
                    "mw, lat, lon, user_agent FROM mcp_paid_intent "
                    "WHERE ts >= NOW() - (%s || ' days')::interval "
                    "ORDER BY ts DESC LIMIT %s", (str(days), limit))
                rows = [dict(r) for r in cur.fetchall()]
                for r in rows:
                    if r.get("ts"):
                        r["ts"] = r["ts"].isoformat()
            return jsonify({"ok": True, "days": days, "count": len(rows), "leads": rows}), 200
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200


@paid_intent_bp.route("/api/v1/admin/paid-intent/summary", methods=["GET"])
def paid_intent_summary():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_key required"}), 401
    if psycopg2 is None or not _dsn():
        return jsonify({"ok": False, "error": "db unavailable"}), 200
    days = min(365, max(1, int(request.args.get("days", "30") or 30)))
    try:
        conn = psycopg2.connect(_dsn(), sslmode="require", connect_timeout=8)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _ensure_table(cur)
                w = "WHERE ts >= NOW() - (%s || ' days')::interval"
                cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT api_key_hash), COUNT(DISTINCT ip_hash) "
                            f"FROM mcp_paid_intent {w}", (str(days),))
                total, distinct_keys, distinct_ips = cur.fetchone()
                cur.execute(f"SELECT tool, COUNT(*) FROM mcp_paid_intent {w} GROUP BY tool ORDER BY 2 DESC", (str(days),))
                by_tool = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute(f"SELECT tier, COUNT(*) FROM mcp_paid_intent {w} GROUP BY tier ORDER BY 2 DESC", (str(days),))
                by_tier = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute(f"SELECT region, COUNT(*) FROM mcp_paid_intent {w} AND region IS NOT NULL "
                            f"GROUP BY region ORDER BY 2 DESC LIMIT 15", (str(days),))
                top_regions = [{"region": r[0], "hits": r[1]} for r in cur.fetchall()]
                cur.execute(f"SELECT iso, COUNT(*) FROM mcp_paid_intent {w} AND iso IS NOT NULL "
                            f"GROUP BY iso ORDER BY 2 DESC LIMIT 15", (str(days),))
                top_isos = [{"iso": r[0], "hits": r[1]} for r in cur.fetchall()]
            return jsonify({
                "ok": True, "days": days,
                "total_intent_events": total or 0,
                "distinct_keyed_callers": distinct_keys or 0,
                "distinct_ips": distinct_ips or 0,
                "by_tool": by_tool, "by_tier": by_tier,
                "top_regions": top_regions, "top_isos": top_isos,
                "note": ("Each row is a non-paid caller who hit the grid/fiber paywall with a "
                         "specific site query — a re-targetable lead. Pull the full list at "
                         "/api/v1/admin/paid-intent?days=30."),
            }), 200
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 200


# ── Weekly intent DIGEST — turn the ledger into an owner-actionable email ─────
# Closes the loop the ledger opened: every grid/fiber paywall hit is a captured
# lead, and this surfaces the week's demand (top regions/ISOs/tools + distinct
# callers + a sample of leads) so the operator (and the brain) can act on it.
# Reuses the platform Resend sender (mirrors routes/brain_weekly_digest.py).
_RESEND_KEY = (os.environ.get("DCHUB_RESEND_API_KEY") or os.environ.get("RESEND_API_KEY") or "").strip()


def _digest_recipients():
    raw = (os.environ.get("PAID_INTENT_DIGEST_TO")
           or os.environ.get("DCHUB_BRAIN_DIGEST_TO")
           or "azmartone@gmail.com").strip()
    return [r.strip() for r in raw.split(",") if r.strip()]


def _digest_from():
    return (os.environ.get("DCHUB_BRAIN_DIGEST_FROM")
            or "Jonathan Martone <jonathan@dchub.cloud>")


def build_intent_digest(days: int = 7) -> dict:
    """READ-ONLY. Summarize the ledger over `days` for the digest."""
    out = {"days": days, "total": 0, "distinct_callers": 0, "distinct_ips": 0,
           "by_tool": {}, "by_tier": {}, "top_regions": [], "top_isos": [], "recent": []}
    if psycopg2 is None or not _dsn():
        out["error"] = "db unavailable"
        return out
    try:
        conn = psycopg2.connect(_dsn(), sslmode="require", connect_timeout=8)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                _ensure_table(cur)
                w = "WHERE ts >= NOW() - (%s || ' days')::interval"
                cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT api_key_hash), COUNT(DISTINCT ip_hash) FROM mcp_paid_intent {w}", (str(days),))
                out["total"], out["distinct_callers"], out["distinct_ips"] = cur.fetchone()
                cur.execute(f"SELECT tool, COUNT(*) FROM mcp_paid_intent {w} GROUP BY tool ORDER BY 2 DESC", (str(days),))
                out["by_tool"] = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute(f"SELECT tier, COUNT(*) FROM mcp_paid_intent {w} GROUP BY tier ORDER BY 2 DESC", (str(days),))
                out["by_tier"] = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute(f"SELECT region, COUNT(*) FROM mcp_paid_intent {w} AND region IS NOT NULL GROUP BY region ORDER BY 2 DESC LIMIT 12", (str(days),))
                out["top_regions"] = [{"region": r[0], "hits": r[1]} for r in cur.fetchall()]
                cur.execute(f"SELECT iso, COUNT(*) FROM mcp_paid_intent {w} AND iso IS NOT NULL GROUP BY iso ORDER BY 2 DESC LIMIT 12", (str(days),))
                out["top_isos"] = [{"iso": r[0], "hits": r[1]} for r in cur.fetchall()]
                cur.execute(f"SELECT ts, tool, tier, region, iso, mw FROM mcp_paid_intent {w} ORDER BY ts DESC LIMIT 25", (str(days),))
                out["recent"] = [{"ts": (r[0].isoformat() if r[0] else None), "tool": r[1], "tier": r[2],
                                  "region": r[3], "iso": r[4], "mw": r[5]} for r in cur.fetchall()]
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def _render_intent_digest(s: dict):
    days = s.get("days", 7)
    def _esc(x): return str(x if x is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    rows_region = "".join(f"<tr><td>{_esc(r['region'])}</td><td style='text-align:right'>{r['hits']}</td></tr>" for r in s.get("top_regions", [])) or "<tr><td colspan=2 style='color:#888'>none yet</td></tr>"
    rows_iso = "".join(f"<tr><td>{_esc(r['iso'])}</td><td style='text-align:right'>{r['hits']}</td></tr>" for r in s.get("top_isos", [])) or "<tr><td colspan=2 style='color:#888'>none yet</td></tr>"
    tools = ", ".join(f"{k}: {v}" for k, v in (s.get("by_tool") or {}).items()) or "—"
    recent = "".join(
        f"<tr><td>{_esc((l.get('ts') or '')[:16])}</td><td>{_esc(l.get('tool'))}</td><td>{_esc(l.get('tier'))}</td><td>{_esc(l.get('region') or l.get('iso') or '')}</td><td style='text-align:right'>{_esc(l.get('mw') or '')}</td></tr>"
        for l in s.get("recent", [])[:25]) or "<tr><td colspan=5 style='color:#888'>no leads captured this period</td></tr>"
    subject = f"DC Hub · grid/fiber demand — {s.get('total',0)} paywall leads, {s.get('distinct_callers',0)} agents (last {days}d)"
    html = f"""<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:680px;margin:0 auto;color:#0f172a">
<h2 style="margin:0 0 4px">Grid/Fiber Intent — last {days} days</h2>
<p style="color:#475569;margin:0 0 16px">Every row below is a non-paid agent that hit the grid/fiber paywall with a specific site query — a re-targetable lead, not a silent bounce.</p>
<div style="display:flex;gap:24px;margin:0 0 20px">
  <div><div style="font-size:28px;font-weight:700">{s.get('total',0)}</div><div style="color:#64748b;font-size:12px">paywall leads</div></div>
  <div><div style="font-size:28px;font-weight:700">{s.get('distinct_callers',0)}</div><div style="color:#64748b;font-size:12px">distinct agents</div></div>
  <div><div style="font-size:28px;font-weight:700">{s.get('distinct_ips',0)}</div><div style="color:#64748b;font-size:12px">distinct IPs</div></div>
</div>
<p style="margin:0 0 16px"><b>By tool:</b> {_esc(tools)}</p>
<table style="width:100%;border-collapse:collapse;margin:0 0 18px"><thead><tr><th style="text-align:left;border-bottom:1px solid #e2e8f0">Top regions</th><th style="text-align:right;border-bottom:1px solid #e2e8f0">hits</th></tr></thead><tbody>{rows_region}</tbody></table>
<table style="width:100%;border-collapse:collapse;margin:0 0 18px"><thead><tr><th style="text-align:left;border-bottom:1px solid #e2e8f0">Top ISOs</th><th style="text-align:right;border-bottom:1px solid #e2e8f0">hits</th></tr></thead><tbody>{rows_iso}</tbody></table>
<h3 style="margin:18px 0 6px">Recent leads</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left;border-bottom:1px solid #e2e8f0">when</th><th style="text-align:left;border-bottom:1px solid #e2e8f0">tool</th><th style="text-align:left;border-bottom:1px solid #e2e8f0">tier</th><th style="text-align:left;border-bottom:1px solid #e2e8f0">site</th><th style="text-align:right;border-bottom:1px solid #e2e8f0">MW</th></tr></thead><tbody>{recent}</tbody></table>
<p style="color:#94a3b8;font-size:12px;margin-top:20px">DC Hub paid-intent ledger (brain #1264). Full list: /api/v1/admin/paid-intent?days={days}. Kill: PAID_INTENT_LEDGER_DISABLE=1.</p>
</div>"""
    text = (f"Grid/Fiber Intent — last {days}d\n{s.get('total',0)} paywall leads, {s.get('distinct_callers',0)} agents, {s.get('distinct_ips',0)} IPs\n"
            f"By tool: {tools}\nTop regions: " + ", ".join(f"{r['region']}({r['hits']})" for r in s.get('top_regions', [])) +
            "\nTop ISOs: " + ", ".join(f"{r['iso']}({r['hits']})" for r in s.get('top_isos', [])) + "\n")
    return {"subject": subject, "html": html, "text": text}


def send_intent_digest(days: int = 7, recipients=None, dry_run=None) -> dict:
    """Render + email the weekly intent digest via Resend (never raises)."""
    s = build_intent_digest(days)
    rendered = _render_intent_digest(s)
    to_list = recipients or _digest_recipients()
    if not to_list:
        return {"ok": False, "reason": "no_recipients", "summary": s}
    if dry_run or not _RESEND_KEY:
        return {"ok": True, "dry_run": True,
                "reason": ("RESEND key unset" if not _RESEND_KEY else "dry_run"),
                "subject": rendered["subject"], "recipients": to_list, "summary": s}
    import requests as _rq
    sent, failed = [], []
    for r in to_list:
        try:
            rr = _rq.post("https://api.resend.com/emails",
                          headers={"Authorization": f"Bearer {_RESEND_KEY}", "Content-Type": "application/json"},
                          json={"from": _digest_from(), "to": [r], "reply_to": "jonathan@dchub.cloud",
                                "subject": rendered["subject"], "html": rendered["html"], "text": rendered["text"]},
                          timeout=20)
            (sent if rr.status_code < 400 else failed).append({"to": r, "status": rr.status_code})
        except Exception as e:
            failed.append({"to": r, "error": str(e)[:160]})
    return {"ok": bool(sent), "sent": sent, "failed": failed, "subject": rendered["subject"],
            "total_leads": s.get("total", 0), "distinct_agents": s.get("distinct_callers", 0)}


@paid_intent_bp.route("/api/v1/admin/paid-intent/digest/preview", methods=["GET"])
def paid_intent_digest_preview():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_key required"}), 401
    days = min(90, max(1, int(request.args.get("days", "7") or 7)))
    s = build_intent_digest(days)
    r = _render_intent_digest(s)
    if request.args.get("html") in ("1", "true"):
        from flask import Response as _Resp
        return _Resp(r["html"], mimetype="text/html")
    return jsonify({"ok": True, "subject": r["subject"], "summary": s}), 200


@paid_intent_bp.route("/api/v1/admin/paid-intent/digest/send", methods=["POST", "GET"])
def paid_intent_digest_send():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "admin_key required"}), 401
    body = request.get_json(silent=True) or {}
    days = min(90, max(1, int(request.args.get("days") or body.get("days") or 7)))
    dr = request.args.get("dry_run") or body.get("dry_run")
    dr_b = None if dr is None else str(dr).lower() in ("1", "true", "yes")
    recs = body.get("recipients")
    if isinstance(recs, str):
        recs = [x.strip() for x in recs.split(",") if x.strip()]
    return jsonify(send_intent_digest(days=days, recipients=recs, dry_run=dr_b)), 200
