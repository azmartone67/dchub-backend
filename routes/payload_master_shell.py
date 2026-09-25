"""
routes/payload_master_shell.py — Payload Master Shell (#38, 2026-07-27).

Born from one measurement. `get_market_intel("ashburn")` — 18,629 calls in 30
days, our highest-volume tool — called the way a new agent calls it:

    call 1   4,233 bytes ≈ 1,058 tokens   Ashburn data returned: 0 fields
    call 2   7,042 bytes ≈ 1,760 tokens   Ashburn data returned: 0 fields

Composition of call 1: high_intent_* 27.8% · upgrade/pricing 24.4% ·
trial/persist 23.7% · cross-sell nudges 20.0% · quota 2.9% · **actual data
1.2%** (which is only `_entity` and `tool`). The API key appeared 8 times,
persist_command twice, upgrade_url twice, across four distinct purchase URLs.

And the two calls CONTRADICT each other:
    call 1  "trial key is ALREADY applied … No header, no reconnect."  bound=true
    call 2  "Add header X-API-Key … and reconnect"                     bound=false
An agent that OBEYS call 1 lands in call 2 and is told the opposite.
`remaining_full_today` stayed 3 both times; neither returned a full answer.

The behaviour matches: of 2,007 distinct callers in 30d, **1,793 (89%) called
exactly ONE tool** and 158 (7.9%) came back on a second day. Agents are not
failing to navigate the surface — they are being handed a wall of upsell with
no data and leaving.

Five lanes:
  1. FIRST-RESPONSE DATA — does call #1 carry any data at all?
  2. ENVELOPE RATIO — non-data bytes per response, and duplicate emissions.
  3. INSTRUCTION COHERENCE — do consecutive calls contradict each other?
  4. AGENT WAIT — p50/p95 and total seconds agents spend blocked.
  5. NAVIGATION — one-tool-and-gone, second-day return, execute_plan adoption.

★★ THE PROBE IS OPT-IN, AND IT SELF-EXCLUDES. Lanes 1-3 need a real response
to measure, which means calling our own MCP server — and that call would land
in mcp_call_log and pollute lanes 4-5, the exact self-traffic artifact that
made June's "hundreds of calls/wk" a measurement illusion
(reference_dchub_mcp_traffic_rfo_0701). So:
  · the default tick is PURE-DB + the last stored probe. It never calls out.
  · a probe runs only on POST …/probe?probe=1, tags itself
    platform='shell38-probe', and lanes 4-5 exclude that tag by name.
  · lanes 1-3 render "?" when no probe has ever run. They never guess.

READ-ONLY otherwise: every lane names an actuator and fires NOTHING.

Endpoints:
  GET/POST /api/v1/admin/payload/master-tick   JSON scoreboard (5 lanes)
  POST     /api/v1/admin/payload/probe         run + store ONE probe pair
  GET      /admin/payload                      HTML dashboard
Kill: PAYLOAD_SHELL_DISABLE=1
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

payload_master_shell_bp = Blueprint("payload_master_shell", __name__)

# Traffic this shell generates, excluded from every traffic lane.
# ★★ EXCLUDE BY USER-AGENT, NOT platform. The MCP server OVERWRITES the platform
# field with its own detection — a probe sent with platform='shell38-probe' was
# recorded as platform='mcp', so a platform-based filter silently excluded
# NOTHING while the SQL still looked correct. The User-Agent survives intact.
# Verified live 2026-07-27: 0 rows matched the platform tag, 2 matched the UA.
PROBE_PLATFORM = "shell38-probe"
PROBE_UA = "dchub-shell38-probe"
PROBE_TOOL = "get_market_intel"

# Keys that carry SELLING, not data. Measured 2026-07-27 on a live response.
_ENVELOPE_PREFIXES = (
    "high_intent", "auto_trial", "upgrade", "owner_purchase", "identify",
    "persist", "retry", "claim", "recover_key", "digest_optin",
    "first_call_nudge", "unlocked_tools", "trial_", "signup_url",
    "for_your_human", "human_message", "redeem_url", "starter_url",
    "developer_url", "usage_url", "web_explore_url", "docs_url", "pricing",
    "agent_payment", "pay_now", "refresh_challenge", "daily_calls_when",
    "remaining_full", "inline_full", "taste_bounded", "auto_bound_session",
)


def _admin_ok() -> bool:
    sent = (request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    exp = ((os.environ.get("DCHUB_ADMIN_KEY")
            or os.environ.get("DCHUB_INTERNAL_KEY") or "").strip())
    return bool(sent) and sent == exp


def _disabled() -> bool:
    return (os.environ.get("PAYLOAD_SHELL_DISABLE") or "").strip() == "1"


def _connect(url):
    if not url:
        return None
    try:
        import psycopg2 as _pg
        c = _pg.connect(url, connect_timeout=8)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("[payload] db connect failed: %s", e)
        return None


def _conn():
    return _connect(os.environ.get("NEON_REPLICA_URL")
                    or os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))


def _write_conn():
    """Never the replica — a swallowed read-only write leaves the snapshot
    table empty while every tick reports success."""
    return _connect(os.environ.get("DATABASE_URL")
                    or os.environ.get("NEON_DATABASE_URL"))


def _row(c, sql: str):
    if c is None:
        return None
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()
    except Exception as e:
        logger.debug("[payload] row failed: %s -- %s", sql[:80], e)
        try:
            c.rollback()
        except Exception:
            pass
        return None


def _pct(p, w):
    try:
        p, w = float(p), float(w)
    except (TypeError, ValueError):
        return None
    return round(p * 100.0 / w, 1) if w else None


def _check(cid, name, passed, detail, critical=False):
    return {"id": cid, "name": name, "pass": passed,
            "detail": (detail or "")[:340], "critical": critical}


def _lane_verdict(checks):
    if any(c["pass"] is None and c.get("critical") for c in checks):
        return None
    decided = [c for c in checks if c["pass"] is not None]
    return all(c["pass"] for c in decided) if decided else None


# ── payload analysis (pure function — unit-testable, no I/O) ──────────

def classify_payload(obj) -> dict:
    """Split a tool response into DATA vs ENVELOPE bytes.

    Envelope = anything selling, onboarding or instructing. Data = the answer
    the caller asked for. `_entity`/`tool`/`quota` are metadata: counted
    separately so they inflate neither side.
    """
    if not isinstance(obj, dict):
        return {"total": 0, "data": 0, "envelope": 0, "meta": 0,
                "data_fields": 0, "envelope_keys": 0}
    total = data = env = meta = 0
    data_fields = env_keys = 0
    for k, v in obj.items():
        b = len(json.dumps({k: v}, default=str))
        total += b
        if k in ("_entity", "tool", "quota", "_cite", "ok", "platform"):
            meta += b
        elif any(k.startswith(p) for p in _ENVELOPE_PREFIXES):
            env += b
            env_keys += 1
        else:
            data += b
            data_fields += 1
    return {"total": total, "data": data, "envelope": env, "meta": meta,
            "data_fields": data_fields, "envelope_keys": env_keys}


def coherence_issues(first: dict, second: dict) -> list:
    """Contradictions between two consecutive responses. Each entry is a
    string a human can act on — not a score."""
    out = []
    if not isinstance(first, dict) or not isinstance(second, dict):
        return out
    f_r = str(first.get("retry_instructions") or "")
    s_r = str(second.get("retry_instructions") or "")
    f_no_hdr = "no header" in f_r.lower() or "already applied" in f_r.lower()
    s_add_hdr = "add header" in s_r.lower() or "reconnect" in s_r.lower()
    if f_no_hdr and s_add_hdr:
        out.append("call1 says 'no header needed', call2 says 'add header and "
                   "reconnect' — obeying call1 leads into call2")
    if first.get("auto_bound_session") is True and second.get("auto_bound_session") is False:
        out.append("auto_bound_session flipped true→false between consecutive calls")
    fr, sr = first.get("remaining_full_today"), second.get("remaining_full_today")
    if fr is not None and fr == sr and first.get("data_fields", 1) == 0:
        out.append(f"remaining_full_today stayed {fr} across both calls — the "
                   "quota never decremented and no full answer was served")
    return out


# ── the probe (opt-in only) ───────────────────────────────────────────

def _mcp_url() -> str:
    base = (os.environ.get("MCP_INTERNAL_URL")
            or os.environ.get("RAILWAY_SERVICE_DCHUB_MCP_SERVER_URL")
            or "dchub-mcp-server-production.up.railway.app")
    if not base.startswith("http"):
        base = "https://" + base
    return base.rstrip("/").removesuffix("/mcp") + "/mcp"


def run_probe() -> dict:
    """TWO consecutive calls to one flagship tool, analysed. Never raises.

    ★ Tagged PROBE_PLATFORM so lanes 4-5 exclude it. Without that tag this
    shell would inflate the very call counts it reports — the June
    self-traffic illusion, rebuilt inside the instrument measuring it.
    """
    import urllib.request
    url, out = _mcp_url(), {"at": datetime.now(timezone.utc).isoformat(),
                            "url": None, "error": None, "calls": []}
    out["url"] = url
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": PROBE_TOOL, "arguments": {"market": "ashburn"},
                       "_meta": {"platform": PROBE_PLATFORM}}}
    for i in (1, 2):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream",
                         "X-DC-Platform": PROBE_PLATFORM,
                         "User-Agent": f"dchub-{PROBE_PLATFORM}/1.0"})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8", "replace")
            ms = int((time.time() - t0) * 1000)
            parsed = _extract_tool_json(raw)
            cls = classify_payload(parsed)
            cls.update({"call": i, "ms": ms,
                        "retry_instructions": str(parsed.get("retry_instructions") or "")[:200],
                        "auto_bound_session": parsed.get("auto_bound_session"),
                        "remaining_full_today": parsed.get("remaining_full_today")})
            out["calls"].append(cls)
        except Exception as e:
            out["error"] = str(e)[:200]
            break
    if len(out["calls"]) == 2:
        out["coherence"] = coherence_issues(out["calls"][0], out["calls"][1])
    return out


def _extract_tool_json(raw: str) -> dict:
    """Pull the tool payload out of a JSON-RPC or SSE body. Returns {} when
    the shape is unrecognised — never a guess."""
    try:
        if raw.lstrip().startswith("event:") or "\ndata:" in raw:
            for line in raw.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break
        env = json.loads(raw)
        res = (env.get("result") or {})
        if isinstance(res.get("structuredContent"), dict):
            return res["structuredContent"]
        content = res.get("content") or []
        if content and isinstance(content[0], dict) and content[0].get("text"):
            return json.loads(content[0]["text"])
        return res if isinstance(res, dict) else {}
    except Exception:
        return {}


def _latest_probe(c):
    r = _row(c, "SELECT payload, created_at FROM payload_probe_snapshots"
                " ORDER BY created_at DESC LIMIT 1")
    if not r or not r[0]:
        return None, None
    p = r[0] if isinstance(r[0], dict) else json.loads(r[0])
    return p, r[1]


# ── lanes 1-3 · from the stored probe ─────────────────────────────────

def _probe_age_note(when):
    if when is None:
        return "no probe stored — POST /api/v1/admin/payload/probe?probe=1"
    age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
    return f"probe age {age_h:.1f}h"


def _lane_first_response(c, ctx):
    out = []
    probe, when = _latest_probe(c)
    ctx["probe"], ctx["probe_at"] = probe, when
    if not probe or not probe.get("calls"):
        out.append(_check("fr_data", "first response carries data", None,
                          _probe_age_note(when), critical=True))
        return out
    first = probe["calls"][0]
    n = int(first.get("data_fields") or 0)
    ctx["first_data_fields"] = n
    out.append(_check(
        "fr_data", "first response carries data (>=1 field)",
        n >= 1,
        f"{PROBE_TOOL}: {n} data field(s) in call #1 · "
        f"{first.get('total',0):,}b ≈ {int(first.get('total',0))//4:,} tokens · "
        + _probe_age_note(when), critical=True))
    return out


def _lane_envelope(c, ctx):
    out = []
    probe = ctx.get("probe")
    if not probe or not probe.get("calls"):
        out.append(_check("env_ratio", "response is mostly data, not selling",
                          None, "no probe stored", critical=True))
        return out
    for cl in probe["calls"]:
        i = cl.get("call")
        share = _pct(cl.get("envelope"), cl.get("total"))
        if i == 1:
            ctx["env_share"] = share
            out.append(_check(
                "env_ratio", "envelope is under 50% of the response",
                (share is not None and share < 50.0),
                f"call#1 envelope {share}% ({cl.get('envelope',0):,}b of "
                f"{cl.get('total',0):,}b) across {cl.get('envelope_keys',0)} "
                f"selling keys vs {cl.get('data_fields',0)} data fields",
                critical=True))
        else:
            out.append(_check(
                "env_ratio_2", "second call is not MORE envelope than the first",
                (cl.get("total") or 0) <= (probe["calls"][0].get("total") or 0),
                f"call#2 {cl.get('total',0):,}b vs call#1 "
                f"{probe['calls'][0].get('total',0):,}b — a repeat caller should "
                "get MORE data and LESS pitch, not the reverse"))
    return out


def _lane_coherence(c, ctx):
    out = []
    probe = ctx.get("probe")
    if not probe or len(probe.get("calls") or []) < 2:
        out.append(_check("coh_contradict", "consecutive calls do not contradict",
                          None, "no two-call probe stored", critical=True))
        return out
    issues = probe.get("coherence") or []
    ctx["coherence_issues"] = len(issues)
    out.append(_check(
        "coh_contradict", "consecutive calls do not contradict",
        len(issues) == 0,
        (" · ".join(issues)[:300] if issues
         else "call#1 and call#2 give consistent instructions"),
        critical=True))
    return out


# ── lane 4 · agent wait (pure DB, probe excluded) ─────────────────────

def _lane_wait(c, ctx):
    out = []
    ex = (" AND coalesce(left(user_agent, " + str(len(PROBE_UA)) + "), '') <> '" + PROBE_UA + "'"
          " AND coalesce(platform,'') <> '" + PROBE_PLATFORM + "'")
    r = _row(c, "SELECT round(sum(duration_ms)/1000.0)::bigint, count(*)"
                " FROM mcp_call_log WHERE timestamp > now() - interval '30 days'"
                "   AND duration_ms IS NOT NULL" + ex)
    if not r:
        out.append(_check("wait_total", "agent wait is bounded", None,
                          "query failed", critical=True))
    else:
        secs, n = int(r[0] or 0), int(r[1] or 0)
        ctx["wait_hours"] = round(secs / 3600.0, 1)
        out.append(_check(
            "wait_total", "total agent wait under 100h/30d",
            secs < 360000,
            f"{secs:,}s ({secs/3600.0:.1f}h) of agent wait across {n:,} calls "
            "in 30d — time callers spend BLOCKED on us", critical=True))

    r = _row(c, "SELECT tool, percentile_disc(0.5) within group (order by duration_ms)::int"
                " FROM mcp_call_log WHERE timestamp > now() - interval '30 days'"
                "   AND duration_ms IS NOT NULL AND duration_ms > 0" + ex +
                " GROUP BY tool HAVING count(*) >= 20"
                " ORDER BY 2 DESC LIMIT 1")
    if r:
        out.append(_check(
            "wait_slowest", "no tool has a p50 over 3s",
            int(r[1] or 0) < 3000,
            f"slowest: {r[0]} p50={int(r[1] or 0):,}ms"))
    return out


# ── lane 5 · navigation (pure DB, probe excluded) ─────────────────────

def _lane_navigation(c, ctx):
    out = []
    ex = (" AND coalesce(left(user_agent, " + str(len(PROBE_UA)) + "), '') <> '" + PROBE_UA + "'"
          " AND coalesce(platform,'') <> '" + PROBE_PLATFORM + "'")
    r = _row(c, "SELECT count(*), count(*) FILTER (WHERE d = 1) FROM ("
                " SELECT api_key, count(DISTINCT tool) d FROM mcp_call_log"
                " WHERE timestamp > now() - interval '30 days'"
                "   AND api_key IS NOT NULL" + ex + " GROUP BY api_key) x")
    if not r or not r[0]:
        out.append(_check("nav_onetool", "callers explore more than one tool",
                          None, "no keyed callers", critical=True))
    else:
        total, one = int(r[0]), int(r[1] or 0)
        share = _pct(one, total)
        ctx["one_tool_share"] = share
        out.append(_check(
            "nav_onetool", "under 60% of callers touch only ONE tool",
            (share is not None and share < 60.0),
            f"{one:,} of {total:,} callers ({share}%) called exactly one tool "
            "in 30d and never explored further", critical=True))

    r = _row(c, "SELECT count(*), count(*) FILTER (WHERE days > 1) FROM ("
                " SELECT api_key, count(DISTINCT timestamp::date) days"
                " FROM mcp_call_log WHERE timestamp > now() - interval '30 days'"
                "   AND api_key IS NOT NULL" + ex + " GROUP BY api_key) x")
    if r and r[0]:
        total, ret = int(r[0]), int(r[1] or 0)
        out.append(_check(
            "nav_return", "second-day return above 15%",
            _pct(ret, total) >= 15.0,
            f"{ret:,} of {total:,} keys ({_pct(ret, total)}%) came back on a "
            "second day"))

    r = _row(c, "SELECT count(*) FILTER (WHERE tool IN ('execute_plan','plan_query')),"
                " count(*) FROM mcp_call_log"
                " WHERE timestamp > now() - interval '30 days'" + ex)
    if r and r[1]:
        planner, total = int(r[0] or 0), int(r[1])
        out.append(_check(
            "nav_planner", "the one-call front door is discoverable", None,
            f"execute_plan/plan_query = {planner:,} of {total:,} calls "
            f"({_pct(planner, total)}%) — it answers a multi-step question in "
            "ONE call, and is the direct remedy for one-tool-and-gone"))
    return out


# ── tick ──────────────────────────────────────────────────────────────

_LANES = [
    ("first_response", "1 · First-response data", _lane_first_response,
     "serve >=1 real row in the preview — data + ONE upsell line, not upsell "
     "with no data"),
    ("envelope", "2 · Envelope ratio", _lane_envelope,
     "collapse ~40 selling keys into one {upgrade:{url,price,note}}; the api "
     "key is emitted 8x, persist_command 2x, upgrade_url 2x"),
    ("coherence", "3 · Instruction coherence", _lane_coherence,
     "make retry_instructions a function of ONE session-bound state; today "
     "call1 says 'no header' and call2 says 'add header'"),
    ("agent_wait", "4 · Agent wait", _lane_wait,
     "cache the hot market/grid reads — get_market_intel alone is ~10.6h of "
     "blocked agent time per 30d"),
    ("navigation", "5 · Navigation depth", _lane_navigation,
     "surface execute_plan as the FRONT DOOR (it is tool #80 of 80 by "
     "adoption) and make tool #1 return data"),
]

_cache = {"ts": 0.0, "payload": None}
_lock = threading.Lock()
_TTL = 180.0


def _ensure_tables(c):
    try:
        with c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS payload_probe_snapshots ("
                        " id BIGSERIAL PRIMARY KEY,"
                        " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                        " payload JSONB)")
            cur.execute("CREATE TABLE IF NOT EXISTS payload_shell_snapshots ("
                        " id BIGSERIAL PRIMARY KEY,"
                        " created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                        " lanes_pass INT, lanes_total INT, payload JSONB)")
    except Exception as e:
        logger.debug("[payload] ddl skipped: %s", e)


def _run_tick() -> dict:
    c = _conn()
    ctx, lanes = {}, []
    for key, label, fn, act in _LANES:
        t0 = time.time()
        try:
            checks = fn(c, ctx)
        except Exception as e:
            checks = [_check(f"{key}_err", "lane crashed", None, str(e)[:200])]
        decided = [x for x in checks if x["pass"] is not None]
        lanes.append({"lane": key, "label": label, "pass": _lane_verdict(checks),
                      "actuator": act, "checks": checks,
                      "ms": int((time.time() - t0) * 1000),
                      "progress": f"{sum(1 for x in decided if x['pass'])}/{len(decided)}"
                      if decided else "0/0"})
    payload = {"ok": True,
               "generated_at": datetime.now(timezone.utc).isoformat(),
               "lanes_pass": sum(1 for l in lanes if l["pass"]),
               "lanes_total": len(lanes), "lanes": lanes,
               "probe_platform": PROBE_PLATFORM,
               "note": "read-only; names an actuator per lane and fires nothing. "
                       "Lanes 1-3 read the last stored probe (opt-in); lanes 4-5 "
                       "are pure-DB and EXCLUDE probe traffic by platform tag."}
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    payload["persisted"] = False
    w = _write_conn()
    if w is not None:
        try:
            _ensure_tables(w)
            with w.cursor() as cur:
                cur.execute("INSERT INTO payload_shell_snapshots"
                            " (lanes_pass, lanes_total, payload) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            (payload["lanes_pass"], payload["lanes_total"],
                             json.dumps(payload)))
            payload["persisted"] = True
        except Exception as e:
            logger.warning("[payload] snapshot insert failed: %s", e)
        try:
            w.close()
        except Exception:
            pass
    return payload


def _tick_cached():
    with _lock:
        if _cache["payload"] is not None and time.time() - _cache["ts"] < _TTL:
            return _cache["payload"]
    p = _run_tick()
    with _lock:
        _cache["ts"], _cache["payload"] = time.time(), p
    return p


# ── routes ────────────────────────────────────────────────────────────

@payload_master_shell_bp.route("/api/v1/admin/payload/master-tick",
                               methods=["GET", "POST"])
def payload_master_tick():
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("fresh") or "") == "1":
        with _lock:
            _cache["payload"] = None
    return jsonify(_tick_cached())


@payload_master_shell_bp.route("/api/v1/admin/payload/probe", methods=["POST"])
def payload_probe():
    """Opt-in. Makes TWO real MCP calls, tagged so lanes 4-5 exclude them."""
    if _disabled():
        return jsonify(ok=False, error="disabled"), 404
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    if (request.args.get("probe") or "") != "1":
        return jsonify(ok=False,
                       error="probe makes 2 real MCP calls — pass ?probe=1"), 400
    result = run_probe()
    w = _write_conn()
    stored = False
    if w is not None:
        try:
            _ensure_tables(w)
            with w.cursor() as cur:
                cur.execute("INSERT INTO payload_probe_snapshots (payload)"
                            " VALUES (%s)", (json.dumps(result),))
            stored = True
        except Exception as e:
            logger.warning("[payload] probe store failed: %s", e)
        try:
            w.close()
        except Exception:
            pass
    with _lock:
        _cache["payload"] = None
    return jsonify(ok=True, stored=stored, probe=result)


@payload_master_shell_bp.route("/admin/payload", methods=["GET"])
@payload_master_shell_bp.route("/api/v1/admin/payload", methods=["GET"])
def payload_dashboard():
    if _disabled():
        return Response("payload shell disabled", status=404)
    if not _admin_ok():
        return Response("forbidden — X-Admin-Key or ?admin_key=", status=403)
    p = _tick_cached()

    def chip(v):
        return ('<span style="color:#22c55e">✓</span>' if v is True else
                '<span style="color:#ef4444">✗</span>' if v is False else
                '<span style="color:#eab308">?</span>')

    cards = []
    for lane in p["lanes"]:
        rows = "".join(
            f"<tr><td style='padding:4px 8px;vertical-align:top'>{chip(ch['pass'])}</td>"
            f"<td style='padding:4px 8px;vertical-align:top'>{_esc(ch['name'])}"
            f"{' <b style=color:#f59e0b>*</b>' if ch.get('critical') else ''}</td>"
            f"<td style='padding:4px 8px;color:#94a3b8'>{_esc(ch['detail'])}</td></tr>"
            for ch in lane["checks"])
        border = "#22c55e" if lane["pass"] else ("#ef4444" if lane["pass"] is False else "#334155")
        cards.append(
            f"<div style='background:#0f172a;border:1px solid {border};border-radius:12px;"
            f"padding:16px;margin:12px 0'><div style='font-weight:700;font-size:15px'>"
            f"{chip(lane['pass'])} {_esc(lane['label'])} <span style='color:#64748b;"
            f"font-weight:400'>({lane['progress']} checks green · {lane['ms']}ms)</span></div>"
            f"<table style='margin-top:8px;font-size:13px;border-collapse:collapse'>{rows}</table>"
            f"<div style='margin-top:8px;font-size:12px;color:#64748b'>⚡ actuator "
            f"(not fired): {_esc(lane.get('actuator',''))}</div></div>")

    html = ("<!doctype html><meta charset='utf-8'>"
            "<meta http-equiv='refresh' content='300'>"
            "<title>Payload Master Shell · DC Hub</title>"
            "<body style='background:#020617;color:#e2e8f0;font-family:-apple-system,"
            "Segoe UI,Roboto,sans-serif;max-width:920px;margin:24px auto;padding:0 16px'>"
            f"<h2 style='margin:0 0 4px'>Payload Master Shell "
            f"<span style='color:{'#22c55e' if p['lanes_pass']==p['lanes_total'] else '#eab308'}'>"
            f"{p['lanes_pass']}/{p['lanes_total']} lanes green</span></h2>"
            f"<div style='color:#64748b;font-size:12px'>#38 · 07-27 · read-only · "
            f"lanes 1-3 read the last OPT-IN probe (POST …/payload/probe?probe=1), "
            f"lanes 4-5 are pure-DB and exclude probe traffic · generated "
            f"{_esc(p['generated_at'])}</div>"
            "<div style='background:#0f172a;border:1px solid #334155;border-radius:12px;"
            "padding:14px;margin:14px 0;color:#94a3b8;font-size:13px'>"
            "Measures what a caller actually RECEIVES: how much of a response is "
            "data versus selling, whether consecutive calls contradict each other, "
            "how long agents wait, and whether they ever get past tool #1."
            "</div>" + "".join(cards) + "</body>")
    return Response(html, mimetype="text/html")
