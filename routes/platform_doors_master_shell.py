"""
routes/platform_doors_master_shell.py — Platform-Doors Master Shell (#27, 2026-07-25).

★ READ-ONLY DIAGNOSTIC. Measures whether the AI-platform integration surfaces we
OWE (Gemini, Copilot, Grok, Meta) are actually OPEN — and whether the recipes and
keys we publish to open them are honest and working. Names the actuator per lane,
FIRES NOTHING. Complements the Onboarding shell (which SCORES readiness 0-100)
with the states that score can't see: door OPEN vs crawl-only, recipe validity,
the published-comp-key exposure, and the honest retention north-star.

WHY (2026-07-25 platform-doors workflow wf_668c4a35-adc)
-------------------------------------------------------------------------------
The four platforms the operator owes updates to CRAWL DC Hub heavily but do not
tool-call: Meta 33k / Gemini 30k / Copilot 24k / Grok 6k reach, yet 7d tool-calls
are Copilot 12 · Gemini 1 · Grok 1 · Meta 0. That gap — huge attention, ~no
tool-use — is the real growth lever, and no existing shell surfaces it as an
actionable per-door state. The same workflow found two honesty defects this shell
now stands sentinel over:
  · a family of 13 PRO comp keys (dchub_<platform>_2026_verify, main.py) with the
    Copilot + Grok ones PUBLISHED in git-tracked static/integrations/*/ — free PRO
    (300/day) to anyone who reads the recipe. REST-path only (server.mjs 401s
    them), but a real free-tier bypass.
  · the Grok recipe embeds one of those keys, which 401s through /mcp — a recipe
    that guarantees failure.
And the growth adversary's ruling: stop citing the churn-inflated distinct-agent
count; the honest north-star is RETURNING agents (an agent_id seen on >=2 days),
because mint ~= rotation.

LANES (each names an actuator; fires nothing)
  1. OWED-DOOR STATE — per owed platform, 7d real-external tool-calls. OPEN vs
     crawl-only. The doors we owe are the ones with reach but no calls.
  2. RECIPE / COMP-KEY INTEGRITY (SECURITY) — the published static integration
     recipes must not embed a PRO comp key. RED while dchub_*_2026_verify ships in
     any served static/integrations file.
  3. AGENT-CARD A2A HONESTY — supports_a2a_handoff must stay False while the A2A
     handoff endpoint is a stub; advertising True to marketplaces would be a false
     capability claim (the growth adversary's must-not).
  4. HONEST RETENTION NORTH-STAR — returning agents (agent_id on >=2 distinct days,
     7d) is the real signal; the raw distinct count is churn/self-inflated. This
     lane cites the returning number and flags the inflation, never the raw count.
  5. CITATION LEVER FRESHNESS — for the crawl-only platforms (Meta/Gemini) the ONLY
     move that helps is fresh per-platform tool descriptions (cited by RAG). The
     tuner must be <14d fresh across the owed platforms.

★ Pure-DB (read replica) + deployed-static-file reads. NO self-requests through the
public edge (the 2026-07-06 pool-saturation footgun). Fail-soft. Admin-gated.
Snapshot to the PRIMARY (replica is read-only). Kill: PLATFORM_DOORS_SHELL_DISABLE=1

Endpoints:
  GET/POST /api/v1/admin/platform-doors/master-tick   JSON (5 lanes)
  GET      /admin/platform-doors                        HTML (60s refresh)
  GET      /api/v1/admin/platform-doors                 CF zone-worker bypass alias
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

platform_doors_master_shell_bp = Blueprint("platform_doors_master_shell", __name__)

# Platforms the operator explicitly owes updates to, with their door type + the
# platform-tag they surface under in mcp_calls_identity.platform.
_OWED = [
    ("gemini",  "Gemini / Google",  "enterprise Custom-MCP data store (No-Auth free tier)"),
    ("copilot", "Microsoft Copilot", "Copilot Studio custom MCP → Agent Store (Partner Center)"),
    ("grok",    "Grok / xAI",        "grok.com/connectors custom connector + X unblock"),
    ("meta",    "Meta AI",           "NO consumer tool-call door exists — citation surface only"),
]
_OPEN_CALLS_MIN = 5   # >=5 real-external 7d tool-calls = a door that is actually open
_TUNER_STALE_DAYS = 14


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    expected = ((os.environ.get("DCHUB_ADMIN_KEY")
                 or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == expected


def _disabled() -> bool:
    return (os.environ.get("PLATFORM_DOORS_SHELL_DISABLE") or "").strip() == "1"


def _conn():
    """Read replica (this shell only reads). Falls back to primary."""
    try:
        import psycopg2 as _pg
        url = (os.environ.get("NEON_REPLICA_URL")
               or os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        if not url:
            return None
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[platform-doors] db connect failed: %s", e)
        return None


def _rows(c, sql: str) -> list:
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    except Exception as e:
        logger.debug("[platform-doors] rows failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return []


def _scalar(c, sql: str):
    r = _rows(c, sql)
    return (r[0][0] if r and r[0] else None)


def _check(cid: str, name: str, passed, detail: str, critical: bool = False) -> dict:
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:320], "critical": critical}


def _lane_verdict(checks: list):
    if any(ch["pass"] is None and ch.get("critical") for ch in checks):
        return None
    decided = [ch for ch in checks if ch["pass"] is not None]
    if not decided:
        return None
    return all(ch["pass"] for ch in decided)


# repo root = parent of this routes/ dir; static files ship with the deploy.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_static(relpath: str):
    """Read a deployed static-integration file, or None. The served copy IS the
    public exposure, so reading it here measures exactly what the world can see."""
    try:
        p = os.path.join(_REPO_ROOT, relpath)
        if os.path.exists(p):
            with open(p, "r", errors="replace") as f:
                return f.read()
    except Exception as e:
        logger.debug("[platform-doors] read %s failed: %s", relpath, e)
    return None


# ── lane 1 · owed-door state ──────────────────────────────────────────

def _lane_doors(c) -> list:
    out = []
    if c is None:
        return [_check("dr_nodb", "door lane needs db", None, "no db", critical=True)]
    calls = {}
    for slug, label, _door in _OWED:
        n = _scalar(c,
            "SELECT count(*) FROM mcp_calls_identity WHERE is_real_external "
            "AND created_at >= now() - interval '7 days' "
            "AND lower(COALESCE(platform,'')) = '" + slug + "'")
        calls[slug] = int(n or 0)
    open_doors = [s for s in ("gemini", "copilot", "grok") if calls.get(s, 0) >= _OPEN_CALLS_MIN]
    detail = " · ".join(f"{s}:{calls.get(s,0)}" for s, _l, _d in _OWED)
    # meta is excluded from "openable" — no consumer tool-call door exists.
    out.append(_check("dr_any_open", "at least one owed enterprise door is OPEN (>=5 calls/7d)",
                      len(open_doors) >= 1,
                      f"7d tool-calls — {detail}  · open: {open_doors or 'none'} "
                      "(meta has NO consumer tool-call door — citation-only)"))
    # The gap: doors with reach but ~no calls are the actionable ones.
    closed = [f"{l} ({_door})" for slug, l, _door in _OWED
              if slug != "meta" and calls.get(slug, 0) < _OPEN_CALLS_MIN]
    out.append(_check("dr_gap", "owed doors mostly open (<=1 closed enterprise door)",
                      len(closed) <= 1,
                      "closed enterprise doors → " + " | ".join(closed) if closed
                      else "all owed enterprise doors open"))
    return out


# ── lane 2 · recipe / comp-key integrity (SECURITY) ───────────────────

def _lane_recipes(c) -> list:
    out = []
    # A published static recipe must not embed a PRO comp key.
    exposed = []
    checked = 0
    for slug in ("copilot", "grok", "gemini", "meta", "chatgpt", "perplexity", "mistral"):
        for fn in ("mcp-config.json", "function-calling.json", "README.md",
                   "test-script.py", "test_script.py"):
            body = _read_static(os.path.join("static", "integrations", slug, fn))
            if body is None:
                continue
            checked += 1
            if "_2026_verify" in body:
                exposed.append(f"{slug}/{fn}")
    out.append(_check("rc_compkey_exposure", "no PRO comp key published in a static recipe",
                      len(exposed) == 0 if checked else None,
                      (f"{len(exposed)} served recipe files embed a dchub_*_2026_verify PRO key "
                       f"(free 300/day to anyone): {', '.join(exposed[:4])}") if exposed
                      else (f"clean across {checked} recipe files" if checked
                            else "no static recipe files found on deploy — cannot verify"),
                      critical=True))
    # The Grok recipe specifically embeds a key that 401s through /mcp (dead recipe).
    grok = _read_static(os.path.join("static", "integrations", "grok", "mcp-config.json"))
    if grok is None:
        out.append(_check("rc_grok_live", "grok recipe uses a working auth path", None,
                          "grok mcp-config.json not on deploy"))
    else:
        dead = "dchub_grok_2026_verify" in grok
        out.append(_check("rc_grok_live", "grok recipe uses a working auth path",
                          not dead,
                          "grok recipe embeds dchub_grok_2026_verify — 401s through /mcp "
                          "(server.mjs doesn't honor _verify keys); use the keyless path"
                          if dead else "grok recipe not pinned to the dead _verify key"))
    return out


# ── lane 3 · agent-card A2A honesty ───────────────────────────────────

def _lane_card(c) -> list:
    out = []
    card = None
    for rel in ("routes/agent_a2a.py",):
        card = _read_static(rel)
        if card:
            break
    if card is None:
        out.append(_check("cd_handoff", "A2A handoff not falsely advertised", None,
                          "agent_a2a.py not readable on deploy"))
        return out
    # supports_a2a_handoff must be False while the handoff endpoint is a stub.
    honest = "supports_a2a_handoff" not in card or 'supports_a2a_handoff": False' in card \
        or "supports_a2a_handoff': False" in card or '"supports_a2a_handoff": False' in card \
        or "supports_a2a_handoff = False" in card
    flipped_true = ('supports_a2a_handoff": True' in card or "supports_a2a_handoff': True" in card)
    out.append(_check("cd_handoff", "A2A handoff not falsely advertised (stub stays False)",
                      (not flipped_true),
                      "supports_a2a_handoff advertised TRUE while handoff is a stub — a false "
                      "capability claim to A2A marketplaces" if flipped_true
                      else "supports_a2a_handoff honest (False/absent) — card is discovery-only"))
    return out


# ── lane 4 · honest retention north-star ──────────────────────────────

def _lane_northstar(c) -> list:
    out = []
    if c is None:
        return [_check("ns_nodb", "north-star needs db", None, "no db", critical=True)]
    # RETURNING agents = an agent_id seen on >=2 DISTINCT days in the last 7d.
    # This is the honest signal; the raw distinct count is churn/self-inflated.
    returning = _scalar(c,
        "SELECT count(*) FROM (SELECT agent_id FROM mcp_calls_identity "
        " WHERE is_real_external AND is_public_ip AND created_at >= now() - interval '7 days' "
        " GROUP BY agent_id HAVING count(DISTINCT created_at::date) >= 2) q")
    distinct = _scalar(c,
        "SELECT count(DISTINCT agent_id) FROM mcp_calls_identity "
        "WHERE is_real_external AND is_public_ip AND created_at >= now() - interval '7 days'")
    r = int(returning or 0)
    d = int(distinct or 0)
    ratio = (r / d * 100.0) if d else 0.0
    out.append(_check("ns_returning", "returning agents is the cited north-star (not raw distinct)",
                      r >= 1,
                      f"{r} returning agents (id on >=2 days/7d) · {d} raw distinct "
                      f"({round(ratio,1)}% return). Cite RETURNING — raw distinct is churn/self-inflated "
                      "(mint~=rotation)"))
    # Return ratio is the binding constraint; flag when it's below the ~8% health floor.
    out.append(_check("ns_return_floor", "cross-week return ratio >= 8% (retention floor)",
                      ratio >= 8.0 if d else None,
                      f"{round(ratio,1)}% return — the binding constraint. Lever = client-auto "
                      "re-auth (re-present key session 2), NOT key durability"))
    return out


# ── lane 5 · citation-lever freshness ─────────────────────────────────

def _lane_tuner(c) -> list:
    out = []
    if c is None:
        return [_check("tn_nodb", "tuner lane needs db", None, "no db")]
    # For the crawl-only doors (Meta/Gemini) the only lever is fresh per-platform
    # tool descriptions that RAG cites. Check the tuner is fresh for the owed set.
    age = _scalar(c,
        "SELECT EXTRACT(EPOCH FROM (now() - max(updated_at)))/86400.0 "
        "FROM mcp_tool_descriptions_per_platform "
        "WHERE lower(platform) IN ('gemini','copilot','grok','meta')")
    if age is None:
        out.append(_check("tn_fresh", "per-platform tool-description tuner fresh (<14d)", None,
                          "no owed-platform tuner rows found — verify tuner seeded them"))
    else:
        out.append(_check("tn_fresh", "per-platform tool-description tuner fresh (<14d)",
                          float(age) < _TUNER_STALE_DAYS,
                          f"owed-platform tuner rows newest {round(float(age),1)}d old "
                          "(the citation lever for crawl-only Meta/Gemini)"))
    return out


_LANES = [
    ("doors",     "1 · Owed-door state (open vs crawl-only)", _lane_doors,
     "gemini→Enterprise Custom-MCP connect · copilot→Partner Center · grok→connector+X · meta→citation-only"),
    ("recipes",   "2 · Recipe / comp-key integrity (SECURITY)", _lane_recipes,
     "pull dchub_*_2026_verify from static/integrations/* + rotate; grok recipe → keyless path"),
    ("card",      "3 · Agent-card A2A honesty", _lane_card,
     "keep supports_a2a_handoff=False until a real A2A JSON-RPC endpoint exists; never juice marketplaces"),
    ("northstar", "4 · Honest retention north-star", _lane_northstar,
     "instrument RETURNING-agent rate as north-star (replace churn-inflated distinct); ship client-auto re-auth"),
    ("tuner",     "5 · Citation-lever freshness", _lane_tuner,
     "keep per-platform tool-descriptions fresh — the only lever for crawl-only Meta/Gemini citations"),
]

_cache: dict = {"ts": 0.0, "payload": None}
_cache_lock = threading.Lock()
_TICK_TTL = 45.0


def _ensure_snapshots(pc) -> None:
    try:
        with pc.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS platform_doors_snapshots ("
                " id BIGSERIAL PRIMARY KEY,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[platform-doors] snapshot ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    lanes = []
    for key, label, fn, actuator in _LANES:
        t0 = time.time()
        try:
            checks = fn(c)
        except Exception as e:
            checks = [_check(f"{key}_error", "lane crashed", None, str(e)[:200])]
        ms = int((time.time() - t0) * 1000)
        decided = [ch for ch in checks if ch["pass"] is not None]
        lanes.append({"lane": key, "label": label, "pass": _lane_verdict(checks),
                      "actuator": actuator, "checks": checks, "ms": ms,
                      "progress": f"{sum(1 for ch in decided if ch['pass'])}/{len(checks)}"})
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only DIAGNOSTIC — names an actuator per lane, fires nothing",
        "lanes_pass": sum(1 for l in lanes if l["pass"] is True),
        "lanes_total": len(lanes),
        "lanes": lanes,
        "note": "Platform-Doors master shell #27 — routes/platform_doors_master_shell.py",
    }
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    # Snapshot to the PRIMARY (replica is read-only).
    pc = None
    try:
        import psycopg2 as _pg
        purl = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if purl:
            pc = _pg.connect(purl, connect_timeout=8)
            pc.autocommit = True
            _ensure_snapshots(pc)
            with pc.cursor() as cur:
                cur.execute("INSERT INTO platform_doors_snapshots (lanes_pass, lanes_total, payload) "
                            "VALUES (%s,%s,%s)",
                            (payload["lanes_pass"], payload["lanes_total"], json.dumps(payload)))
    except Exception as e:
        logger.debug("[platform-doors] snapshot insert skipped: %s", e)
    finally:
        if pc is not None:
            try:
                pc.close()
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


@platform_doors_master_shell_bp.route("/api/v1/admin/platform-doors/master-tick", methods=["GET", "POST"])
def platform_doors_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _cache_lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@platform_doors_master_shell_bp.route("/admin/platform-doors", methods=["GET"])
@platform_doors_master_shell_bp.route("/api/v1/admin/platform-doors", methods=["GET"])
def platform_doors_dashboard():
    if _disabled():
        return Response("platform-doors shell disabled", status=404)
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
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] is True else ("#eab308" if lane["pass"] is None else "#ef4444")
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'>"
            f"<div style='font-weight:700;font-size:15px'>{_chip(lane['pass'])} "
            f"{_esc(lane['label'])} <span style='color:#64748b;font-weight:400'>"
            f"({lane['progress']} · {lane.get('ms',0)}ms)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator (not fired): "
            f"{_esc(lane.get('actuator',''))}</div></div>")

    green = p["lanes_pass"] == p["lanes_total"]
    html = (
        "<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='60'>"
        "<title>Platform-Doors Master Shell · DC Hub</title>"
        "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,Segoe UI,"
        "Roboto,sans-serif;max-width:900px;margin:24px auto;padding:0 16px'>"
        f"<h2 style='margin:0 0 4px'>Platform-Doors Master Shell "
        f"<span style='color:{'#22c55e' if green else '#eab308'}'>"
        f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
        f"<div style='color:#64748b;font-size:12px'>#27 · 07-25 · gemini/copilot/grok/meta doors + "
        f"recipe/comp-key integrity + honest retention north-star · read-only DIAGNOSTIC "
        f"(names an actuator per lane, fires nothing) · 45s cache · read replica · "
        f"generated {_esc(p['generated_at'])} · JSON /api/v1/admin/platform-doors/master-tick</div>"
        + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
