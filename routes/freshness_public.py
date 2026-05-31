"""Phase 268 — public /freshness surface.

Why this exists
---------------
The internal /heartbeat ops dashboard exists, but no *public* page proves
that dchub.cloud refreshes itself faster than DC Hawk / DC Byte /
datacenters.com (which the disruption audit confirmed don't even have
freshness signals — DCH has no AI surface, DCB's "MCP" is a WordPress 404,
datacenters.com rate-limits LLM crawlers entirely).

This module ships:

  • GET /freshness            — public HTML pitch page with live freshness
                                stats, intended for journalists / LLMs /
                                competitive deck citations.
  • GET /api/v1/freshness     — JSON companion. CORS '*' so anyone can poll.

Both pull from the same data the internal heartbeat already maintains:
the `freshness_checks` rows + DCPI quality summary.

Read-only. No writes. No auth. Heavy CDN caching is intentional (60s).

Phase GG (2026-05-14): fixed a silent bug — this module queried a table
named `heartbeat_surfaces` that NO code in the repo ever created or
wrote to (the real table heartbeat.py maintains is `freshness_checks`).
So `_surfaces_snapshot()` always returned [] + an error and the public
freshness page — the "proof we don't go stale" pitch page — was itself
empty. Repointed to the real table.
"""
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
from html import escape as _h
from flask import Blueprint, jsonify, Response

# phase 270 hardening: only these status values map to a CSS class; anything
# else gets rendered as "unknown". Defense-in-depth even though the DB writers
# today only produce these three.
_STATUS_WHITELIST = {"fresh", "stale", "unknown"}

freshness_public_bp = Blueprint("freshness_public", __name__)


def _conn():
    import psycopg2
    return psycopg2.connect(os.environ.get("DATABASE_URL"))


def _surfaces_snapshot():
    """Return list of surfaces with status (fresh/stale/unknown) + age."""
    rows = []
    try:
        import psycopg2.extras
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT surface,
                       last_updated,
                       stale_after_hours,
                       last_refresh_info
                FROM freshness_checks
                ORDER BY last_updated DESC NULLS LAST
            """)
            rows = cur.fetchall()
    except Exception as e:
        return [], str(e)
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        lu = r.get("last_updated")
        if lu and getattr(lu, "tzinfo", None) is None:
            lu = lu.replace(tzinfo=timezone.utc)
        age_h = ((now - lu).total_seconds() / 3600.0) if lu else None
        stale_after = r.get("stale_after_hours") or 24
        status = "unknown" if age_h is None else ("fresh" if age_h <= stale_after else "stale")
        out.append({
            "surface": r["surface"],
            "last_updated": lu.isoformat() if lu else None,
            "age_hours": round(age_h, 2) if age_h is not None else None,
            "stale_after_hours": stale_after,
            "status": status,
            "info": r.get("last_refresh_info"),
        })
    return out, None


def _dcpi_summary():
    """Last DCPI computed_at + total published markets."""
    try:
        import psycopg2.extras
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT COUNT(*) AS published, MAX(computed_at) AS last_computed
                FROM (
                  SELECT DISTINCT ON (market_slug) market_slug, computed_at
                  FROM market_power_scores WHERE published = true
                  ORDER BY market_slug, computed_at DESC
                ) t
            """)
            r = cur.fetchone() or {}
        last = r.get("last_computed")
        if last and getattr(last, "tzinfo", None) is None:
            last = last.replace(tzinfo=timezone.utc)
        age_min = ((datetime.now(timezone.utc) - last).total_seconds() / 60.0) if last else None
        return {
            "published_markets": int(r.get("published") or 0),
            "last_computed_at": last.isoformat() if last else None,
            "age_minutes": round(age_min, 1) if age_min is not None else None,
        }
    except Exception as e:
        return {"error": str(e)[:160]}


def _aggregate(surfaces):
    fresh = sum(1 for s in surfaces if s["status"] == "fresh")
    stale = sum(1 for s in surfaces if s["status"] == "stale")
    unknown = sum(1 for s in surfaces if s["status"] == "unknown")
    last_24h = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for s in surfaces:
        if s["last_updated"]:
            try:
                if datetime.fromisoformat(s["last_updated"].replace("Z", "+00:00")) >= cutoff:
                    last_24h += 1
            except Exception:
                pass
    return {"fresh": fresh, "stale": stale, "unknown": unknown,
            "total_surfaces": len(surfaces), "refreshed_last_24h": last_24h}


# Phase 296 (Phase O): per-domain SLA targets. Each data domain has a
# documented refresh target. Surfaces are grouped by domain; SLA-compliance
# is computed against the worst (oldest) surface in that domain.
#
# Hour values are tuned to actual upstream availability:
#   - ISO grid: 1h (real-time LMP feeds)
#   - Power retail rates: 168h (EIA monthly, ~lags 30-60d but pulled weekly)
#   - DCPI: 24h (daily 06:00 UTC recompute, plus emergency triggers)
#   - News: 1h (60+ source RSS poll)
#   - M&A: 24h (manual + scraped daily)
#   - Pipeline: 24h
#   - Renewables: 168h (NREL slow data)
#   - Gas: 24h
DOMAIN_SLA_HOURS = {
    # Phase YY (2026-05-17): right-size aspirational SLAs to match the
    # actual upstream cadence. iso=1h was unrealistic — the data-pulse
    # cron runs every 15 min but several specific surfaces (supported-
    # isos, summary, fuel-mix-live) update on EIA/gridstatus's hourly
    # cadence with their own lag. Anything stricter than upstream's
    # publish window guarantees a permanent breach finding that's just
    # noise. news=1h was the same trap — RSS feeds update hourly at
    # best, often every 2-4h. Use realistic operational targets:
    "iso":       4,      # /api/v1/grid/<iso>  (was 1h, raised to upstream LMP cadence)
    "power":     168,    # /api/v1/energy/electricity-rates
    "renewable": 168,    # /api/renewable/*
    "dcpi":      24,     # /api/v1/dcpi/live-count
    "news":      6,      # /api/news/live  (was 1h, raised to RSS aggregation cadence)
    "mna":       24,     # /api/v1/deals
    "pipeline":  24,     # /api/v1/pipeline
    "fiber":     168,    # /api/v1/connectivity/*
    "gas":       24,     # /api/v1/energy/gas-*
    "facilities": 24,    # /api/v1/facilities
}


def _domain_of(surface_name: str) -> str:
    """Map a surface name to one of the SLA domains.

    Phase YY-2 (2026-05-17): EXCLUDE agent-discovery surfaces
    (`/ai/learn/*`, `/api/agent/*`, `/ai/schema/*`, `/api/_diagnose/*`)
    from data-freshness SLAs. These track 'last-pinged-by-an-agent'
    timestamps, not underlying data age. Misclassifying them was the
    root cause of Phase TT's persistent ISO/news 'breach' findings
    (worst was always 29-35h because those endpoints get hit by AI
    crawlers on their own cadence — has nothing to do with our cron).
    Real data-source surfaces (e.g. /api/v1/grid/<iso>) are still
    monitored at their proper SLA.
    """
    s = (surface_name or "").lower()
    # ── Agent-discovery / learning surfaces — measured as 'other'
    # (no SLA reported) so they don't pollute data-source breach signal.
    if (s.startswith("/ai/learn") or s.startswith("/ai/schema")
            or s.startswith("/api/agent") or s.startswith("/api/_diagnose")):
        return "other"
    # Phase r34 (2026-05-31): EXCLUDE operational/internal surfaces for the
    # same reason. admin dashboards, ingest writers, CSV exports and draft
    # generators are hit on their own (or no) cadence — their "age" tracks
    # the last admin/ingest action, NOT user-facing data freshness. They were
    # dragging iso/news/dcpi/gas/facilities/mna/pipeline into perpetual breach
    # (all ~155-168h = "last touched a week ago", while the real public feeds
    # — dcpi 42min, fiber/power/renewable current — are fine). Demote to 'other'.
    _OPS_MARKERS = ("/admin/", "/ingest", "/export", "/draft-", "/draft/",
                    "ner/status", "/recompute", "/backfill", "/dedup",
                    "/probe/", "/import", "/upload", "/sync")
    if any(m in s for m in _OPS_MARKERS):
        return "other"
    if "grid" in s or "iso" in s: return "iso"
    if "renewable" in s or "solar" in s or "wind" in s: return "renewable"
    if "rate" in s or "energy" in s and "gas" not in s: return "power"
    if "dcpi" in s: return "dcpi"
    if "news" in s or "press" in s: return "news"
    if "deal" in s or "transaction" in s or "m&a" in s: return "mna"
    if "pipeline" in s and "gas" not in s: return "pipeline"
    if "fiber" in s or "ix" in s or "connectivity" in s: return "fiber"
    if "gas" in s: return "gas"
    if "facility" in s or "facilities" in s: return "facilities"
    return "other"


def _sla_breakdown(surfaces):
    """Compute per-domain SLA compliance. Returns
    {domain: {target_h, worst_age_h, status, surfaces_n, worst_surface}}.

    Phase TT (2026-05-17): also expose `worst_surface` so ops can see
    WHICH specific surface is dragging the domain into breach. Without
    this, the freshness endpoint reported 'iso: breach worst=26h' with
    no way to tell whether one dead ISO surface or all 55 are stale.
    """
    by_domain = {}
    for s in surfaces:
        d = _domain_of(s.get("surface", ""))
        by_domain.setdefault(d, []).append(s)
    out = {}
    for domain, ss in by_domain.items():
        target = DOMAIN_SLA_HOURS.get(domain)
        if target is None:
            continue  # 'other' bucket — don't report SLA
        # Sort by age desc so the head is the worst offender
        rated = [(s.get("age_hours"), s.get("surface", "?")) for s in ss
                 if s.get("age_hours") is not None]
        rated.sort(reverse=True)
        worst_age = rated[0][0] if rated else None
        worst_surface = rated[0][1] if rated else None
        # Phase TT (2026-05-17): show the top-3 stale surfaces in each
        # breaching domain so ops know which ones to investigate. List
        # is omitted when status is within_sla (no signal needed).
        stale_list = [{"surface": surf, "age_hours": round(age, 2)}
                       for age, surf in rated[:3] if age > target]
        if worst_age is None:
            status = "unknown"
        elif worst_age <= target:
            status = "within_sla"
        elif worst_age <= target * 2:
            status = "warning"  # 1-2x the SLA target
        else:
            status = "breach"   # >2x the SLA target
        entry = {
            "target_hours":     target,
            "worst_age_hours":  round(worst_age, 2) if worst_age is not None else None,
            "worst_surface":    worst_surface,
            "status":           status,
            "surfaces":         len(ss),
        }
        if status in ("warning", "breach") and stale_list:
            entry["stale_surfaces"] = stale_list
        out[domain] = entry
    return out


@freshness_public_bp.route("/api/v1/freshness", methods=["GET"])
def api_freshness():
    """JSON freshness snapshot. CORS '*'. Cache 60s."""
    surfaces, err = _surfaces_snapshot()
    # Phase 296 (Phase O): per-domain SLA breakdown — turns the raw surface
    # list into "is each data domain meeting its refresh target?" — same
    # signal a status-page would expose. Used by /freshness HTML and by AI
    # agents to decide whether to trust the data.
    sla = _sla_breakdown(surfaces)
    breaches = [d for d, info in sla.items() if info.get("status") == "breach"]
    body = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "summary": _aggregate(surfaces),
        "dcpi": _dcpi_summary(),
        "sla_by_domain": sla,                     # phase 296
        "sla_breaches": breaches,                 # phase 296
        "sla_overall": "all_within_sla" if not breaches
                       else f"{len(breaches)}_domains_breached",
        "surfaces": surfaces,
        "citation": "DC Hub freshness signal — public proof-of-self-heal. https://dchub.cloud/freshness",
    }
    if err:
        body["surfaces_error"] = err
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


_FRESHNESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Hub · Freshness — live proof of self-healing data</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Live freshness signal for DC Hub. {{fresh}} of {{total}} data surfaces refreshed in the last 24 hours. DCPI recomputed {{dcpi_age}} ago.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/freshness">
<meta property="og:title" content="DC Hub · Live Data Freshness">
<meta property="og:description" content="{{fresh}} of {{total}} data surfaces refreshed in last 24h. DCPI: {{dcpi_age}} ago.">
<meta property="og:url" content="https://dchub.cloud/freshness">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "DataFeed",
  "name": "DC Hub freshness feed",
  "description": "Live freshness signal across all DC Hub data surfaces. {{fresh}} of {{total}} surfaces refreshed in the last 24h.",
  "url": "https://dchub.cloud/freshness",
  "isAccessibleForFree": true,
  "publisher": {"@type": "Organization", "name": "DC Hub", "url": "https://dchub.cloud"},
  "distribution": {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": "https://dchub.cloud/api/v1/freshness"}
}
</script>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<script src="/js/dchub-nav.js" defer></script>
<style>
:root{--bg:#0a0a0f;--bg2:#0f1119;--card:#131319;--bd:rgba(255,255,255,.08);--tx:#fafafa;--tx2:#a1a1aa;--tx3:#71717a;--green:#10b981;--red:#ef4444;--orange:#f59e0b;--acc:#6366f1;--gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);}
*{box-sizing:border-box}
body{font-family:'Instrument Sans',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--tx);margin:0;line-height:1.55;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1100px;margin:0 auto;padding:3rem 1.5rem;}
.eyebrow{font-family:'JetBrains Mono',monospace;font-size:0.75rem;color:var(--acc);text-transform:uppercase;letter-spacing:0.12em;margin-bottom:0.6rem;}
h1{font-size:2.6rem;margin:0 0 0.6rem;font-weight:800;letter-spacing:-0.025em;line-height:1.1;}
h1 .live{display:inline-block;width:14px;height:14px;background:var(--green);border-radius:50%;margin-right:0.6rem;animation:pulse 1.4s ease-in-out infinite;vertical-align:middle;}
@keyframes pulse{50%{opacity:0.3;transform:scale(0.85);}}
.lede{color:var(--tx2);font-size:1.1rem;max-width:760px;margin:0 0 2.4rem;}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;margin:2rem 0 2.6rem;}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:1.3rem 1.4rem;}
.kpi .v{font-family:'JetBrains Mono',monospace;font-size:2.1rem;font-weight:800;line-height:1;}
.kpi .v.green{color:var(--green);}.kpi .v.red{color:var(--red);}.kpi .v.orange{color:var(--orange);}
.kpi .l{color:var(--tx2);font-size:0.78rem;margin-top:0.55rem;text-transform:uppercase;letter-spacing:0.08em;}
.kpi .sub{color:var(--tx3);font-size:0.78rem;margin-top:0.35rem;}
.section-title{font-size:1.15rem;font-weight:700;margin:2.4rem 0 1rem;letter-spacing:-0.01em;}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;font-size:0.9rem;}
th,td{text-align:left;padding:0.7rem 1rem;border-bottom:1px solid var(--bd);}
th{background:var(--bg2);color:var(--tx2);font-weight:600;font-size:0.74rem;text-transform:uppercase;letter-spacing:0.08em;}
tr:last-child td{border-bottom:none;}
td.mono{font-family:'JetBrains Mono',monospace;font-size:0.86rem;}
.status{display:inline-block;padding:2px 8px;border-radius:99px;font-size:0.7rem;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;}
.status.fresh{background:rgba(16,185,129,0.15);color:var(--green);}
.status.stale{background:rgba(239,68,68,0.15);color:var(--red);}
.status.unknown{background:rgba(156,163,175,0.15);color:var(--tx2);}
.cite{margin-top:3rem;padding:1.2rem 1.4rem;background:var(--bg2);border:1px solid var(--bd);border-radius:10px;color:var(--tx2);font-size:0.88rem;}
.cite code{background:#11121a;padding:2px 6px;border-radius:4px;color:var(--tx);font-family:'JetBrains Mono',monospace;font-size:0.84rem;}
a{color:var(--acc);text-decoration:none;border-bottom:1px dotted rgba(99,102,241,0.5);}
a:hover{color:#fff;border-bottom-color:#fff;}
.foot{margin-top:3rem;color:var(--tx3);font-size:0.8rem;}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">Live · proof of self-heal</div>
  <h1><span class="live"></span>Data freshness, in public</h1>
  <p class="lede">
    DC Hub's data is recomputed and self-healed continuously. Every public surface
    on this site reports its own freshness here, in real time. Compare against any
    other data-center intelligence source — most don't publish this at all.
  </p>

  <div class="kpis">
    <div class="kpi"><div class="v green">{{refreshed_24h}}</div><div class="l">Refreshed in last 24h</div><div class="sub">across {{total}} surfaces</div></div>
    <div class="kpi"><div class="v {{dcpi_class}}">{{dcpi_age}}</div><div class="l">DCPI last computed</div><div class="sub">{{dcpi_published}} markets published</div></div>
    <div class="kpi"><div class="v {{fresh_class}}">{{fresh}}/{{total}}</div><div class="l">Currently fresh</div><div class="sub">{{stale}} stale · {{unknown}} unknown</div></div>
    <div class="kpi"><div class="v">JSON</div><div class="l">Machine-readable</div><div class="sub"><a href="/api/v1/freshness">/api/v1/freshness</a></div></div>
  </div>

  <div class="section-title">Per-surface freshness</div>
  <table>
    <thead><tr><th>Surface</th><th>Status</th><th>Age</th><th>Stale after</th><th>Last note</th></tr></thead>
    <tbody>
{{rows_html}}
    </tbody>
  </table>

  <div class="cite">
    <strong>Cite this signal:</strong>
    <code>DC Hub freshness — https://dchub.cloud/freshness</code><br>
    Machine surface: <code>GET https://dchub.cloud/api/v1/freshness</code> (CORS open, 60s cache).<br>
    Methodology: <a href="/dcpi#methodology">/dcpi#methodology</a> · <a href="/audit/">site audit</a>
  </div>

  <p class="foot">As of {{as_of}}. This page is rendered fresh on every load.
  Healer detection findings: <a href="/api/v1/heal/findings">/api/v1/heal/findings</a>.</p>
</div>
</body>
</html>"""


@freshness_public_bp.route("/freshness", methods=["GET"])
def freshness_page():
    surfaces, _err = _surfaces_snapshot()
    summary = _aggregate(surfaces)
    dcpi = _dcpi_summary()

    dcpi_age_min = dcpi.get("age_minutes")
    if dcpi_age_min is None:
        dcpi_age = "—"
        dcpi_class = "orange"
    elif dcpi_age_min < 60:
        dcpi_age = f"{int(dcpi_age_min)}m"
        dcpi_class = "green"
    elif dcpi_age_min < 1440:
        dcpi_age = f"{int(dcpi_age_min/60)}h"
        dcpi_class = "green" if dcpi_age_min < 360 else "orange"
    else:
        dcpi_age = f"{int(dcpi_age_min/1440)}d"
        dcpi_class = "red"

    fresh_class = "green" if summary["stale"] == 0 else ("orange" if summary["fresh"] > summary["stale"] else "red")

    # phase 270 hardening: HTML-escape every field that comes from the DB.
    # `status` is whitelisted to known values so it can't break out of the
    # CSS class attribute; everything else uses html.escape().
    rows = []
    for s in surfaces[:80]:
        age = "—" if s["age_hours"] is None else (f"{int(s['age_hours']*60)}m" if s["age_hours"] < 1 else f"{int(s['age_hours'])}h")
        info = (s["info"] or "")[:90]
        status = s["status"] if s["status"] in _STATUS_WHITELIST else "unknown"
        rows.append(
            f'<tr><td class="mono">{_h(str(s["surface"]))}</td>'
            f'<td><span class="status {status}">{status}</span></td>'
            f'<td class="mono">{_h(age)}</td>'
            f'<td class="mono">{int(s["stale_after_hours"] or 24)}h</td>'
            f'<td>{_h(info)}</td></tr>'
        )
    html = (_FRESHNESS_HTML
            .replace("{{refreshed_24h}}", str(summary["refreshed_last_24h"]))
            .replace("{{fresh}}", str(summary["fresh"]))
            .replace("{{stale}}", str(summary["stale"]))
            .replace("{{unknown}}", str(summary["unknown"]))
            .replace("{{total}}", str(summary["total_surfaces"]))
            .replace("{{dcpi_age}}", dcpi_age)
            .replace("{{dcpi_class}}", dcpi_class)
            .replace("{{dcpi_published}}", str(dcpi.get("published_markets", 0)))
            .replace("{{fresh_class}}", fresh_class)
            .replace("{{rows_html}}", "\n".join(rows))
            .replace("{{as_of}}", datetime.now(timezone.utc).isoformat()))
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    return resp
