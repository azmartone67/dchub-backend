"""
routes/webmcp_master_shell.py — WebMCP Master Shell (2026-07-11, webmcp-lane).

The sentinel for the WebMCP enablement lane: js/dchub-webmcp.js (frontend)
registers read-only page tools with the browser's model context (Chrome
origin trial), stamping every bound API call src=webmcp + X-DC-Source so
ai_tracking attributes it as platform 'webmcp'; the backend's
_webmcp_enable after_request hook (main.py) serves the Origin-Trial header
+ script include on /markets|/facilities|/dcpi|/grid. FOUR silent-failure
modes, one pane:

  1. ATTRIBUTION — the 'webmcp' platform rows actually land (ai_daily_stats /
     ai_requests, written by ai_tracking.log_ai_request) and the classifier
     is still wired (pure import — catches a revert).
  2. HEADER SERVING — HEAD 3 key pages through the PUBLIC edge for the
     Origin-Trial response header: one frontend (CF Pages _headers) family
     page + two backend families (_webmcp_enable). Without the header the
     whole lane is a feature-detected no-op in every browser — invisibly.
  3. TOKEN EXPIRY — decode the env token's base64 JSON payload (Chrome
     origin-trial token format: version byte + 64B signature + 4B length +
     JSON; this trial's expiry=1794873600). Files a brain finding when <30
     days remain — trial tokens die silently and take every page tool down.
  4. TOOLS↔ENDPOINTS DRIFT — every API path the page tools bind returns 200
     keyless (loopback on Railway, cron_heartbeat BASE pattern). A renamed
     or newly-gated endpoint turns a page tool into an error string.

Read-only DIAGNOSTIC: names an actuator per lane, fires nothing. Findings
via routes/brain_findings_writer.upsert_brain_finding on BREAKAGE ONLY
(token <30d, header decidedly missing, bound path decidedly non-200) —
probe errors are unknowns, not findings; zero webmcp traffic is not
breakage. Outbound HTTP is bounded: ≤3 edge HEADs + ≤12 loopback GETs per
tick, 30s tick cache, daily dispatch (20:xx UTC quiet hour) — NOT the
public-edge self-request loop pattern (the 07-06 flywheel outage class).

Endpoints:
  GET/POST /api/v1/admin/webmcp/master-tick   JSON scoreboard
  GET      /admin/webmcp                       HTML dashboard
  GET      /api/v1/admin/webmcp                CF zone-worker bypass

Auth: X-Admin-Key header or ?admin_key= vs DCHUB_ADMIN_KEY (→ DCHUB_INTERNAL_KEY).
Kill: WEBMCP_SHELL_DISABLE=1 (disabled → 404, NEVER 5xx: a 5xx trips the CF
failover breaker → stale Render).

Registration rides on cron_heartbeat_bp.record_once (main.py wiring frozen
for parallel worktree tracks — same pattern as dark_availability_zones).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape as _esc

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

webmcp_master_shell_bp = Blueprint("webmcp_master_shell", __name__)

# The trial token's payload pins expiry 1794873600 (2026-11-16). Warn margin:
TOKEN_WARN_DAYS = 30

# Public pages probed for the Origin-Trial header (family, url). Kept to 3 —
# one CF-Pages-served page (frontend _headers) + two backend-rendered
# families (_webmcp_enable hook). HEAD only, daily.
HEADER_PROBES = [
    ("frontend (/rankings, CF Pages _headers)", "https://dchub.cloud/rankings"),
    ("backend /markets (_webmcp_enable)", "https://dchub.cloud/markets/dallas"),
    ("backend /dcpi (_webmcp_enable)", "https://dchub.cloud/dcpi"),
]

# Every API path js/dchub-webmcp.js binds (base tools + all PAGE_TOOLS
# entries) PLUS the backend page-tool bindings (routes/_webmcp.py, 2026-07-18:
# /radar, /phx, /integrations/mcp). MUST be updated when either side
# adds/renames a binding — this list IS the drift check. Probed keyless via
# loopback.
BOUND_API_PATHS = [
    "/api/v1/search?q=ashburn&limit=1",              # search + facility filter
    "/api/v1/mcp/tools/rank_markets?limit=1",        # rank markets
    "/api/v1/deals?limit=1",                         # M&A deals
    "/api/v1/iso/comparison",                        # grid scoreboard (/land-power, /grid)
    "/api/rankings/construction",                    # state rankings
    "/api/v1/dcpi/trending",                         # DCPI movers (/dcpi)
    "/api/v1/dcpi/scores?limit=1",                   # DCPI coverage (/dcpi)
    "/api/market-intelligence",                      # market intel list (/markets)
    "/api/market-intelligence/Northern%20Virginia",  # market intel detail (/markets)
    "/api/v1/dcpi/scores/northern-virginia",         # market DCPI score (/markets)
    # webmcp-proto (2026-07-18) — backend page tools (routes/_webmcp.py):
    "/api/v1/markets/phoenix",                       # /phx market stats
    "/api/v1/dcpi/scores/phoenix",                   # /phx DCPI scorecard
]

# Loopback base for the drift probes (cron_heartbeat BASE pattern — never
# round-trip through the public edge to reach ourselves).
BASE = (
    f"http://127.0.0.1:{os.environ.get('PORT', '8080')}"
    if (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
    else "https://api.dchub.cloud"
)

_PROBE_UA = "DCHub-WebMCP-Shell/1.0"  # 'dchub' substring ⇒ classified internal


# ── auth / kill ───────────────────────────────────────────────────────

def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("WEBMCP_SHELL_DISABLE") or "").strip().lower() in (
        "1", "true", "yes")


# ── db helpers (fail-soft) ────────────────────────────────────────────

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
        logger.warning("[webmcp-shell] db connect failed: %s", e)
        return None


def _rows(c, sql: str, params=None) -> list | None:
    """Fail-soft rows. None on error (NOT []) so callers can tell 'query
    broke' from 'no rows'."""
    try:
        with c.cursor() as cur:
            cur.execute(sql, params) if params else cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[webmcp-shell] query failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _check(cid: str, name: str, passed, detail: str) -> dict:
    return {"id": cid, "name": name, "pass": passed, "detail": (detail or "")[:300]}


# ── token decode (pure, unit-tested) ──────────────────────────────────

def decode_origin_trial_token(token: str) -> dict | None:
    """Chrome origin-trial token → its JSON payload dict, or None.

    Format (version 2/3): 1 version byte + 64-byte Ed25519 signature +
    4-byte big-endian payload length + JSON payload. Falls back to scanning
    the decoded bytes for the first '{' (format drift tolerance). Pure;
    never raises."""
    try:
        raw = base64.b64decode((token or "").strip())
        if len(raw) > 69:
            (plen,) = struct.unpack(">I", raw[65:69])
            if 0 < plen <= len(raw) - 69:
                return json.loads(raw[69:69 + plen].decode("utf-8"))
        brace = raw.find(b"{")
        if brace >= 0:
            return json.loads(raw[brace:].decode("utf-8", "ignore"))
    except Exception:
        pass
    return None


def token_days_remaining(token: str, now_ts: float | None = None):
    """(days_remaining float, expiry_iso str) or (None, detail str). Pure."""
    payload = decode_origin_trial_token(token)
    if not payload:
        return None, "token missing or undecodable"
    expiry = payload.get("expiry")
    try:
        expiry = float(expiry)
    except (TypeError, ValueError):
        return None, "payload has no numeric expiry"
    now_ts = now_ts if now_ts is not None else time.time()
    iso = datetime.fromtimestamp(expiry, tz=timezone.utc).date().isoformat()
    return (expiry - now_ts) / 86400.0, iso


# ── http probe (bounded, fail-soft) ───────────────────────────────────

def _probe(url: str, method: str = "HEAD", timeout: int = 10) -> dict:
    """One bounded probe. Returns {status, origin_trial(bool), error?}.
    Never raises."""
    try:
        req = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": _PROBE_UA,
                                              "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if method != "HEAD":
                resp.read(256)
            return {"status": int(resp.status),
                    "origin_trial": bool(resp.headers.get("Origin-Trial"))}
    except urllib.error.HTTPError as e:
        return {"status": int(e.code),
                "origin_trial": bool(e.headers.get("Origin-Trial") if e.headers else False)}
    except Exception as e:
        return {"status": 0, "origin_trial": False,
                "error": f"{type(e).__name__}: {str(e)[:80]}"}


# ── lane 1 · attribution ──────────────────────────────────────────────

def _lane_attribution(c) -> list[dict]:
    out = []
    # 1a — classifier wiring (pure import; catches a revert of Task-1 code).
    try:
        import ai_tracking as _at
        wired = (callable(getattr(_at, "is_webmcp_request", None))
                 and getattr(_at, "WEBMCP_PLATFORM", None) == "webmcp"
                 and "webmcp" in getattr(_at, "AI_PLATFORMS", {}))
        out.append(_check("wm_classifier_wired",
                          "ai_tracking webmcp classifier present",
                          bool(wired),
                          "is_webmcp_request + WEBMCP_PLATFORM + AI_PLATFORMS['webmcp']"
                          if wired else "classifier pieces MISSING — attribution reverted?"))
    except Exception as e:
        out.append(_check("wm_classifier_wired",
                          "ai_tracking webmcp classifier present", None,
                          f"import failed: {str(e)[:120]}"))

    # 1b — calls/day 7d from ai_daily_stats (where log_ai_request lands).
    if c is None:
        out.append(_check("wm_calls_7d", "webmcp-attributed calls/day (7d)",
                          None, "no db"))
        return out
    rows = _rows(c, "SELECT date::text, request_count FROM ai_daily_stats "
                    "WHERE platform = 'webmcp' "
                    "AND date >= (CURRENT_DATE - INTERVAL '7 days') "
                    "ORDER BY date")
    if rows is None:
        out.append(_check("wm_calls_7d", "webmcp-attributed calls/day (7d)",
                          False, "ai_daily_stats query FAILED"))
    else:
        total = sum(int(r[1] or 0) for r in rows)
        days = ", ".join(f"{r[0]}:{r[1]}" for r in rows) or "no rows yet"
        # GAUGE: zero traffic in week one is expected, not breakage.
        out.append(_check("wm_calls_7d", "webmcp-attributed calls/day (7d)",
                          None, f"total={total} · {days}"))
    return out


# ── lane 2 · header serving ───────────────────────────────────────────

def _lane_headers(_c) -> list[dict]:
    out = []
    for i, (label, url) in enumerate(HEADER_PROBES):
        r = _probe(url, method="HEAD")
        if r.get("error") or r["status"] == 0:
            out.append(_check(f"wm_hdr_{i}", f"Origin-Trial header · {label}",
                              None, f"probe error: {r.get('error', 'no status')}"))
        elif r["status"] >= 400:
            out.append(_check(f"wm_hdr_{i}", f"Origin-Trial header · {label}",
                              False, f"page HTTP {r['status']} ({url})"))
        else:
            out.append(_check(f"wm_hdr_{i}", f"Origin-Trial header · {label}",
                              bool(r["origin_trial"]),
                              f"HTTP {r['status']}, header "
                              f"{'PRESENT' if r['origin_trial'] else 'MISSING'} ({url})"))
    return out


# ── lane 3 · token expiry ─────────────────────────────────────────────

def _lane_token(_c) -> list[dict]:
    out = []
    token = (os.environ.get("WEBMCP_ORIGIN_TRIAL_TOKEN") or "").strip()
    out.append(_check("wm_token_env", "WEBMCP_ORIGIN_TRIAL_TOKEN env set",
                      bool(token),
                      f"set ({len(token)} chars)" if token
                      else "env NOT set — backend header/injection hooks no-op"))
    if not token:
        return out
    days, iso = token_days_remaining(token)
    if days is None:
        out.append(_check("wm_token_decode", "token payload decodes", False, iso))
        return out
    payload = decode_origin_trial_token(token) or {}
    out.append(_check("wm_token_decode", "token payload decodes", True,
                      f"origin={payload.get('origin')} feature={payload.get('feature')}"))
    out.append(_check("wm_token_expiry",
                      f"token expiry ≥{TOKEN_WARN_DAYS}d away",
                      days >= TOKEN_WARN_DAYS,
                      f"{days:.0f} days remaining (expires {iso})"))
    return out


# ── lane 4 · tools↔endpoints drift ────────────────────────────────────

def _lane_drift(_c) -> list[dict]:
    out = []
    for i, path in enumerate(BOUND_API_PATHS):
        r = _probe(BASE + path, method="GET", timeout=8)
        if r.get("error") or r["status"] == 0:
            out.append(_check(f"wm_ep_{i}", f"bound 200 · {path.split('?')[0]}",
                              None, f"probe error: {r.get('error', 'no status')}"))
        else:
            out.append(_check(f"wm_ep_{i}", f"bound 200 · {path.split('?')[0]}",
                              r["status"] == 200,
                              f"HTTP {r['status']} ({path})"))
    return out


# ── findings (breakage only, canonical writer) ────────────────────────

def _file_findings(payload: dict) -> int:
    """Upsert a brain finding per DECIDED failure. Probe errors (pass=None)
    are unknowns, not findings. Never raises; returns findings filed."""
    broken = []
    for lane in payload.get("lanes", []):
        for ch in lane.get("checks", []):
            if ch["pass"] is False:
                broken.append((lane["lane"], ch))
    if not broken:
        return 0
    c = _conn()
    if c is None:
        return 0
    filed = 0
    try:
        from routes.brain_findings_writer import upsert_brain_finding
        with c.cursor() as cur:
            for lane_key, ch in broken:
                res = upsert_brain_finding(
                    cur,
                    issue=f"webmcp_{lane_key}_broken",
                    url=f"/admin/webmcp#{ch['id']}",
                    count=1,
                    detail=f"{ch['name']}: {ch['detail']}"[:1900],
                    detector="webmcp_master_shell",
                )
                if res in ("inserted", "updated"):
                    filed += 1
    except Exception as e:
        logger.warning("[webmcp-shell] findings write failed: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass
    return filed


# ── tick orchestration ────────────────────────────────────────────────
# (key, label, fn, actuator) — actuator NAMED but never fired (diagnostic).

_LANES = [
    ("attribution", "1 · Attribution (src=webmcp → platform)", _lane_attribution,
     "ai_tracking is_webmcp_request + js/dchub-webmcp.js api() markers"),
    ("headers",     "2 · Origin-Trial header serving",         _lane_headers,
     "frontend _headers rule / main.py _webmcp_enable + CF purge"),
    ("token",       "3 · Trial token expiry",                  _lane_token,
     "re-register trial at developer.chrome.com/origintrials + rotate env"),
    ("drift",       "4 · Tools ↔ endpoints drift",             _lane_drift,
     "update BOUND_API_PATHS + js/dchub-webmcp.js bindings together"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 30.0


# ── dead-man beat ───────────────────────────────────────────────────────

def _beat_ledger(note: str, failing: bool = False) -> None:
    """★ batch-3/Screen D: webmcp runs on the cron dispatcher (cron_heartbeat
    _DISPATCH 'webmcp_shell_daily') and beat NOTHING, so it was absent from
    /api/v1/ops/deadman altogether — the one shell whose death no surface
    could report. A shell that is scheduled is a loop, and every loop needs a
    feed.

    rows_inserted is deliberately OMITTED, not zeroed: a shell inserts no
    rows, `0` climbs the consecutive-zero alarm toward a false red, and `1`
    fabricates health. record_beat() leaves the counter alone when it is
    absent, which is the honest third option.
    """
    try:
        body = json.dumps({
            "feed": "webmcp-shell-daily",
            "status": ("lanes_failing" if failing else "success"),
            "cadence_hours": 24,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "note": note[:280],
        }).encode()
        admin_key = (os.environ.get("DCHUB_ADMIN_KEY")
                     or os.environ.get("DCHUB_INTERNAL_KEY")
                     or os.environ.get("ADMIN_API_KEY", ""))
        import requests as _rq   # not urllib (regression_lint urllib-request-on-railway)
        _rq.post(BASE + "/api/v1/admin/ingest-runs/beat",
                 data=body, timeout=5,
                 headers={"Content-Type": "application/json",
                          "User-Agent": _PROBE_UA,
                          "X-Admin-Key": admin_key})
    except Exception as e:  # noqa: BLE001 — a beat error must never break the tick
        logger.debug("[webmcp] ledger beat failed: %s", e)


def _run_tick(beat: bool = True) -> dict:
    # ★2026-09-02 (D5): beat=False on every GET. A dashboard view — with its
    # auto-refresh — must never stamp the daily beat, or a browser tab keeps a
    # dead cron "alive" on /api/v1/ops/deadman. Only the POST master-tick beats.
    c = _conn()
    lanes = []
    for key, label, fn, actuator in _LANES:
        try:
            checks = fn(c)
        except Exception as e:
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        decided = [ch for ch in checks if ch["pass"] is not None]
        lane_pass = bool(decided) and all(ch["pass"] for ch in decided)
        lanes.append({"lane": key, "label": label, "pass": lane_pass,
                      "actuator": actuator, "checks": checks,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lanes_pass": sum(1 for l in lanes if l["pass"]),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "read-only DIAGNOSTIC (≤3 edge HEADs + ≤12 loopback GETs, "
                "30s tick cache, daily dispatch); names an actuator per lane "
                "but fires nothing; findings on breakage only; see "
                "routes/webmcp_master_shell.py",
    }
    payload["findings_filed"] = _file_findings(payload)
    if beat:
        # ★ NAME the lanes. `lanes 2/4 pass` counted the failures without
        # saying which, so the board could not triage this shell at all —
        # and the ids were right here in _LANES the whole time.
        from routes.lane_triage import format_lane_verdicts
        _beat_ledger(
            format_lane_verdicts((l["lane"], "PASS" if l["pass"] else "FAIL")
                                 for l in lanes)
            + f" | {payload['lanes_pass']}/{payload['lanes_total']} pass",
            failing=payload["lanes_pass"] < payload["lanes_total"])
    return payload


def _tick_cached(beat: bool = False) -> dict:
    with _cache_lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TICK_TTL:
            return _cache["payload"]
    payload = _run_tick(beat=beat)
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["payload"] = payload
    return payload


# ── routes (disabled → 404, NEVER 5xx) ────────────────────────────────

@webmcp_master_shell_bp.route("/api/v1/admin/webmcp/master-tick",
                              methods=["GET", "POST"])
def webmcp_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    beat = request.method == "POST"
    # A POST is the scheduled beat path: it must run a REAL tick, never
    # serve the 30s cache a dashboard view just filled (that would skip the
    # beat and read as a missed slot).
    if beat or (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached(beat=beat))


@webmcp_master_shell_bp.route("/admin/webmcp", methods=["GET"])
@webmcp_master_shell_bp.route("/api/v1/admin/webmcp", methods=["GET"])
def webmcp_dashboard():
    if _disabled():
        return Response("webmcp shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached(beat=False)

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
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else "#334155"
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} checks green)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator (not fired): "
            f"{_esc(lane.get('actuator', ''))}</div></div>")

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='120'>"
        "<title>WebMCP Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:880px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>WebMCP Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>07-11 · read-only DIAGNOSTIC "
        f"(bounded probes; names an actuator per lane, fires nothing; findings on "
        f"breakage only) · 30s tick cache · auto-refresh 120s · "
        f"generated {_esc(p['generated_at'])} · findings filed: "
        f"{p.get('findings_filed', 0)} · "
        f"JSON: /api/v1/admin/webmcp/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
