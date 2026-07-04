"""
routes/fixwave_master_shell.py — Fix-Wave Master Shell (2026-07-03 deep-dive wave).

One pane that live-probes the FIVE fix lanes spawned by the 07-03 flywheel/QA
deep dive and scores each PASS/FAIL, so the operator can watch the wave land
in real time and keep the checks as a standing regression sentinel afterward.

The five lanes (one per fix session):
  1. MCP-RELIABILITY — tools/list must render for a keyless Claude connector
     (WorkOS challenge exemption) + stale-session tools/call must 404 (spec)
     so clients auto-reinitialize.
  2. RAG — /api/v1/rag/search keyless capped at k<=3 (the gate); market
     context packs live; market_deep_dives chunks embedded.
  3. FRONTEND — residual map.on guards shipped; early preconnects on
     /land-power-map; home no longer eager-loads the full map iframe.
  4. SEO — homepage links /facilities/directory; /map + /ai titles rewritten
     off the frozen 07-03 baselines; legacy /facility/<id> 301 stays healthy;
     directory stays link-rich for Googlebot (regression guard).
  5. FUNNEL-HONESTY — step-drop Branch A counts DISTINCT keys (parity with
     DB); paywall "real" count excludes QA probes + the clawith scraper;
     pair codes stop bot-minting on bare page view.

Design mirrors the house master-shell pattern: admin-gated, killable
(FIXWAVE_DISABLED=1), loopback self-calls for backend-own endpoints, every
probe fail-soft + timeout-bounded, snapshot row per tick, 30s cache.
READ-ONLY — this shell never mutates product state; it only measures.

Endpoints:
  GET/POST /api/v1/admin/fixwave/master-tick   JSON scoreboard (5 lanes)
  GET      /admin/fixwave                       HTML dashboard (60s refresh)
  GET      /api/v1/admin/fixwave                CF zone-worker bypass alias

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (falls back to
DCHUB_INTERNAL_KEY) — same gate as growth_master_shell / funnel_health.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

fixwave_master_shell_bp = Blueprint("fixwave_master_shell", __name__)

# ── targets ───────────────────────────────────────────────────────────
# The MCP server is probed at its Railway origin (what the fix session
# verifies against) — the CF worker in front adds variance we don't want
# in a mechanical PASS/FAIL. Public site surfaces are probed through the
# edge because that's where the leak/regression actually lives.
_MCP_ORIGIN = os.environ.get(
    "FIXWAVE_MCP_ORIGIN",
    "https://dchub-mcp-server-production-4d2e.up.railway.app").rstrip("/")
_EDGE = "https://dchub.cloud"
# Loopback for backend-own admin endpoints (mirrors growth_master_shell —
# public round-trips through CF would 503 a fan-out tick past ~15s).
_BACKEND_BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else os.environ.get("DCHUB_BACKEND_BASE",
                        "https://dchub-backend-production.up.railway.app"))

_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

# Frozen 2026-07-03 pre-fix baselines: the SEO lane passes when the live
# title DIFFERS from these (i.e. the rewrite shipped).
_TITLE_BASELINES = {
    "/map": "Facility Map — DC Hub Intelligence",
    "/ai":  "AI Platform | DC Hub — Data Center Intelligence for Every AI Agent",
}

# Internal/QA clients + known scraper that must NOT count as "real"
# high-intent prospects (07-03 finding: clawith = python-httpx, 95 rotating
# IPs; gating-audit / fix2-verify / hi-claim-test / render-verify = our own probes).
_NON_REAL_CLIENTS = ("clawith", "gating-audit", "fix2-verify", "hi-claim-test",
                     "clientx-friction-audit", "p", "v")

# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("FIXWAVE_DISABLED") or "").strip() == "1"


# ── helpers ───────────────────────────────────────────────────────────

def _http(url: str, *, method: str = "GET", headers: dict | None = None,
          body: dict | None = None, timeout: float = 6.0) -> tuple[int, str, int]:
    """Bounded fetch. Returns (status, body_text[:200k], elapsed_ms).
    Never raises — network failure returns (0, err, ms)."""
    t0 = time.time()
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", _BROWSER_UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)  # add_header keys by .capitalize() → same-key overrides UA default
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read(200_000).decode("utf-8", "replace"), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read(50_000).decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, int((time.time() - t0) * 1000)
    except Exception as e:  # DNS, timeout, TLS, conn-refused
        return 0, f"{type(e).__name__}: {e}", int((time.time() - t0) * 1000)


def _conn():
    """Raw psycopg2 connection (mirrors funnel_health._conn). None on failure."""
    try:
        import psycopg2 as _pg
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[fixwave] db connect failed: %s", e)
        return None


def _scalar(c, sql: str, args: tuple | None = None):
    """Fail-soft scalar. None on error (NOT 0 — a probe must be able to tell
    'query broke' from 'count is zero'). Pass args=None for literal SQL so
    psycopg2 never %-substitutes (empty-tuple trap)."""
    try:
        with c.cursor() as cur:
            if args is None:
                cur.execute(sql)
            else:
                cur.execute(sql, args)
            row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.debug("[fixwave] scalar failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _check(cid: str, name: str, passed, detail: str, ms: int = 0) -> dict:
    # passed: True / False / None (None = indeterminate, shown as "?")
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:300], "ms": ms}


_CB = lambda: f"_fw={int(time.time())}"  # cache-buster for edge-cached surfaces


# ── lane 1 · MCP reliability ─────────────────────────────────────────

def _lane_mcp() -> list[dict]:
    out = []
    st, body, ms = _http(f"{_MCP_ORIGIN}/mcp", method="POST",
                         headers={"Accept": "application/json, text/event-stream",
                                  "User-Agent": "Claude-User"},
                         body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    ok = st == 200 and '"tools"' in body
    out.append(_check("mcp_tools_list_anon", "tools/list renders for keyless Claude connector",
                      ok, f"HTTP {st}" + ("" if ok else f" — {body[:80]}"), ms))
    out.append(_check("mcp_tools_list_latency", "tools/list latency < 2s",
                      (ms < 2000) if st else None, f"{ms}ms", ms))

    st2, body2, ms2 = _http(f"{_MCP_ORIGIN}/mcp", method="POST",
                            headers={"Accept": "application/json, text/event-stream",
                                     "mcp-session-id": f"fixwave-bogus-{int(time.time())}"},
                            body={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                  "params": {"name": "get_grid_scoreboard", "arguments": {}}})
    out.append(_check("mcp_stale_session_404", "stale-session tools/call returns 404 (spec) not 400",
                      st2 == 404, f"HTTP {st2}", ms2))
    return out


# ── lane 2 · RAG gate + context packs ────────────────────────────────

def _lane_rag(c) -> list[dict]:
    out = []
    st, body, ms = _http(f"{_EDGE}/api/v1/rag/search?q=texas+grid+capacity&k=15&{_CB()}")
    n = None
    try:
        n = len(json.loads(body).get("results", []))
    except Exception:
        pass
    if st != 200 or n is None:
        out.append(_check("rag_keyless_capped", "keyless /rag/search capped at k<=3",
                          None, f"HTTP {st}, unparseable", ms))
    else:
        out.append(_check("rag_keyless_capped", "keyless /rag/search capped at k<=3",
                          n <= 3, f"{n} results keyless (want <=3)", ms))

    st2, body2, ms2 = _http(f"{_EDGE}/api/v1/context/market/northern-virginia?max_tokens=800&{_CB()}")
    ok2 = st2 == 200 and '"sections"' in body2
    out.append(_check("rag_context_pack", "market context pack endpoint live",
                      ok2, f"HTTP {st2}", ms2))

    if c is not None:
        cnt = _scalar(c, "SELECT count(*) FROM brain_corpus_embeddings "
                         "WHERE source_table = 'market_deep_dives'")
        out.append(_check("rag_deepdives_embedded", "market_deep_dives chunks embedded",
                          (cnt or 0) > 0 if cnt is not None else None,
                          f"{cnt} chunks" if cnt is not None else "query failed"))
    else:
        out.append(_check("rag_deepdives_embedded", "market_deep_dives chunks embedded",
                          None, "no db"))
    return out


# ── lane 3 · frontend (map guards + LCP) ─────────────────────────────

def _lane_frontend() -> list[dict]:
    out = []
    st, js, ms = _http(f"{_EDGE}/js/land-power-search-fix.js?{_CB()}")
    guarded = bool(re.search(r"typeof\s+map\.on\s*===\s*['\"]function['\"]", js))
    out.append(_check("fe_searchfix_guard", "land-power-search-fix.js map.on guard shipped",
                      guarded if st == 200 else None, f"HTTP {st}", ms))

    st2, html, ms2 = _http(f"{_EDGE}/land-power-map?{_CB()}")
    if st2 == 200:
        head = html[:6000]
        out.append(_check("fe_preconnects", "early preconnects (cdnjs + tiles) on /land-power-map",
                          ("preconnect" in head and "cdnjs.cloudflare.com" in head),
                          "found in first 6KB" if ("preconnect" in head and "cdnjs.cloudflare.com" in head)
                          else "not in first 6KB of <head>", ms2))
        fire_guarded = ("typeof map.fire" in html) or ("map.fire(" not in html)
        out.append(_check("fe_mapfire_guard", "inline map.fire / map.on handlers guarded",
                          fire_guarded, "guarded" if fire_guarded else "unguarded map.fire( present", ms2))
    else:
        out.append(_check("fe_preconnects", "early preconnects on /land-power-map", None, f"HTTP {st2}", ms2))
        out.append(_check("fe_mapfire_guard", "inline map.fire guarded", None, f"HTTP {st2}", ms2))

    st3, home, ms3 = _http(f"{_EDGE}/?{_CB()}")
    if st3 == 200:
        eager = re.search(r"rootMargin:\s*['\"]400px['\"]", home) is not None
        out.append(_check("fe_home_iframe", "home no longer eager-loads full map iframe",
                          not eager, "rootMargin 400px still present" if eager else "eager preload gone", ms3))
    else:
        out.append(_check("fe_home_iframe", "home no longer eager-loads full map iframe",
                          None, f"HTTP {st3}", ms3))
    return out


# ── lane 4 · SEO ──────────────────────────────────────────────────────

def _lane_seo() -> list[dict]:
    out = []
    st, home, ms = _http(f"{_EDGE}/?{_CB()}")
    linked = st == 200 and "facilities/directory" in home
    out.append(_check("seo_directory_linked", "homepage links /facilities/directory",
                      linked if st == 200 else None,
                      "linked" if linked else f"no link (HTTP {st})", ms))

    for path, baseline in _TITLE_BASELINES.items():
        st2, html, ms2 = _http(f"{_EDGE}{path}?{_CB()}")
        m = re.search(r"<title>([^<]*)</title>", html or "")
        title = (m.group(1).strip() if m else "")
        if st2 != 200 or not title:
            out.append(_check(f"seo_title_{path.strip('/')}", f"{path} title rewritten",
                              None, f"HTTP {st2}, no title parsed", ms2))
        else:
            out.append(_check(f"seo_title_{path.strip('/')}", f"{path} title rewritten",
                              title != baseline, f"『{title[:70]}』", ms2))

    st3, _, ms3 = _http(f"{_EDGE}/facility/1539", timeout=6.0)
    # urllib follows redirects; a healthy 301 chain ends 200 on the slug page.
    # Probe redirect explicitly: no-follow isn't native to urllib, so hit HEAD
    # via a Request subclass — simpler: accept 200 (followed) as healthy, and
    # flag 404/5xx which would mean the legacy chain broke.
    out.append(_check("seo_legacy_301", "legacy /facility/<id> redirect chain healthy",
                      st3 == 200, f"final HTTP {st3}", ms3))

    st4, dirhtml, ms4 = _http(f"{_EDGE}/facilities/directory?{_CB()}",
                              headers={"User-Agent": _GOOGLEBOT_UA})
    nlinks = dirhtml.count('href="/facilities/') if st4 == 200 else 0
    out.append(_check("seo_directory_links", "directory link-rich for Googlebot (>=200 links)",
                      (nlinks >= 200) if st4 == 200 else None, f"{nlinks} facility links", ms4))
    return out


# ── lane 5 · funnel honesty ──────────────────────────────────────────

def _lane_funnel(c) -> list[dict]:
    out = []
    admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "")
    st, body, ms = _http(f"{_BACKEND_BASE}/api/v1/admin/mcp/high-intent/step-drop?days=30",
                         headers={"X-Admin-Key": admin_key}, timeout=15.0)
    wf = None
    try:
        wf = json.loads(body)
    except Exception:
        pass

    if c is not None and wf and isinstance(wf.get("steps"), list):
        by_step = {s.get("step"): s.get("count") for s in wf["steps"]}
        db_distinct = _scalar(c,
            "SELECT count(DISTINCT minted_api_key) FROM mcp_high_intent_sessions "
            "WHERE claim_used_at > now() - interval '30 days' "
            "AND claim_email IS NULL AND minted_api_key IS NOT NULL")
        ep = by_step.get("agent_key_issued")
        if ep is None or db_distinct is None:
            out.append(_check("fn_waterfall_distinct", "Branch A key_issued counts DISTINCT keys",
                              None, f"endpoint={ep} db={db_distinct}", ms))
        else:
            # FAIL only in the artifact direction (rows > distinct keys).
            # endpoint <= distinct is fine — the endpoint legitimately strips
            # bot/QA redemptions that the raw DB count includes.
            out.append(_check("fn_waterfall_distinct", "Branch A key_issued counts DISTINCT keys",
                              int(ep) <= int(db_distinct),
                              f"endpoint={ep} vs db-distinct={db_distinct}", ms))

        clean = _scalar(c,
            "SELECT count(DISTINCT mcp_session_id) FROM mcp_high_intent_sessions "
            "WHERE first_hit_at > now() - interval '30 days' "
            "AND coalesce(mcp_client,'') <> ALL(%s)", (list(_NON_REAL_CLIENTS),))
        raw = _scalar(c,
            "SELECT count(DISTINCT mcp_session_id) FROM mcp_high_intent_sessions "
            "WHERE first_hit_at > now() - interval '30 days'")
        ep_hit = by_step.get("paywall_sessions")
        if None in (clean, raw, ep_hit):
            out.append(_check("fn_excludes_probes", "paywall 'real' count excludes QA probes + clawith",
                              None, f"endpoint={ep_hit} clean={clean} raw={raw}", 0))
        else:
            # PASS when the endpoint's "real" count is at or below the clean
            # count (tolerance +2 for window skew). If bots are quiet
            # (raw==clean) the check passes trivially — that's fine.
            out.append(_check("fn_excludes_probes", "paywall 'real' count excludes QA probes + clawith",
                              int(ep_hit) <= int(clean) + 2,
                              f"endpoint={ep_hit} clean-db={clean} raw-db={raw}", 0))
    else:
        out.append(_check("fn_waterfall_distinct", "Branch A key_issued counts DISTINCT keys",
                          None, f"step-drop HTTP {st}", ms))
        out.append(_check("fn_excludes_probes", "paywall 'real' count excludes QA probes + clawith",
                          None, f"step-drop HTTP {st}", ms))

    if c is not None:
        bots = _scalar(c,
            "SELECT count(*) FROM mcp_pair_codes "
            "WHERE created_at > now() - interval '24 hours' "
            "AND referring_agent LIKE 'Mozilla%'")
        out.append(_check("fn_paircode_bot_mints", "pair codes: 0 scanner-UA mints in 24h",
                          (bots == 0) if bots is not None else None,
                          f"{bots} scanner-UA mints/24h" if bots is not None else "query failed"))
    else:
        out.append(_check("fn_paircode_bot_mints", "pair codes: 0 scanner-UA mints in 24h",
                          None, "no db"))
    return out


# ── tick orchestration ────────────────────────────────────────────────

_LANES = [
    ("mcp_reliability", "1 · MCP reliability",  lambda c: _lane_mcp()),
    ("rag",             "2 · RAG gate + packs", lambda c: _lane_rag(c)),
    ("frontend",        "3 · Frontend map/LCP", lambda c: _lane_frontend()),
    ("seo",             "4 · SEO",              lambda c: _lane_seo()),
    ("funnel_honesty",  "5 · Funnel honesty",   lambda c: _lane_funnel(c)),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 30.0


def _ensure_snapshots(c) -> None:
    try:
        with c.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS fixwave_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[fixwave] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    lanes = []
    for key, label, fn in _LANES:
        try:
            checks = fn(c)
        except Exception as e:  # a lane must never sink the tick
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        decided = [ch for ch in checks if ch["pass"] is not None]
        lane_pass = bool(decided) and all(ch["pass"] for ch in decided)
        lanes.append({"lane": key, "label": label, "pass": lane_pass,
                      "checks": checks,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "read-only probes; frozen 07-03 baselines; see routes/fixwave_master_shell.py",
    }
    if c is not None:
        try:
            _ensure_snapshots(c)
            with c.cursor() as cur:
                cur.execute("INSERT INTO fixwave_snapshots (lanes_pass, lanes_total, payload) "
                            "VALUES (%s, %s, %s)",
                            (payload["lanes_pass"], payload["lanes_total"], json.dumps(payload)))
        except Exception as e:
            logger.debug("[fixwave] snapshot insert failed: %s", e)
        try:
            c.close()
        except Exception:
            pass
    return payload


def _tick_cached() -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick()
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes ────────────────────────────────────────────────────────────

@fixwave_master_shell_bp.route("/api/v1/admin/fixwave/master-tick", methods=["GET", "POST"])
def fixwave_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 503
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    fresh = (request.args.get("fresh") or "") == "1"
    if fresh:
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@fixwave_master_shell_bp.route("/admin/fixwave", methods=["GET"])
@fixwave_master_shell_bp.route("/api/v1/admin/fixwave", methods=["GET"])
def fixwave_dashboard():
    if _disabled():
        return Response("fixwave disabled", status=503)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def _chip(v):
        if v is True:
            return '<span style="color:#22c55e">✓</span>'
        if v is False:
            return '<span style="color:#ef4444">✗</span>'
        return '<span style="color:#eab308">?</span>'

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px'>{_chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px'>{_esc(ch['name'])}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}"
            f"{(' · ' + str(ch['ms']) + 'ms') if ch.get('ms') else ''}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else "#334155"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table></div>")

    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Fix-Wave Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Fix-Wave Master Shell "
        f"<span style='color:{'#22c55e' if p['lanes_pass'] == p['lanes_total'] else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>07-03 deep-dive wave · read-only probes · "
        f"30s tick cache · auto-refresh 60s · generated {_esc(p['generated_at'])} · "
        f"JSON: /api/v1/admin/fixwave/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
