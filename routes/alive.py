"""Phase YY (2026-05-16) — /alive: proof of life for a living, agentic system.

The "we're alive and you can see it" page. Aggregates the system's vital
signs into ONE JSON (for agents) and ONE HTML page (for humans), both
auto-refreshing.

No competitor (DCHawk, dcByte, DCK, DCF) exposes anything like this.
DCHawk claims "real time" but shows no timestamps. dcByte claims
"continuously validated" but no live signals. We're the only ones
willing to BE THE PROOF.

Vital signs surfaced:
  - Data freshness: green/yellow/red counts across 49 sources
  - MCP traffic: last-hour call rate + funnel velocity (signals→codes→conversions)
  - Brain self-awareness: open findings count + by-severity breakdown
  - Auto-heal: number of auto-fixes in the last 24h
  - Discovery loop: minutes since last DCPI recompute, next-scheduled
  - International expansion: count of intl markets with live adapters
  - Version + uptime

Both endpoints cache 30s in-process — fresh enough to feel live, infrequent
enough that scrapers don't spin up DB load.
"""

from __future__ import annotations

import os
import time
import datetime
import threading
from flask import Blueprint, jsonify, Response
import psycopg2
import psycopg2.extras


alive_bp = Blueprint("alive", __name__)


_ALIVE_CACHE: dict = {"data": None, "ts": 0.0}
# r43-H (2026-05-28): bumped 30s→120s. _build_vitals() calls the brain
# consistency radar synchronously and takes ~34s, so a 30s TTL meant the
# cache was always stale → every request rebuilt → the build (>30s) blew
# past gunicorn's 30s budget and hard-timed-out (000). The build now runs
# in a background thread (stale-while-revalidate, below); a longer TTL just
# trims how often that background rebuild fires.
_ALIVE_TTL = 120.0
# Single-flight guard so only one background refresh runs at a time.
_ALIVE_REFRESH_LOCK = threading.Lock()
_ALIVE_REFRESHING = {"flag": False}


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _build_vitals() -> dict:
    """Pull the live vital signs from the database."""
    now = datetime.datetime.now(datetime.timezone.utc)
    vitals: dict = {
        "alive":          True,
        "checked_at":     now.isoformat(),
        "version":        "2.0.0",
        "data_freshness": {"green": 0, "yellow": 0, "red": 0, "never_ran": 0,
                            "disabled": 0, "total": 0},
        "mcp_flow": {
            "calls_last_hour":   None,
            "signals_last_24h":  None,
            "codes_minted_24h":  None,
            "conversions_24h":   None,
            "conversion_ratio":  None,
        },
        "brain": {
            "findings_open":  None,
            "by_severity":    {},
            "last_scan":       None,
        },
        "dcpi": {
            "markets_scored":      None,
            "last_recompute_at":   None,
            "minutes_since":       None,
            "next_recompute_eta":  None,
        },
        "international": {
            "markets_total":       None,
            "with_live_adapter":   None,
        },
        "agentic_heartbeat_score": None,  # 0-100, computed at the end
    }
    conn = _conn()
    if conn is None:
        vitals["alive"] = False
        vitals["error"] = "no_database"
        return vitals

    try:
        with conn.cursor() as cur:
            # Bound every query so a slow scan degrades to a partial vital
            # sign instead of hanging the (background) build.
            try: cur.execute("SET statement_timeout = 4000")
            except Exception: pass
            # ── Data freshness summary ──
            try:
                cur.execute("SELECT to_regclass('public.source_registry')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("""
                        SELECT enabled, cadence_seconds, last_success_at FROM source_registry
                    """)
                    rows = cur.fetchall()
                    for enabled, cadence, last_ok in rows:
                        vitals["data_freshness"]["total"] += 1
                        if not enabled:
                            vitals["data_freshness"]["disabled"] += 1; continue
                        if last_ok is None:
                            vitals["data_freshness"]["never_ran"] += 1; continue
                        sla_h = max(1.0, (cadence or 86400) / 3600.0)
                        age_h = (now - last_ok).total_seconds() / 3600.0
                        if age_h <= sla_h:        vitals["data_freshness"]["green"] += 1
                        elif age_h <= sla_h * 2:  vitals["data_freshness"]["yellow"] += 1
                        else:                      vitals["data_freshness"]["red"] += 1
            except Exception:
                pass

            # ── MCP traffic ──
            try:
                cur.execute("SELECT to_regclass('public.mcp_call_log')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("SELECT COUNT(*) FROM mcp_call_log "
                                 "WHERE timestamp >= NOW() - INTERVAL '1 hour'")
                    vitals["mcp_flow"]["calls_last_hour"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                pass

            # ── Signals ──
            try:
                cur.execute("SELECT to_regclass('public.mcp_upgrade_signals')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("SELECT COUNT(*) FROM mcp_upgrade_signals "
                                 "WHERE created_at >= NOW() - INTERVAL '24 hours'")
                    vitals["mcp_flow"]["signals_last_24h"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                pass

            # ── Codes minted + conversions ──
            try:
                cur.execute("SELECT to_regclass('public.mcp_pair_codes')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("SELECT COUNT(*) FROM mcp_pair_codes "
                                 "WHERE created_at >= NOW() - INTERVAL '24 hours'")
                    vitals["mcp_flow"]["codes_minted_24h"] = int((cur.fetchone() or [0])[0] or 0)
                    cur.execute("SELECT COUNT(*) FROM mcp_pair_codes "
                                 "WHERE redeemed_at IS NOT NULL "
                                 "  AND redeemed_at >= NOW() - INTERVAL '24 hours'")
                    vitals["mcp_flow"]["conversions_24h"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                pass

            # Conversion ratio
            sigs = vitals["mcp_flow"].get("signals_last_24h") or 0
            convs = vitals["mcp_flow"].get("conversions_24h") or 0
            if sigs > 0 and convs >= 0:
                vitals["mcp_flow"]["conversion_ratio"] = (
                    f"1:{int(sigs / max(1, convs))}" if convs > 0 else f"1:{sigs}+"
                )

            # ── DCPI ──
            try:
                cur.execute("SELECT to_regclass('public.market_power_scores')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("""
                        SELECT COUNT(DISTINCT market_slug), MAX(computed_at)
                          FROM market_power_scores
                    """)
                    r = cur.fetchone()
                    if r:
                        vitals["dcpi"]["markets_scored"] = int(r[0] or 0)
                        if r[1]:
                            vitals["dcpi"]["last_recompute_at"] = r[1].isoformat()
                            mins = (now - r[1]).total_seconds() / 60.0
                            vitals["dcpi"]["minutes_since"] = int(mins)
                            # DCPI cron is daily (24h) so eta = 24h - mins_since
                            eta = max(0, 24 * 60 - int(mins))
                            vitals["dcpi"]["next_recompute_eta"] = f"~{eta} min"
            except Exception:
                pass

            # ── International coverage ──
            try:
                cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE state IN
                        ('GB','IE','DE','NL','FR','SG','JP','IN','AU','BR','AE','ZA'))
                      AS intl
                      FROM market_power_scores
                """)
                r = cur.fetchone()
                if r:
                    vitals["international"]["markets_total"] = int(r[0] or 0)
                    # live_adapter count requires env-var inspection
                    live = 0
                    for var in ("ENTSOE_API_TOKEN", "JEPX_API_KEY",
                                "EMA_API_KEY", "NESO_API_KEY"):
                        if os.environ.get(var): live += 1
                    vitals["international"]["with_live_adapter"] = live
            except Exception:
                pass
    finally:
        try: conn.close()
        except Exception: pass

    # ── Brain findings (call our own radar) ──
    try:
        from routes.brain_consistency_radar import scan_summary as _brain_scan
        bs = _brain_scan()
        vitals["brain"]["findings_open"] = int(bs.get("count") or 0)
        vitals["brain"]["by_severity"] = bs.get("by_issue") or {}
        vitals["brain"]["last_scan"] = bs.get("as_of")
    except Exception:
        pass

    # ── Agentic heartbeat score (0-100) ──
    score = 100.0
    df = vitals["data_freshness"]
    if df["total"] > 0:
        red_pct = df["red"] / df["total"]
        yellow_pct = df["yellow"] / df["total"]
        score -= red_pct * 50
        score -= yellow_pct * 20
    if (vitals["mcp_flow"].get("calls_last_hour") or 0) == 0:
        score -= 15   # no traffic = something is wrong
    if (vitals["brain"].get("findings_open") or 0) > 10:
        score -= 10
    if vitals["dcpi"].get("minutes_since") is not None \
            and vitals["dcpi"]["minutes_since"] > 60 * 36:
        score -= 15   # DCPI stale > 36h
    vitals["agentic_heartbeat_score"] = max(0, min(100, round(score)))

    return vitals


def _refresh_vitals_bg():
    """Rebuild vitals into the cache. Single-flight: a second caller while
    one refresh is in flight is a no-op (avoids piling up 34s builds)."""
    with _ALIVE_REFRESH_LOCK:
        if _ALIVE_REFRESHING["flag"]:
            return
        _ALIVE_REFRESHING["flag"] = True

    def _run():
        try:
            fresh = _build_vitals()
            _ALIVE_CACHE["data"] = fresh
            _ALIVE_CACHE["ts"]   = time.time()
        except Exception:
            pass
        finally:
            _ALIVE_REFRESHING["flag"] = False

    threading.Thread(target=_run, daemon=True).start()


def _get_cached_vitals() -> dict:
    """Stale-while-revalidate. The request path never pays the ~34s build
    cost: fresh cache → return it; stale cache → return stale + kick a
    background refresh; cold (no data yet) → wait briefly for the first
    build, else return a lightweight 'warming' payload so we never hang."""
    now = time.time()
    data = _ALIVE_CACHE["data"]
    if data and (now - _ALIVE_CACHE["ts"]) < _ALIVE_TTL:
        return data

    _refresh_vitals_bg()

    if data is not None:
        return data  # serve stale immediately while the refresh runs

    # Truly cold (process just booted): give the first build a short window,
    # otherwise return a minimal alive payload and let the bg thread fill it.
    deadline = now + 8.0
    while time.time() < deadline:
        if _ALIVE_CACHE["data"] is not None:
            return _ALIVE_CACHE["data"]
        time.sleep(0.25)
    return {
        "alive":      True,
        "warming":    True,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "version":    "2.0.0",
        "data_freshness": {"green": 0, "yellow": 0, "red": 0, "never_ran": 0,
                           "disabled": 0, "total": 0},
        "mcp_flow": {}, "brain": {"by_severity": {}}, "dcpi": {},
        "international": {}, "agentic_heartbeat_score": None,
    }


@alive_bp.route("/api/v1/alive", methods=["GET", "OPTIONS"])
def api_alive():
    """JSON heartbeat. 30s cache. The agentic vital signs."""
    from flask import request
    if request.method == "OPTIONS":
        resp = jsonify(ok=True); resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp, 200
    v = _get_cached_vitals()
    resp = jsonify(v)
    resp.headers["Cache-Control"]               = "public, max-age=30"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    # Surface the heartbeat score as a header so monitoring tools can read it
    # without parsing the body.
    resp.headers["X-Agentic-Heartbeat"] = str(v.get("agentic_heartbeat_score", "n/a"))
    return resp, 200


# ── Tiny pulse: just heartbeat + status. For uptime checks. ──
@alive_bp.route("/api/v1/alive/pulse", methods=["GET"])
def api_alive_pulse():
    v = _get_cached_vitals()
    resp = jsonify({
        "alive":     bool(v.get("alive")),
        "score":     v.get("agentic_heartbeat_score"),
        "checked":   v.get("checked_at"),
    })
    resp.headers["Cache-Control"] = "public, max-age=15"
    return resp, 200


@alive_bp.route("/alive", methods=["GET"], strict_slashes=False)
def html_alive():
    """Human-readable vital signs dashboard. Auto-refreshes every 30s."""
    v = _get_cached_vitals()

    df = v.get("data_freshness", {})
    mcp = v.get("mcp_flow", {})
    brain = v.get("brain", {})
    dcpi = v.get("dcpi", {})
    intl = v.get("international", {})
    score = v.get("agentic_heartbeat_score", 0)

    score_color = "#16a34a" if score >= 80 else ("#ca8a04" if score >= 60 else "#dc2626")
    status_phrase = (
        "Excellent — all systems nominal" if score >= 90 else
        "Healthy — minor warnings"        if score >= 75 else
        "Degraded — investigate"          if score >= 50 else
        "Critical — needs attention"
    )

    def _badge(color, label, count=None):
        bg = {"green": "#dcfce7", "yellow": "#fef3c7", "red": "#fee2e2",
              "gray":  "#e5e7eb"}.get(color, "#e5e7eb")
        fg = {"green": "#166534", "yellow": "#92400e", "red": "#991b1b",
              "gray":  "#374151"}.get(color, "#374151")
        n = f" {count}" if count is not None else ""
        return f'<span style="display:inline-block;padding:3px 10px;border-radius:14px;background:{bg};color:{fg};font-weight:600;font-size:.85rem;margin-right:6px">{label}{n}</span>'

    fresh_badges = (
        _badge("green",  "Green",     df.get("green", 0)) +
        _badge("yellow", "Yellow",    df.get("yellow", 0)) +
        _badge("red",    "Red",       df.get("red", 0)) +
        _badge("gray",   "Disabled",  df.get("disabled", 0)) +
        _badge("gray",   "Never ran", df.get("never_ran", 0))
    )

    sev_rows = ""
    for issue, n in (brain.get("by_severity") or {}).items():
        sev_rows += f'<tr><td><code>{issue}</code></td><td>{n}</td></tr>'
    if not sev_rows:
        sev_rows = '<tr><td colspan="2" style="color:#16a34a">No open findings 🎉</td></tr>'

    intl_live = intl.get("with_live_adapter") or 0
    intl_total = 4
    intl_pct = int((intl_live / intl_total) * 100) if intl_total else 0

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub — Alive · Vital signs score {score}/100</title>
<meta name="description" content="Live system vital signs for dchub.cloud — agentic heartbeat score, data freshness across 49 sources, MCP traffic, brain findings, DCPI recompute status. The only data center intelligence platform that publishes its own pulse.">
<meta http-equiv="refresh" content="30">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://dchub.cloud/alive">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        max-width:1100px;margin:1.5rem auto;padding:0 1rem;color:#1f2937;line-height:1.55;
        background:#fafbfc}}
  h1{{margin:0 0 .25rem;font-size:1.8rem;display:flex;align-items:center;gap:.6rem}}
  .pulse{{display:inline-block;width:14px;height:14px;border-radius:50%;
          background:{score_color};animation:pulse 1.5s ease-in-out infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
  .score-card{{background:white;padding:1.5rem;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.06);
               margin:1rem 0;display:grid;grid-template-columns:1fr 2fr;gap:2rem;align-items:center}}
  .score-big{{font-size:5rem;font-weight:700;color:{score_color};line-height:1;text-align:center}}
  .score-meta{{color:#6b7280;font-size:.95rem}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin:1rem 0}}
  .card{{background:white;padding:1rem 1.25rem;border-radius:8px;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
  .card h3{{margin:0 0 .5rem;font-size:.92rem;text-transform:uppercase;letter-spacing:.06em;color:#6b7280}}
  .metric{{font-size:1.6rem;font-weight:600;color:#111827}}
  .metric-sub{{color:#6b7280;font-size:.85rem;margin-top:.25rem}}
  table{{width:100%;border-collapse:collapse;font-size:.88rem;margin-top:.5rem}}
  td{{padding:.3rem .5rem;border-bottom:1px solid #f3f4f6}}
  code{{font-family:Menlo,Consolas,monospace;font-size:.85em;background:#f3f4f6;padding:1px 5px;border-radius:3px}}
  .footnote{{color:#9ca3af;font-size:.8rem;margin-top:2rem;text-align:center}}
</style>
</head>
<body>
<h1><span class="pulse"></span> DC Hub is alive</h1>
<p style="margin:0 0 1rem;color:#6b7280">Vital signs refresh every 30 seconds · checked at {v.get('checked_at', 'n/a')[:19]}Z</p>

<div class="score-card">
  <div class="score-big">{score}<span style="font-size:1.5rem;color:#9ca3af">/100</span></div>
  <div>
    <div style="font-size:1.3rem;font-weight:600;margin-bottom:.4rem;color:{score_color}">{status_phrase}</div>
    <div class="score-meta">
      <strong>Agentic heartbeat score</strong> — computed from data freshness across {df.get('total',0)} sources,
      live MCP traffic, brain self-awareness findings, and DCPI recompute cadence.
      Updated every 30s. <code>X-Agentic-Heartbeat</code> header on <code>/api/v1/alive</code>.
    </div>
  </div>
</div>

<div class="grid">

  <div class="card">
    <h3>Data freshness · {df.get('total', 0)} sources</h3>
    <div style="margin:.6rem 0">{fresh_badges}</div>
    <div class="metric-sub">Source-by-source vital signs at <a href="/api/v1/sources/health">/api/v1/sources/health</a></div>
  </div>

  <div class="card">
    <h3>MCP traffic · last hour</h3>
    <div class="metric">{mcp.get('calls_last_hour', 0) or 0}</div>
    <div class="metric-sub">tool calls · {mcp.get('signals_last_24h', 0) or 0} signals · {mcp.get('codes_minted_24h', 0) or 0} codes · {mcp.get('conversions_24h', 0) or 0} conversions (24h)</div>
  </div>

  <div class="card">
    <h3>Brain · self-awareness</h3>
    <div class="metric">{brain.get('findings_open', '—') if brain.get('findings_open') is not None else '—'}</div>
    <div class="metric-sub">open findings · <a href="/api/v1/brain/consistency-radar">consistency radar</a></div>
    <table>{sev_rows}</table>
  </div>

  <div class="card">
    <h3>DCPI · last recompute</h3>
    <div class="metric">{dcpi.get('markets_scored', '—') or '—'}<span style="font-size:.9rem;color:#9ca3af"> markets</span></div>
    <div class="metric-sub">
      Last computed: {dcpi.get('last_recompute_at', '—') or '—'}<br>
      {dcpi.get('minutes_since', '—') if dcpi.get('minutes_since') is not None else '—'} min ago · next in {dcpi.get('next_recompute_eta', '—') or '—'}
    </div>
  </div>

  <div class="card">
    <h3>International expansion</h3>
    <div class="metric">{intl.get('markets_total', 0) or 0}<span style="font-size:.9rem;color:#9ca3af"> intl markets</span></div>
    <div class="metric-sub">{intl_live}/{intl_total} adapters live ({intl_pct}%) · <a href="/api/v1/intl/ingestion-status">status</a></div>
  </div>

  <div class="card">
    <h3>Conversion velocity</h3>
    <div class="metric">{mcp.get('conversion_ratio', '—') or '—'}</div>
    <div class="metric-sub">signal-to-conversion ratio (24h) · target 1:100 or better</div>
  </div>

</div>

<div class="footnote">
  DC Hub is the only data center intelligence platform that publishes its own pulse.<br>
  No competitor in the category exposes anything comparable.<br>
  <a href="/api/v1/alive">JSON</a> · <a href="/api/v1/alive/pulse">pulse</a> · <a href="/mcp/tools">MCP tools</a> · <a href="/llms.txt">llms.txt</a>
</div>
</body>
</html>"""

    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=30"})
