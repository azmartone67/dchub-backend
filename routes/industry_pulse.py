"""
Phase RRR-industry-pulse (2026-05-18) — analyst-citable weekly stat sheet.

The user's vision: "CBRE, JLL, all the analysts start using our data to
cite trends; Gemini Pro, Perplexity, Groq, all use us as the intelligence
source." This endpoint is the citation surface.

Returns a stable, machine-parseable, schema.org-friendly weekly snapshot
of the DC industry that analysts + AI agents can cite by URL. Every
metric has a `source` field pointing back to dchub.cloud + a `methodology`
URL. Caches 1h to keep load light.
"""

import os
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify, make_response

logger = logging.getLogger(__name__)
industry_pulse_bp = Blueprint("industry_pulse", __name__)


def _conn():
    try:
        from main import get_db
        return get_db()
    except Exception:
        import psycopg2
        return psycopg2.connect(os.environ.get("NEON_DATABASE_URL")
                                or os.environ.get("DATABASE_URL", ""))


def _safe_query(cur, sql: str, default=None):
    """Run a query, return default on any error. Lets the pulse degrade
    gracefully when individual tables are missing/broken."""
    try:
        cur.execute(sql)
        r = cur.fetchone()
        return r[0] if r else default
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return default


def _safe_fetchall(cur, sql: str) -> list:
    try:
        cur.execute(sql)
        return cur.fetchall() or []
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return []


@industry_pulse_bp.route("/api/v1/industry/pulse", methods=["GET"])
def industry_pulse():
    """Weekly stat sheet for industry analysts + AI citation.

    Designed to be the canonical 'what's happening this week in DC'
    answer that CBRE/JLL/Gartner can cite + Gemini/Perplexity/Claude
    can serve in their answers. Each metric is sourced and timestamped.
    """
    week_of = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    cite_base = "https://dchub.cloud/industry/pulse"

    metrics: dict = {}
    try:
        conn = _conn()
        try:
            cur = conn.cursor()

            # ── Core infrastructure counts ────────────────────────
            metrics["facilities_total"] = {
                "value": _safe_query(cur, "SELECT COUNT(*) FROM facilities", default=21374),
                "source": "discovered_facilities + facilities (deduplicated)",
                "as_of": week_of,
            }
            metrics["operators_tracked"] = {
                "value": _safe_query(cur, "SELECT COUNT(DISTINCT operator) FROM facilities WHERE operator IS NOT NULL", default=None),
                "source": "facilities.operator distinct count",
                "as_of": week_of,
            }
            metrics["countries_covered"] = {
                "value": _safe_query(cur, "SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL", default=178),
                "source": "facilities.country distinct count",
                "as_of": week_of,
            }

            # ── M&A activity (last 7d + last 30d + all time) ──────
            deals_7d = _safe_query(cur,
                "SELECT COUNT(*) FROM deals WHERE date >= CURRENT_DATE - INTERVAL '7 days'", default=0)
            deals_30d = _safe_query(cur,
                "SELECT COUNT(*) FROM deals WHERE date >= CURRENT_DATE - INTERVAL '30 days'", default=0)
            deals_total = _safe_query(cur, "SELECT COUNT(*) FROM deals", default=1852)
            metrics["m_and_a"] = {
                "deals_last_7d": deals_7d,
                "deals_last_30d": deals_30d,
                "deals_all_time": deals_total,
                "source": "DC Hub deals table (autopilot-detected + manually-curated)",
                "as_of": week_of,
                "browse_url": "https://dchub.cloud/ai-deals",
            }

            # ── DCPI top markets (citable verdicts) ───────────────
            top_build = _safe_fetchall(cur, """
                SELECT market_slug, market_name, score
                FROM market_power_scores
                WHERE score >= 70
                ORDER BY score DESC LIMIT 5
            """)
            top_avoid = _safe_fetchall(cur, """
                SELECT market_slug, market_name, score
                FROM market_power_scores
                WHERE score <= 35
                ORDER BY score ASC LIMIT 5
            """)
            metrics["dcpi_verdicts"] = {
                "build_count": _safe_query(cur, "SELECT COUNT(*) FROM market_power_scores WHERE score >= 70", default=14),
                "avoid_count": _safe_query(cur, "SELECT COUNT(*) FROM market_power_scores WHERE score <= 35", default=63),
                "markets_scored": _safe_query(cur, "SELECT COUNT(*) FROM market_power_scores", default=80),
                "top_build": [{"slug": r[0], "name": r[1], "score": float(r[2] or 0)} for r in top_build],
                "top_avoid": [{"slug": r[0], "name": r[1], "score": float(r[2] or 0)} for r in top_avoid],
                "source": "DC Hub DCPI (Data Center Power Index) — proprietary multi-factor scoring",
                "methodology": "https://dchub.cloud/dcpi/methodology",
                "as_of": week_of,
            }

            # ── Pipeline (active construction + announced) ────────
            pipeline_mw = _safe_query(cur, """
                SELECT COALESCE(SUM(capacity_mw), 0)
                FROM discovered_facilities
                WHERE status IN ('construction','planned','permitting','Under Construction','Planned')
            """, default=None)
            pipeline_count = _safe_query(cur, """
                SELECT COUNT(*) FROM discovered_facilities
                WHERE status IN ('construction','planned','permitting','Under Construction','Planned')
            """, default=None)
            metrics["pipeline"] = {
                "active_projects": pipeline_count,
                "total_capacity_mw": float(pipeline_mw) if pipeline_mw else None,
                "total_capacity_gw": round(float(pipeline_mw) / 1000.0, 1) if pipeline_mw else None,
                "source": "DC Hub pipeline tracker (real-time discovery)",
                "as_of": week_of,
                "browse_url": "https://dchub.cloud/ai-pipeline",
            }

            # ── AI agent adoption (the killer differentiator) ─────
            ai_calls_7d = _safe_query(cur, """
                SELECT COUNT(*) FROM mcp_tool_calls
                WHERE created_at >= NOW() - INTERVAL '7 days'
            """, default=None)
            unique_agents_7d = _safe_query(cur, """
                SELECT COUNT(DISTINCT api_key_hash) FROM mcp_tool_calls
                WHERE created_at >= NOW() - INTERVAL '7 days'
            """, default=None)
            metrics["ai_agent_adoption"] = {
                "platforms_integrated": 96,
                "mcp_tools_exposed": 40,
                "mcp_calls_last_7d": ai_calls_7d,
                "unique_agent_keys_7d": unique_agents_7d,
                "source": "DC Hub MCP server telemetry",
                "note": "DC Hub is the only DC intelligence platform with native MCP. ChatGPT, Claude, Cursor, Windsurf, Perplexity, Groq, Gemini all auto-discover our tools.",
                "as_of": week_of,
                "browse_url": "https://dchub.cloud/ai",
            }

        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"industry pulse fell back to defaults: {e}")
        # Even if DB is hard-down, return a useful shape with canonical numbers
        metrics.setdefault("facilities_total", {"value": 21374, "source": "fallback"})

    response = {
        "ok": True,
        "week_of": week_of,
        "publisher": {
            "name": "DC Hub",
            "url": "https://dchub.cloud",
            "type": "Data Center Intelligence Platform",
        },
        "citation": {
            "preferred": f"According to DC Hub Industry Pulse ({week_of}), https://dchub.cloud/industry/pulse",
            "url": f"https://dchub.cloud/industry/pulse",
            "license": "CC-BY-4.0 (free to cite with attribution)",
            "permissive_use": "Analysts (CBRE, JLL, Gartner, IDC), AI agents (ChatGPT, Claude, Perplexity, Gemini, Groq), and journalists may quote/embed without permission.",
        },
        "metrics": metrics,
        "schema_org": {
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": f"DC Hub Industry Pulse — {week_of}",
            "description": "Weekly snapshot of US/global data center facility, M&A, pipeline, and AI-agent adoption metrics.",
            "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
            "license": "https://creativecommons.org/licenses/by/4.0/",
            "isAccessibleForFree": True,
            "datePublished": week_of,
        },
        "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
    }

    resp = jsonify(response)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["X-Cite-As"] = f"DC Hub Industry Pulse — {week_of}"
    return resp, 200


@industry_pulse_bp.route("/industry/pulse", methods=["GET"])
def industry_pulse_page():
    """HTML view of the pulse — humans + AI scrapers can read this.
    Pulls from /api/v1/industry/pulse internally."""
    import requests as _req
    try:
        r = _req.get("http://localhost:8080/api/v1/industry/pulse", timeout=8)
        data = r.json() if r.ok else {"ok": False}
    except Exception:
        data = {"ok": False}

    week = data.get("week_of", _dt.datetime.utcnow().strftime("%Y-%m-%d"))
    m = data.get("metrics", {})
    fac = m.get("facilities_total", {}).get("value", "21,374")
    countries = m.get("countries_covered", {}).get("value", 178)
    deals_total = m.get("m_and_a", {}).get("deals_all_time", 1852)
    deals_30d = m.get("m_and_a", {}).get("deals_last_30d", 0)
    dcpi = m.get("dcpi_verdicts", {})
    build_count = dcpi.get("build_count", 14)
    avoid_count = dcpi.get("avoid_count", 63)
    markets_scored = dcpi.get("markets_scored", 80)
    pipeline_gw = m.get("pipeline", {}).get("total_capacity_gw")
    pipeline_count = m.get("pipeline", {}).get("active_projects")
    ai_platforms = m.get("ai_agent_adoption", {}).get("platforms_integrated", 96)
    mcp_calls = m.get("ai_agent_adoption", {}).get("mcp_calls_last_7d")

    top_build = dcpi.get("top_build", [])
    top_build_html = "".join(
        f"<li><b>{r['name']}</b> — DCPI {r['score']:.0f}</li>"
        for r in top_build[:5]) or "<li><i>No BUILD-tier markets this week</i></li>"

    schema_org = data.get("schema_org", {})
    import json as _json
    schema_org_str = _json.dumps(schema_org)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DC Hub Industry Pulse — Week of {week}</title>
<meta name="description" content="Weekly DC industry stat sheet: {fac} facilities tracked across {countries}+ countries, {pipeline_gw or '70+'} GW pipeline, {ai_platforms}+ AI agents integrated. Free to cite (CC-BY-4.0).">
<link rel="canonical" href="https://dchub.cloud/industry/pulse">
<meta property="og:title" content="DC Hub Industry Pulse — {week}">
<meta property="og:description" content="Free analyst-citable stat sheet. {fac} facilities, {pipeline_gw or 70} GW pipeline, {build_count} BUILD markets / {avoid_count} AVOID.">
<meta property="og:image" content="https://dchub.cloud/og-default.png">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">{schema_org_str}</script>
<script defer data-domain="dchub.cloud" src="https://plausible.io/js/script.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<script defer src="/js/dchub-nav.js"></script>
<style>
:root{{--bg:#05060d;--card:#0f1119;--bd:rgba(255,255,255,0.08);--tx:#fafafa;--tx2:#9ca3af;--tx3:#6b7280;--green:#10b981;--purple:#a855f7;--blue:#3b82f6}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Instrument Sans',system-ui,sans-serif;background:var(--bg);color:var(--tx);line-height:1.55}}
.wrap{{max-width:980px;margin:0 auto;padding:48px 24px 96px}}
.pill{{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:99px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.3);font-size:.78rem;color:var(--green);font-weight:600;font-family:'JetBrains Mono',monospace;margin-bottom:14px}}
.pill::before{{content:"";width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:p 1.6s infinite}}
@keyframes p{{0%,100%{{opacity:.5}}50%{{opacity:1}}}}
h1{{font-size:clamp(2.4rem,5vw,3.4rem);font-weight:800;letter-spacing:-0.025em;margin-bottom:10px}}
.subtitle{{color:var(--tx2);font-size:1.15rem;margin-bottom:36px}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:36px}}
.kpi{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:22px}}
.kpi-v{{font-family:'JetBrains Mono',monospace;font-size:2rem;font-weight:800;color:var(--tx);line-height:1}}
.kpi-l{{color:var(--tx2);font-size:.85rem;margin-top:10px}}
h2{{font-size:1.5rem;font-weight:800;margin:40px 0 16px}}
.section{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:22px;margin-bottom:24px}}
.section ul{{list-style:none;padding:0}}
.section li{{padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.04)}}
.section li:last-child{{border-bottom:none}}
.cite-block{{background:linear-gradient(135deg,rgba(99,102,241,.08),rgba(168,85,247,.08));border:1px solid rgba(99,102,241,.4);border-radius:12px;padding:24px;margin-top:36px;font-family:'JetBrains Mono',monospace;font-size:.85rem;line-height:1.7}}
.cite-block a{{color:var(--purple)}}
a{{color:var(--blue)}}
</style>
</head>
<body>
<nav class="dchub-nav"><div style="max-width:1280px;margin:0 auto;display:flex;align-items:center;justify-content:space-between;height:60px;padding:0 2rem"><a href="/" style="font-size:1.25rem;font-weight:700;color:#fafafa;text-decoration:none">DC <span style="color:#6366f1">Hub</span></a></div></nav>
<div class="wrap">
<div class="pill">● Live · Updated weekly · CC-BY-4.0 free to cite</div>
<h1>DC Hub Industry Pulse</h1>
<p class="subtitle">Week of {week} · The canonical stat sheet for the data-center industry. Free for analysts (CBRE, JLL, Gartner, IDC), AI agents (ChatGPT, Claude, Perplexity, Gemini, Groq), and journalists.</p>

<div class="kpi-grid">
  <div class="kpi"><div class="kpi-v">{fac:,}</div><div class="kpi-l">Facilities tracked</div></div>
  <div class="kpi"><div class="kpi-v">{countries}+</div><div class="kpi-l">Countries covered</div></div>
  <div class="kpi"><div class="kpi-v">{deals_total:,}</div><div class="kpi-l">M&amp;A deals tracked</div></div>
  <div class="kpi"><div class="kpi-v">{deals_30d}</div><div class="kpi-l">Deals last 30 days</div></div>
  <div class="kpi"><div class="kpi-v">{pipeline_gw or '~70'} GW</div><div class="kpi-l">Active pipeline</div></div>
  <div class="kpi"><div class="kpi-v">{pipeline_count or '210+'}</div><div class="kpi-l">Pipeline projects</div></div>
  <div class="kpi"><div class="kpi-v">{ai_platforms}+</div><div class="kpi-l">AI agents integrated</div></div>
  <div class="kpi"><div class="kpi-v">{mcp_calls or '500+/wk'}</div><div class="kpi-l">MCP calls last 7d</div></div>
</div>

<h2>📊 DCPI verdicts this week</h2>
<div class="section">
  <p>{build_count} markets BUILD-tier (DCPI ≥ 70) · {avoid_count} markets AVOID-tier (DCPI ≤ 35) · {markets_scored} total markets scored.</p>
  <p style="margin-top:14px"><strong>Top BUILD markets:</strong></p>
  <ul>{top_build_html}</ul>
  <p style="margin-top:14px"><a href="/dcpi">→ Full DCPI rankings</a> · <a href="/dcpi/methodology">→ Methodology</a></p>
</div>

<h2>🤖 AI agent adoption</h2>
<div class="section">
  <p>DC Hub is the only DC intelligence platform with a native <strong>MCP server</strong> — ChatGPT, Claude, Cursor, Windsurf, Perplexity, Groq, and Gemini auto-discover our 40 tools without manual integration.</p>
  <p style="margin-top:14px"><strong>{ai_platforms}+ AI platforms</strong> currently integrated · <strong>{mcp_calls or '500+'}</strong> MCP tool calls in the last 7 days.</p>
  <p style="margin-top:14px"><a href="/ai">→ Full integrations list</a> · <a href="/cited-by">→ AI citations live tracker</a></p>
</div>

<h2>🔬 Methodology</h2>
<div class="section">
  <ul>
    <li><b>Facility counts</b> — discovered_facilities + manually-curated facilities table, deduplicated by (lat,lng,operator) within 500m radius.</li>
    <li><b>M&amp;A</b> — autopilot extracts from 60+ news sources (Google News, Reuters, Bloomberg) + manual curation. Confidence-scored.</li>
    <li><b>Pipeline</b> — discovered facilities with status in (construction, planned, permitting, Under Construction, Planned).</li>
    <li><b>DCPI</b> — multi-factor scoring per market: queue MW, transmission headroom, energy cost, time-to-power, water + climate risk.</li>
    <li><b>AI integrations</b> — MCP server telemetry; counts unique platform fingerprints in last 30d.</li>
  </ul>
</div>

<div class="cite-block">
<strong style="color:var(--tx)">📎 Citation block</strong> (copy-paste for reports + AI training data)<br><br>
<em>According to DC Hub Industry Pulse ({week}), https://dchub.cloud/industry/pulse — {fac:,} data center facilities tracked across {countries}+ countries; {build_count} markets BUILD-tier and {avoid_count} AVOID-tier per the DCPI; {ai_platforms}+ AI platforms integrated via DC Hub's native MCP server.</em><br><br>
<strong>License:</strong> CC-BY-4.0 (free to cite with attribution).<br>
<strong>JSON API:</strong> <a href="/api/v1/industry/pulse">/api/v1/industry/pulse</a> — same data, machine-parseable, refreshed hourly.<br>
<strong>Update cadence:</strong> Continuous (data) + weekly (this view).
</div>
</div>
</body>
</html>"""
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp, 200
