"""
routes/agents_portal.py — /admin/agents: ONE portal for the agent-enablement stack (2026-07-03).

The agent-enablement machinery lives in three shells + one tuner, each with
JSON-only endpoints and its own snapshot table. This portal is the single
HTML pane the operator asked for — "where do I inspect it in action":

  · NORTH STAR   — distinct real external AI agents/week (mcp_calls_identity,
                   is_real_external) — the number the whole stack serves.
  · ONBOARDING   — agent_onboarding_master_shell: latest 0-100 score,
                   per-platform dims, ranked next-action worklist
                   (daily cron agent_onboarding_master_tick_daily).
  · ADOPTION     — ai_adoption_master_shell: distinct agents 7d + WoW delta,
                   registry coverage, self-surfaces, drafts queued.
  · TOOL TUNER   — mcp_tool_descriptions_per_platform freshness: the
                   per-platform tool descriptions served to tools/list.
                   Flags STALE when the newest row is older than 14 days
                   (seeded 06-07, no refresh loop wired as of 07-03).

READ-ONLY: renders existing snapshot tables + one identity query. It never
ticks the shells itself — each card links to that shell's master-tick URL
(admin key passed through from this request) so "run now" stays deliberate.

Routes:
  GET /admin/agents               HTML portal (60s cache, auto-refresh 120s)
  GET /api/v1/admin/agents        CF zone-worker bypass alias (same HTML)
  GET /api/v1/admin/agents/state  JSON payload behind the portal

Auth: X-Admin-Key or ?admin_key= vs DCHUB_ADMIN_KEY (fallback DCHUB_INTERNAL_KEY).
Kill switch: AGENTS_PORTAL_DISABLED=1.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

agents_portal_bp = Blueprint("agents_portal", __name__)

_TUNER_STALE_DAYS = 14


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("AGENTS_PORTAL_DISABLED") or "").strip() == "1"


def _conn():
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[agents_portal] db connect failed: %s", e)
        return None


def _rows(c, sql: str):
    """Fail-soft list-of-tuples. [] on any error (missing table must not
    blank the portal — each card renders its own 'unavailable' state)."""
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[agents_portal] query failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return []


# ── data assembly ─────────────────────────────────────────────────────

def _build_state() -> dict:
    c = _conn()
    out: dict = {"ok": c is not None,
                 "generated_at": datetime.now(timezone.utc).isoformat()}
    if c is None:
        return out

    # NORTH STAR — real external agents/wk, last 8 weeks. The most recent bucket is
    # the IN-PROGRESS calendar week (only a few days elapsed) — flag it `partial` so
    # the UI can dim it and never read "33 vs 84" as a WoW drop (2026-07-23).
    from datetime import timedelta as _td
    _cur_wk_monday = (datetime.now(timezone.utc).date()
                      - _td(days=datetime.now(timezone.utc).weekday()))
    def _is_partial(v):
        try:
            d = v if hasattr(v, "year") else datetime.fromisoformat(str(v)[:10]).date()
            return d >= _cur_wk_monday
        except Exception:
            return False
    out["north_star"] = [
        {"week": str(r[0]), "agents": int(r[1] or 0), "calls": int(r[2] or 0),
         "partial": _is_partial(r[0])}
        for r in _rows(c, """
            SELECT date_trunc('week', created_at)::date,
                   count(DISTINCT agent_id) FILTER (WHERE is_real_external),
                   count(*) FILTER (WHERE is_real_external)
            FROM mcp_calls_identity
            WHERE created_at > now() - interval '8 weeks'
            GROUP BY 1 ORDER BY 1""")]

    # ONBOARDING — latest snapshot + trend
    ob = _rows(c, "SELECT score, platforms_json, worklist_json, created_at "
                  "FROM agent_onboarding_snapshots ORDER BY id DESC LIMIT 1")
    if ob:
        score, plats, work, ts = ob[0]
        try:
            plats = plats if isinstance(plats, (list, dict)) else json.loads(plats or "[]")
        except Exception:
            plats = []
        try:
            work = work if isinstance(work, (list, dict)) else json.loads(work or "[]")
        except Exception:
            work = []
        out["onboarding"] = {
            "score": float(score) if score is not None else None,
            "as_of": str(ts),
            "platforms": [
                {"name": p.get("name") or p.get("key"), "score": p.get("score"),
                 "weak": min((p.get("dims") or {}).items(),
                             key=lambda kv: kv[1], default=(None, None))[0]}
                for p in (plats if isinstance(plats, list) else [])[:14]],
            "worklist": [
                {"key": w.get("key"), "action": (w.get("action") or "")[:220],
                 "effort": w.get("effort")}
                for w in (work if isinstance(work, list) else [])[:6]],
            "trend": [{"at": str(r[1]), "score": float(r[0] or 0)} for r in _rows(
                c, "SELECT score, created_at FROM agent_onboarding_snapshots "
                   "ORDER BY id DESC LIMIT 14")],
        }

    # ADOPTION — latest snapshot
    ad = _rows(c, "SELECT computed_at, distinct_agents_7d, agents_wow_delta, "
                  "distinct_platforms, registry_covered, registry_total, "
                  "self_surfaces_ok, self_surfaces_total, drafts_queued, "
                  "submitted, top_platform "
                  "FROM ai_adoption_snapshots ORDER BY id DESC LIMIT 1")
    if ad:
        r = ad[0]
        out["adoption"] = {
            "as_of": str(r[0]), "distinct_agents_7d": r[1], "wow_delta": r[2],
            "distinct_platforms": r[3],
            "registry": f"{r[4]}/{r[5]}", "self_surfaces": f"{r[6]}/{r[7]}",
            "drafts_queued": r[8], "submitted": r[9], "top_platform": r[10],
        }

    # TOOL TUNER — coverage + freshness
    tu = _rows(c, "SELECT platform, count(*), max(updated_at) "
                  "FROM mcp_tool_descriptions_per_platform GROUP BY 1 ORDER BY 1")
    newest = max((r[2] for r in tu), default=None)
    stale = None
    if newest is not None:
        try:
            age = datetime.now(timezone.utc) - newest
            stale = age.days >= _TUNER_STALE_DAYS
        except Exception:
            stale = None
    out["tool_tuner"] = {
        "platforms": [{"platform": r[0], "tools": int(r[1]),
                       "updated": str(r[2])[:19]} for r in tu],
        "newest": str(newest)[:19] if newest else None,
        "stale": stale,
        "stale_after_days": _TUNER_STALE_DAYS,
    }

    try:
        c.close()
    except Exception:
        pass
    return out


_cache: dict = {"ts": 0.0, "payload": None}
_lock = threading.Lock()
_TTL = 60.0


def _state_cached() -> dict:
    with _lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TTL:
            return _cache["payload"]
    p = _build_state()
    with _lock:
        _cache["ts"], _cache["payload"] = time.time(), p
    return p


# ── routes ────────────────────────────────────────────────────────────

@agents_portal_bp.route("/api/v1/admin/agents/state", methods=["GET"])
def agents_state():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    return jsonify(_state_cached())


@agents_portal_bp.route("/admin/agents", methods=["GET"])
@agents_portal_bp.route("/api/v1/admin/agents", methods=["GET"])
def agents_portal():
    if _disabled():
        return Response("agents portal disabled", status=503)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _state_cached()
    key = (request.args.get("admin_key") or "").strip()
    qs = f"?admin_key={_esc(key)}" if key else ""

    def card(title, inner, sub=""):
        return (f"<div style='background:#0f172a;border:1px solid #334155;"
                f"border-radius:12px;padding:16px;margin:12px 0'>"
                f"<div style='font-weight:700;font-size:15px'>{title}</div>"
                f"{('<div style=&quot;color:#64748b;font-size:12px&quot;>' + sub + '</div>') if sub else ''}"
                f"{inner}</div>")

    # north star sparkline as a plain bar row (no JS, renders anywhere)
    ns = p.get("north_star") or []
    peak = max((w["agents"] for w in ns), default=1) or 1
    bars = "".join(
        f"<div style='display:inline-block;width:44px;margin-right:6px;text-align:center'>"
        f"<div title='{'in-progress week — partial, not comparable' if w.get('partial') else ''}' "
        f"style='background:{'#334155' if w.get('partial') else '#38bdf8'};"
        f"height:{max(4, int(60 * w['agents'] / peak))}px;border-radius:3px 3px 0 0'></div>"
        f"<div style='font-size:11px;color:{'#64748b' if w.get('partial') else '#e2e8f0'}'>"
        f"{w['agents']}{'*' if w.get('partial') else ''}</div>"
        f"<div style='font-size:10px;color:#64748b'>{w['week'][5:]}</div></div>"
        for w in ns)
    ns_html = card("🌟 North star — distinct real external agents / week",
                   f"<div style='display:flex;align-items:flex-end;margin-top:10px'>{bars}</div>",
                   "mcp_calls_identity · is_real_external only · current week is partial")

    ob = p.get("onboarding")
    if ob:
        prows = "".join(
            f"<tr><td style='padding:3px 8px'>{_esc(str(pl['name']))}</td>"
            f"<td style='padding:3px 8px;text-align:right'>{pl['score']}</td>"
            f"<td style='padding:3px 8px;color:#94a3b8'>weakest: {_esc(str(pl['weak']))}</td></tr>"
            for pl in ob["platforms"])
        wrows = "".join(
            f"<li style='margin:4px 0'><b>{_esc(str(w['key']))}</b>"
            f"{(' · ' + _esc(str(w['effort']))) if w.get('effort') else ''} — "
            f"{_esc(str(w['action']))}</li>" for w in ob["worklist"])
        trend = " → ".join(str(round(t["score"], 1)) for t in reversed(ob["trend"]))
        ob_html = card(
            f"🧭 Onboarding shell — score {round(ob['score'] or 0, 1)}/100",
            f"<table style='font-size:13px;border-collapse:collapse;margin-top:8px'>{prows}</table>"
            f"<div style='margin-top:10px;font-size:13px;color:#e2e8f0'>Next actions:"
            f"<ul style='margin:4px 0 0 18px;padding:0'>{wrows}</ul></div>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>trend: {trend}</div>"
            f"<div style='margin-top:6px;font-size:12px'>"
            f"<a style='color:#38bdf8' href='/api/v1/admin/agent-onboarding/master-tick{qs}'>run tick now</a> · "
            f"<a style='color:#38bdf8' href='/api/v1/admin/agent-onboarding/state{qs}'>raw state</a></div>",
            f"daily cron · last tick {ob['as_of'][:19]}")
    else:
        ob_html = card("🧭 Onboarding shell", "<div style='color:#94a3b8'>no snapshots yet</div>")

    ad = p.get("adoption")
    if ad:
        delta = ad["wow_delta"]
        dcol = "#22c55e" if (delta or 0) >= 0 else "#ef4444"
        ad_html = card(
            "📈 AI-Adoption shell",
            f"<div style='margin-top:8px;font-size:14px'>distinct agents 7d: "
            f"<b>{ad['distinct_agents_7d']}</b> "
            f"<span style='color:{dcol}'>({'+' if (delta or 0) >= 0 else ''}{delta} WoW)</span>"
            f" · platforms: {ad['distinct_platforms']}"
            f" · registries: {ad['registry']}"
            f" · self-surfaces: {ad['self_surfaces']}"
            f" · drafts queued: {ad['drafts_queued']}"
            f" · top: {_esc(str(ad['top_platform']))}</div>",
            f"last tick {ad['as_of'][:19]}")
    else:
        ad_html = card("📈 AI-Adoption shell", "<div style='color:#94a3b8'>no snapshots yet</div>")

    tt = p.get("tool_tuner") or {}
    trows = "".join(
        f"<tr><td style='padding:3px 8px'>{_esc(r['platform'])}</td>"
        f"<td style='padding:3px 8px;text-align:right'>{r['tools']}</td>"
        f"<td style='padding:3px 8px;color:#94a3b8'>{r['updated']}</td></tr>"
        for r in tt.get("platforms", []))
    badge = ("<span style='color:#ef4444;font-weight:700'>STALE</span>"
             if tt.get("stale") else
             ("<span style='color:#22c55e'>fresh</span>" if tt.get("stale") is False
              else "<span style='color:#eab308'>?</span>"))
    tt_html = card(
        f"🛠 Per-platform tool-description tuner — {badge}",
        f"<table style='font-size:13px;border-collapse:collapse;margin-top:8px'>"
        f"<tr style='color:#64748b'><td style='padding:3px 8px'>platform</td>"
        f"<td style='padding:3px 8px'>tools</td><td style='padding:3px 8px'>updated</td></tr>"
        f"{trows}</table>"
        f"<div style='margin-top:6px;font-size:12px'>"
        f"<a style='color:#38bdf8' href='/api/v1/admin/mcp/tool-tuner/coverage{qs}'>coverage</a> · "
        f"POST /api/v1/admin/mcp/tool-tuner/seed to regenerate</div>",
        f"newest row {tt.get('newest')} · stale after {tt.get('stale_after_days')}d · "
        f"served to tools/list, refreshed by mcp-server every 30min")

    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='120'>"
        "<title>Agent Enablement · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        "<h2 style='margin:0 0 4px'>Agent-Enablement Portal</h2>"
        f"<div style='color:#64748b;font-size:12px'>onboarding + adoption + tuner in one pane · "
        f"read-only · 60s cache · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/agents/state</div>"
        + ns_html + ob_html + ad_html + tt_html + "</body>")
    return Response(html, mimetype="text/html")
