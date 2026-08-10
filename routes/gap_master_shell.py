"""gap_master_shell.py — Gap Master Shell (2026-07-04)
=======================================================

Owns the FIVE gaps from the 07-04 flywheel QA assessment — the things that
remained after the plumbing was fixed (per-tool zeros, blind growth levers,
claim attribution, llms.txt variants). Where the Growth shell asks "are the
levers healthy?", this shell asks "are the assessed GAPS closing?" and takes
ONE bounded action per tick against the worst one.

LANES (each scored 0..1; 1.0 = healed)
--------------------------------------
  demand     — is real external MCP traffic flowing? hours since the last
               is_real_external call (holiday-aware threshold) + 7d-vs-prior-7d
               call trend. The 07-04 assessment's "0 real calls today" watch.
  conversion — is the in-session conversion loop producing? claims redeemed,
               first-ever mcp_session_upgrades row, paid in 30d. The
               "no in-session checkout has EVER completed" gap.
  citations  — citation drought (0 tracked citations since 06-22). velocity_7d
               from /api/v1/media/north-star + days since the last citation.
  search     — Bing/Google recovery machinery: IndexNow freshness (<48h),
               sitemap lastmod today, robots reachable.
  retention  — multi-day return rate (agents active on 2+ days / 30d) +
               returning real IPs this week.

Plus an UNSCORED human-unlock ledger (railway login, X app enrollment) —
surfaced every tick, auto-cleared where a probe can detect resolution.

ACTIONS (one per tick, worst lane, all dispatch EXISTING endpoints)
-------------------------------------------------------------------
  demand     → POST /api/v1/admin/audience/master-tick
  conversion → flag (URL-elicitation experiment is running; watch [url-elicit])
  citations  → POST /api/v1/admin/media/master-tick
  search     → POST /api/v1/admin/indexnow?recent=1 (only when stale >48h)
  retention  → flag (r-return + durable identity own this)

SAFETY
------
  admin-gated (X-Admin-Key) · GAPS_MASTER_DISABLED kill · GAPS_MASTER_ACT_DISABLED
  shadow mode · per-lane GAPS_LANE_<NAME>_OFF · never posts externally itself ·
  every probe try/except (a dead source scores 0, never 500s the tick).

ROUTES
------
  POST/GET /api/v1/admin/gaps/master-tick — measure → score → act → persist → verify
  GET      /api/v1/admin/gaps/state       — latest snapshots (trend)
  GET      /admin/gaps                    — HTML dashboard (?admin_key=)
"""
from __future__ import annotations

import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify, request
from routes._swallowed_writes import note_swallowed_write

gap_master_shell_bp = Blueprint("gap_master_shell", __name__)

# Loopback base (mirrors growth_master_shell): self-calls through CF would make
# a multi-probe tick blow past CF's ~15s proxy limit.
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE", "https://dchub-backend-production.up.railway.app")
)
_GEO_BASE = "https://dchub.cloud"

_LANES = ("demand", "conversion", "citations", "search", "retention")


# ── auth / kills (house pattern) ──────────────────────────────────────
def _admin_key():
    return os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("DCHUB_INTERNAL_KEY")


def _admin_ok() -> bool:
    expected = (_admin_key() or "").strip()
    if not expected:
        return False
    got = (request.headers.get("X-Admin-Key")
           or request.args.get("admin_key") or "").strip()
    return bool(got) and hmac.compare_digest(got, expected)


def _disabled() -> bool:
    return str(os.environ.get("GAPS_MASTER_DISABLED", "")).lower() in ("1", "true", "yes")


def _act_disabled() -> bool:
    return str(os.environ.get("GAPS_MASTER_ACT_DISABLED", "")).lower() in ("1", "true", "yes")


def _lane_off(name: str) -> bool:
    return str(os.environ.get(f"GAPS_LANE_{name.upper()}_OFF", "")).lower() in ("1", "true", "yes")


# ── self-call helpers (mirrors growth_master_shell) ───────────────────
def _req(path: str, method: str = "GET", timeout: int = 10) -> dict:
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=(b"" if method == "POST" else None), method=method)
        req.add_header("X-DC-Probe", "gap-tick")
        req.add_header("User-Agent", "dchub-gap-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = {"_raw": body[:300]}
            return {"ok": True, "http": resp.status, "data": parsed}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "http": None, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def _fire(path: str, timeout: int = 4) -> dict:
    """Dispatch a downstream action; short timeout = 'dispatched', the endpoint
    finishes server-side regardless (growth-shell convention)."""
    url = (path if path.startswith("http") else _BACKEND_BASE.rstrip("/") + path)
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("X-DC-Probe", "gap-tick")
        req.add_header("User-Agent", "dchub-gap-orchestrator/1.0")
        ak = _admin_key()
        if ak:
            req.add_header("X-Admin-Key", ak)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": resp.status < 400, "http": resp.status, "dispatched": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "dispatched": True}
    except Exception:
        return {"ok": True, "dispatched": True, "note": "not awaited"}


def _fetch_text(url: str, timeout: int = 12):
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "dchub-gap-orchestrator/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ── DB ────────────────────────────────────────────────────────────────
def _conn():
    try:
        from routes.ai_reach import _conn as _raw
        return _raw()
    except Exception:
        return None


def _q1(sql: str):
    """One-row query; returns tuple or None. Never raises."""
    c = _conn()
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _ensure_tables() -> bool:
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gap_snapshots (
                    id           SERIAL PRIMARY KEY,
                    computed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    overall      NUMERIC(6,2),
                    worst_lane   TEXT,
                    action_taken TEXT,
                    lane_scores  JSONB,
                    detail       JSONB
                )
            """)
        return True
    except Exception:
        return False
    finally:
        try: c.close()
        except Exception: pass


# ── TIER 1+2 — MEASURE AND SCORE EACH LANE ────────────────────────────
def _lane_demand() -> dict:
    # qa-0704b: bound the scan to 30d — the unbounded MAX cold-scanned the whole
    # table (2.9s vs 0.21s bounded) and was the one probe that died on the first
    # live tick (demand read "error: db" while four lanes measured fine). A real
    # call older than 30d correctly reads as NULL → fresh=0.
    row = _q1("""
        SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at) FILTER (WHERE is_real_external)))/3600.0,
               COUNT(*) FILTER (WHERE is_real_external AND created_at > NOW() - INTERVAL '7 days'),
               COUNT(*) FILTER (WHERE is_real_external AND created_at <= NOW() - INTERVAL '7 days'
                                AND created_at > NOW() - INTERVAL '14 days')
        FROM mcp_calls_identity
        WHERE created_at > NOW() - INTERVAL '30 days'
    """)
    if not row:
        return {"score": 0.0, "error": "db"}
    hours_since = round(float(row[0]), 1) if row[0] is not None else None
    calls_7d, calls_prior7d = int(row[1] or 0), int(row[2] or 0)
    # Holiday/weekend-aware freshness: agent traffic follows human work hours.
    dow = datetime.now(timezone.utc).weekday()   # 0=Mon … 6=Sun
    threshold_h = 54.0 if dow in (5, 6) else 30.0
    if hours_since is None:
        fresh = 0.0
    elif hours_since <= 12:
        fresh = 1.0
    else:
        fresh = max(0.0, 1.0 - (hours_since - 12) / (2 * threshold_h - 12))
    trend = min(1.0, calls_7d / max(1.0, float(calls_prior7d))) if calls_prior7d else (1.0 if calls_7d else 0.0)
    return {"score": round(0.6 * fresh + 0.4 * trend, 3),
            "hours_since_last_real_call": hours_since, "threshold_h": threshold_h,
            "real_calls_7d": calls_7d, "real_calls_prior_7d": calls_prior7d}


def _lane_conversion() -> dict:
    sd = (_req("/api/v1/admin/mcp/high-intent/step-drop").get("data") or {})
    paywall = int(_num(sd.get("paywall_sessions")) or 0)
    paid = int(_num(sd.get("paid_total")) or 0)
    redeemed = 0
    for st in (sd.get("steps") or []):
        if "redeem" in str(st.get("step", "")).lower():
            redeemed = int(_num(st.get("count")) or 0)
            break
    row = _q1("SELECT COUNT(*) FROM mcp_session_upgrades")
    session_upgrades = int(row[0]) if row else 0
    # 1.0 needs a real paid close; redemptions are the leading indicator.
    score = (0.15 * (1.0 if paywall else 0.0)
             + 0.35 * min(1.0, redeemed / 5.0)
             + 0.25 * (1.0 if session_upgrades > 0 else 0.0)
             + 0.25 * (1.0 if paid > 0 else 0.0))
    return {"score": round(score, 3), "paywall_sessions_30d": paywall,
            "claims_redeemed_30d": redeemed, "session_upgrades_total": session_upgrades,
            "paid_30d": paid}


def _lane_citations() -> dict:
    north = (_req("/api/v1/media/north-star").get("data") or {})
    v7 = int(_num(north.get("citation_velocity_7d")) or 0)
    v30 = int(_num(north.get("citation_velocity_30d")) or 0)
    days_since = None
    try:
        recent = north.get("recent") or []
        if recent:
            ts = str(recent[0].get("at") or "").replace("Z", "+00:00")
            days_since = round((datetime.now(timezone.utc)
                                - datetime.fromisoformat(ts)).total_seconds() / 86400.0, 1)
    except Exception:
        pass
    if not north:
        score = 0.0
    elif v7 > 0:
        score = 1.0
    elif v30 > 0 and (days_since is None or days_since <= 21):
        score = 0.5
    else:
        score = 0.15
    return {"score": score, "citation_velocity_7d": v7, "citation_velocity_30d": v30,
            "days_since_last_citation": days_since}


def _lane_search() -> dict:
    idx = (_req("/api/v1/admin/indexnow").get("data") or {})
    last = idx.get("last") or {}
    hours_since_submit = None
    try:
        ts = str(last.get("at") or "").replace("Z", "+00:00")
        if ts:
            hours_since_submit = round((datetime.now(timezone.utc)
                                        - datetime.fromisoformat(ts)).total_seconds() / 3600.0, 1)
    except Exception:
        pass
    idx_fresh = 1.0 if (hours_since_submit is not None and hours_since_submit < 48) else 0.0
    idx_ok = 1.0 if _num(last.get("status")) == 200 else 0.0
    code_sm, sitemap = _fetch_text(_GEO_BASE + "/sitemap.xml")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sm_fresh = 1.0 if (code_sm == 200 and today in (sitemap or "")) else 0.0
    code_rb, _ = _fetch_text(_GEO_BASE + "/robots.txt")
    rb_ok = 1.0 if code_rb == 200 else 0.0
    return {"score": round(0.4 * idx_fresh + 0.2 * idx_ok + 0.2 * sm_fresh + 0.2 * rb_ok, 3),
            "indexnow_hours_since_submit": hours_since_submit,
            "indexnow_last_status": last.get("status"),
            "sitemap_fresh_today": bool(sm_fresh), "robots_http": code_rb}


def _lane_retention() -> dict:
    row = _q1("""
        WITH real30 AS (
            SELECT agent_id, COUNT(DISTINCT created_at::date) AS days
            FROM mcp_calls_identity
            WHERE is_real_external AND created_at > NOW() - INTERVAL '30 days'
              AND agent_id IS NOT NULL
            GROUP BY agent_id
        )
        SELECT COUNT(*) FILTER (WHERE days >= 2), COUNT(*),
               (SELECT COUNT(DISTINCT a.ip_address) FROM mcp_calls_identity a
                 WHERE a.is_real_external AND a.created_at > NOW() - INTERVAL '7 days'
                   AND EXISTS (SELECT 1 FROM mcp_calls_identity b
                                WHERE b.is_real_external AND b.ip_address = a.ip_address
                                  AND b.created_at <= NOW() - INTERVAL '7 days'
                                  AND b.created_at > NOW() - INTERVAL '28 days'))
        FROM real30
    """)
    if not row:
        return {"score": 0.0, "error": "db"}
    multi, total, returning = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    rate = (multi / total) if total else 0.0
    score = 0.6 * min(1.0, rate / 0.30) + 0.4 * min(1.0, returning / 20.0)
    return {"score": round(score, 3), "multi_day_agents_30d": multi,
            "agents_30d": total, "multi_day_rate": round(rate, 3),
            "returning_ips_7d": returning}


def _unlock_ledger() -> list:
    """Human blockers surfaced every tick; auto-cleared where detectable."""
    ledger = []
    pub = (_req("/api/v1/dchub-media/publisher-status").get("data") or {})
    tw = ((pub.get("loops") or {}).get("twitter") or {})
    x_active = bool(_num(tw.get("attempts_24h")) or _num(tw.get("successes_24h")))
    ledger.append({"unlock": "x_app_enrollment",
                   "cleared": x_active,
                   "note": "X app 33102505 needs a Project; posting queue capped until enrolled."})
    ledger.append({"unlock": "railway_mcp_login",
                   "cleared": None,   # not server-detectable
                   "note": "local `railway login` expired 07-04 — blocks log/deploy introspection "
                           "from assistant sessions (incl. [url-elicit] telemetry reads)."})
    return ledger


def measure_all() -> dict:
    lanes = {
        "demand": _lane_demand(),
        "conversion": _lane_conversion(),
        "citations": _lane_citations(),
        "search": _lane_search(),
        "retention": _lane_retention(),
    }
    scores = {k: (v.get("score") or 0.0) for k, v in lanes.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1])
    worst = next((n for n, _ in ranked if not _lane_off(n)), ranked[0][0])
    return {"lanes": lanes, "scores": scores, "worst": worst,
            "unlocks": _unlock_ledger()}


# ── TIER 3 — ONE BOUNDED ACTION ON THE WORST LANE ─────────────────────
def act(m: dict) -> dict:
    if _act_disabled():
        return {"action": "none", "reason": "GAPS_MASTER_ACT_DISABLED (shadow mode)"}
    lane = m.get("worst")
    if _lane_off(lane):
        return {"action": "none", "reason": f"lane '{lane}' killed"}

    if lane == "demand":
        r = _fire("/api/v1/admin/audience/master-tick")
        return {"action": "audience_master_tick", "lane": lane, "dispatched": r.get("dispatched")}

    if lane == "citations":
        r = _fire("/api/v1/admin/media/master-tick")
        return {"action": "media_master_tick", "lane": lane, "dispatched": r.get("dispatched")}

    if lane == "search":
        hrs = (m["lanes"]["search"] or {}).get("indexnow_hours_since_submit")
        if hrs is None or hrs >= 48:
            r = _fire("/api/v1/admin/indexnow?recent=1", timeout=8)
            return {"action": "indexnow_recent_submit", "lane": lane, "dispatched": r.get("dispatched")}
        return {"action": "flag", "lane": lane,
                "flag": "IndexNow fresh — remaining search gap is crawl-side; watch BWT weekly."}

    if lane == "conversion":
        return {"action": "flag", "lane": lane,
                "flag": "URL-elicitation experiment live (mcp-server b3a3f24) — watch [url-elicit] "
                        "logs + first mcp_session_upgrades row. No safe actuator to fire."}

    if lane == "retention":
        return {"action": "flag", "lane": lane,
                "flag": "r-return hook + durable identity own this; needs elicit/claim funnel to "
                        "produce its first durable binding."}

    return {"action": "none", "reason": f"unknown lane {lane}"}


# ── PERSIST + VERIFY ──────────────────────────────────────────────────
def _persist(m: dict, overall: float, action: dict) -> bool:
    if not _ensure_tables():
        return False
    c = _conn()
    if c is None:
        return False
    try:
        with c.cursor() as cur:
            cur.execute("""
                INSERT INTO gap_snapshots (overall, worst_lane, action_taken, lane_scores, detail)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """, (overall, m.get("worst"), (action or {}).get("action"),
                  json.dumps(m.get("scores") or {}),
                  json.dumps({"lanes": m.get("lanes"), "unlocks": m.get("unlocks"),
                              "action": action})))
        return True
    except Exception:
        note_swallowed_write("gap_snapshots", where="gap_master_shell._persist")
        return False
    finally:
        try: c.close()
        except Exception: pass


def _prev() -> tuple:
    row = _q1("SELECT computed_at, overall, worst_lane FROM gap_snapshots ORDER BY id DESC LIMIT 1")
    return row


def verify(overall: float) -> dict:
    prev = _prev()
    if not prev:
        return {"baseline": True, "note": "first gap snapshot — deltas start next tick."}
    try:
        delta = round(float(overall) - float(prev[1]), 2)
    except (TypeError, ValueError):
        delta = None
    return {"baseline": False, "since": str(prev[0]), "prev_worst": prev[2],
            "delta_overall": delta}


# ── ROUTES ────────────────────────────────────────────────────────────
@gap_master_shell_bp.route("/api/v1/admin/gaps/master-tick", methods=["POST", "GET"])
def gaps_master_tick():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    if _disabled():
        return jsonify(skipped="GAPS_MASTER_DISABLED"), 200
    started = time.time()
    m = measure_all()
    overall = round(100.0 * sum(m["scores"].values()) / max(1, len(m["scores"])), 2)
    action = act(m)
    v = verify(overall)
    persisted = _persist(m, overall, action)
    worst = m.get("worst")
    headline = (f"gaps {overall}/100 · worst → {worst} ({m['scores'].get(worst)}) · "
                f"acted: {action.get('action')}")
    return jsonify(ok=True, generated_at=datetime.now(timezone.utc).isoformat(),
                   overall=overall, headline=headline, scores=m["scores"],
                   worst=worst, lanes=m["lanes"], unlocks=m["unlocks"],
                   action=action, verify=v, persisted=persisted,
                   ms=int((time.time() - started) * 1000)), 200


@gap_master_shell_bp.route("/api/v1/admin/gaps/state", methods=["GET"])
def gaps_state():
    if not _admin_ok():
        return jsonify(error="unauthorized"), 401
    c = _conn()
    rows = []
    if c is not None:
        try:
            with c.cursor() as cur:
                cur.execute("""SELECT computed_at, overall, worst_lane, action_taken, lane_scores
                               FROM gap_snapshots ORDER BY id DESC LIMIT 20""")
                for r in cur.fetchall():
                    rows.append({"at": str(r[0]), "overall": float(r[1]) if r[1] is not None else None,
                                 "worst": r[2], "action": r[3], "scores": r[4]})
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass
    return jsonify(ok=True, snapshots=rows), 200


@gap_master_shell_bp.route("/admin/gaps", methods=["GET"])
def gaps_dashboard():
    if not _admin_ok():
        return Response("<h3>unauthorized — pass ?admin_key=</h3>", mimetype="text/html", status=401)
    m = measure_all()
    overall = round(100.0 * sum(m["scores"].values()) / max(1, len(m["scores"])), 2)
    bars = ""
    for lane in _LANES:
        s = m["scores"].get(lane, 0.0)
        pct = int(s * 100)
        color = "#22c55e" if s >= 0.8 else ("#eab308" if s >= 0.5 else "#ef4444")
        detail = json.dumps({k: v for k, v in (m["lanes"].get(lane) or {}).items() if k != "score"})[:220]
        bars += (f"<div style='margin:10px 0'><b>{lane}</b> — {s}"
                 f"<div style='background:#1e293b;border-radius:6px;height:10px;margin:4px 0'>"
                 f"<div style='width:{pct}%;background:{color};height:10px;border-radius:6px'></div></div>"
                 f"<div style='color:#64748b;font-size:.75rem'>{detail}</div></div>")
    unlocks = "".join(
        f"<li>{'✅' if u['cleared'] else ('🟡' if u['cleared'] is None else '⛔')} "
        f"<b>{u['unlock']}</b> — {u['note']}</li>" for u in m["unlocks"])
    html = (f"<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='300'>"
            f"<title>Gap Master Shell · DC Hub</title>"
            f"<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,sans-serif;"
            f"max-width:820px;margin:24px auto;padding:0 16px'>"
            f"<h2>Gap Master Shell <span style='color:#22d3ee'>{overall}/100</span></h2>"
            f"<p style='color:#94a3b8'>worst lane → <b>{m['worst']}</b> · five gaps from the 07-04 QA "
            f"assessment · one bounded action per tick · cron every 6h</p>{bars}"
            f"<h3>Human unlocks</h3><ul>{unlocks}</ul>"
            f"<p style='color:#475569;font-size:.75rem'>routes/gap_master_shell.py · "
            f"/api/v1/admin/gaps/master-tick · GAPS_MASTER_ACT_DISABLED=1 for shadow</p>")
    return Response(html, mimetype="text/html")
