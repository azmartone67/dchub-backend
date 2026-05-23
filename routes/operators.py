"""Phase YYYY (2026-05-16) — operator profiles + activity feed.

User strategic ask: where do DCHawk + dcByte dominate that we can
close? Answer: per-operator profile pages. They have rich Equinix /
Digital Realty / Iron Mountain pages with portfolio + M&A history;
we have the data but no UI/API surface.

This module ships:
  GET /operators                  list top 50 operators by facility count
  GET /operators/<slug>           per-operator profile (HTML)
  GET /api/v1/operators           JSON list (sitemap-friendly)
  GET /api/v1/operators/<slug>    JSON profile (for MCP + AI agents)
  GET /api/v1/activity/recent     live feed: last 50 events across surfaces

Each operator profile aggregates:
  - Facility count + total MW (operating + pipeline)
  - Markets where they operate (top 10)
  - Recent M&A deals involving the operator (as buyer or seller)
  - Schema.org Organization + ItemList markup so AI agents fact-cite

Brain detector check_operator_profile_gap surfaces top operators
that lack rich data (e.g., facility count high but no website url,
no markets identified) so the discovery pipeline can prioritize fills.
"""

from __future__ import annotations

import os
import re
import datetime
from flask import Blueprint, Response, jsonify, request, abort


operators_bp = Blueprint("operators", __name__)


def _conn():
    import psycopg2
    db = os.environ.get("DATABASE_URL")
    if not db: return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


def _slugify(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80]


# Phase ZZZZZ-round4 (2026-05-23): slug aliases for common short names.
# Search-engine + AI-agent traffic types "/operators/aws", not "/operators/
# amazon-web-services". Map the short form to the canonical provider name
# (case-insensitive). Add new entries here when SEO logs show 404s on
# canonical short forms (azure, gcp, fb, etc.).
SLUG_ALIASES = {
    "aws":              "Amazon Web Services",
    "amazon":           "Amazon Web Services",
    "azure":            "Microsoft",
    "ms":               "Microsoft",
    "gcp":              "Google",
    "google-cloud":     "Google",
    "fb":               "Meta",
    "facebook":         "Meta",
    "ali":              "Alibaba",
    "alibaba-cloud":    "Alibaba",
    "tencent-cloud":    "Tencent",
    "oci":              "Oracle",
    "oracle-cloud":     "Oracle",
    "ibm-cloud":        "IBM",
    "dr":               "Digital Realty",
    "eq":               "Equinix",
    "im":               "Iron Mountain",
    "ntt":              "NTT Global Data Centers",
    "sti":              "STACK Infrastructure",
    "cyrus":            "CyrusOne",
    "qts":              "QTS",
    "compass":          "Compass Datacenters",
    "vantage":          "Vantage Data Centers",
    "edgeconnex":       "EdgeConneX",
    "switch":           "Switch",
    "cologix":          "Cologix",
    "coresite":         "CoreSite",
}


def _operator_summary(cur, name: str) -> dict | None:
    """Aggregate one operator from discovered_facilities + deals."""
    try:
        cur.execute("""
            SELECT COUNT(*) AS facility_count,
                   COALESCE(SUM(power_mw), 0) AS total_mw,
                   COUNT(*) FILTER (WHERE LOWER(COALESCE(status,'')) IN
                       ('operational','operating','live','active','running','in-service')) AS operating_count,
                   COUNT(*) FILTER (WHERE LOWER(COALESCE(status,'')) IN
                       ('construction','planned','permitting','under construction','proposed','development')) AS pipeline_count,
                   COUNT(DISTINCT country) AS countries,
                   COUNT(DISTINCT state) AS states_us
              FROM discovered_facilities
             WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
               AND merged_at IS NULL AND is_duplicate = 0
        """, (name,))
        r = cur.fetchone()
        if not r or not r[0]: return None
        out = {
            "name":            name,
            "slug":            _slugify(name),
            "facility_count":  int(r[0] or 0),
            "total_mw":        float(r[1] or 0),
            "operating_count": int(r[2] or 0),
            "pipeline_count":  int(r[3] or 0),
            "countries":       int(r[4] or 0),
            "states_us":       int(r[5] or 0),
        }
        # Top markets
        try:
            cur.execute("""
                SELECT COALESCE(market, city, '') AS m, COUNT(*) AS n
                  FROM discovered_facilities
                 WHERE LOWER(COALESCE(provider, '')) = LOWER(%s)
                   AND merged_at IS NULL AND is_duplicate = 0
                   AND COALESCE(market, city) IS NOT NULL
                 GROUP BY COALESCE(market, city)
                 ORDER BY n DESC LIMIT 10
            """, (name,))
            out["top_markets"] = [{"market": r[0], "facilities": int(r[1])}
                                   for r in cur.fetchall() if r[0]]
        except Exception:
            out["top_markets"] = []
        # Recent deals (buyer OR seller)
        try:
            cur.execute("""
                SELECT id, date, buyer, seller, value, mw, type, region
                  FROM deals
                 WHERE LOWER(COALESCE(buyer, '')) LIKE LOWER(%s)
                    OR LOWER(COALESCE(seller, '')) LIKE LOWER(%s)
                 ORDER BY date DESC NULLS LAST LIMIT 10
            """, (f"%{name}%", f"%{name}%"))
            out["recent_deals"] = [{
                "id":     int(r[0]) if r[0] else None,
                "date":   r[1].isoformat() if hasattr(r[1], "isoformat") else (str(r[1]) if r[1] else None),
                "buyer":  r[2], "seller": r[3],
                "value":  float(r[4]) if r[4] is not None else None,
                "mw":     float(r[5]) if r[5] is not None else None,
                "type":   r[6], "region": r[7],
            } for r in cur.fetchall()]
        except Exception:
            out["recent_deals"] = []
        return out
    except Exception:
        return None


def _top_operators(cur, limit: int = 50) -> list[dict]:
    try:
        cur.execute("""
            SELECT provider, COUNT(*) AS n, COALESCE(SUM(power_mw), 0) AS mw
              FROM discovered_facilities
             WHERE provider IS NOT NULL AND provider != ''
               AND merged_at IS NULL AND is_duplicate = 0
             GROUP BY provider
             ORDER BY n DESC LIMIT %s
        """, (limit,))
        return [{"name": r[0], "slug": _slugify(r[0]),
                  "facility_count": int(r[1]), "total_mw": float(r[2] or 0)}
                 for r in cur.fetchall()]
    except Exception:
        return []


# ── JSON endpoints (AI-agent friendly) ──────────────────────────

@operators_bp.route("/api/v1/operators", methods=["GET"])
def api_operators_list():
    try: limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except (ValueError, TypeError): limit = 50
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            ops = _top_operators(cur, limit=limit)
    finally:
        try: c.close()
        except Exception: pass
    resp = jsonify(operators=ops, count=len(ops),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z",
                   note="Top operators by facility count. Per-operator detail at /api/v1/operators/<slug>.")
    resp.headers["Cache-Control"] = "public, max-age=600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


def _resolve_slug_to_provider(cur, slug: str) -> str | None:
    """Map a URL slug to the canonical provider name.
    Checks aliases first, then does exact slug match against the DB."""
    alias_target = SLUG_ALIASES.get(slug.lower())
    if alias_target:
        return alias_target
    cur.execute("""
        SELECT DISTINCT provider FROM discovered_facilities
         WHERE provider IS NOT NULL AND provider != ''
           AND merged_at IS NULL AND is_duplicate = 0
    """)
    for r in cur.fetchall():
        if _slugify(r[0]) == slug:
            return r[0]
    return None


@operators_bp.route("/api/v1/operators/<slug>", methods=["GET"])
def api_operator_detail(slug):
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    try:
        with c.cursor() as cur:
            provider = _resolve_slug_to_provider(cur, slug)
            if provider:
                summary = _operator_summary(cur, provider)
                if summary:
                    # If user hit an alias, tell them the canonical slug
                    if summary["slug"] != slug:
                        summary["alias_for"] = summary["slug"]
                        summary["canonical_url"] = f"/api/v1/operators/{summary['slug']}"
                    resp = jsonify(summary)
                    resp.headers["Cache-Control"] = "public, max-age=600"
                    resp.headers["Access-Control-Allow-Origin"] = "*"
                    return resp, 200
    finally:
        try: c.close()
        except Exception: pass
    return jsonify(error="operator_not_found", slug=slug,
                   hint="Try /api/v1/operators for the canonical list."), 404


# ── HTML pages ──────────────────────────────────────────────────

@operators_bp.route("/operators", methods=["GET"], strict_slashes=False)
def operators_index():
    """Public index page — top 50 operators by facility count."""
    try:
        from routes.surface_brain import auto_log
        auto_log("operators", "view", target="/operators")
    except Exception: pass

    c = _conn()
    ops = []
    if c is not None:
        try:
            with c.cursor() as cur:
                ops = _top_operators(cur, limit=50)
        finally:
            try: c.close()
            except Exception: pass

    rows = "".join(
        f'<tr><td>{i+1}</td>'
        f'<td><a href="/operators/{o["slug"]}">{o["name"]}</a></td>'
        f'<td>{o["facility_count"]:,}</td>'
        f'<td>{o["total_mw"]:,.0f}</td></tr>'
        for i, o in enumerate(ops)
    )
    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>Data Center Operators · {len(ops)} tracked | DC Hub</title>
<meta name="description" content="Live directory of {len(ops)} data center operators. Per-operator portfolio, total MW, top markets, M&A history. Free, indexable, AI-agent discoverable.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/operators">
<script type="application/ld+json">{{
 "@context": "https://schema.org", "@type": "ItemList",
 "numberOfItems": {len(ops)},
 "url": "https://dchub.cloud/operators",
 "itemListElement": [
  {','.join(f'{{"@type":"ListItem","position":{i+1},"name":"{o["name"]}","url":"https://dchub.cloud/operators/{o["slug"]}"}}' for i, o in enumerate(ops))}
 ]
}}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>body{{font-family:'Instrument Sans',-apple-system,sans-serif;max-width:1100px;margin:0 auto;
padding:2rem 1rem;color:var(--dch-text);line-height:1.55;background:var(--dch-bg)}}
h1{{margin:0 0 .25rem;font-size:1.85rem}}
.sub{{color:var(--dch-text-mute);margin:0 0 1.5rem}}
table{{width:100%;border-collapse:collapse;font-size:.95rem;background:var(--dch-surface);
border-radius:8px;overflow:hidden;border:1px solid var(--dch-border)}}
th{{text-align:left;padding:.6rem .8rem;background:var(--dch-surface-2);font-size:.8rem;
text-transform:uppercase;color:var(--dch-text-mute);font-weight:600}}
td{{padding:.5rem .8rem;border-top:1px solid var(--dch-border)}}
td:first-child{{color:var(--dch-text-dim);font-family:'JetBrains Mono',monospace}}
a{{color:#818cf8;text-decoration:none}} a:hover{{text-decoration:underline;color:#a855f7}}
.foot{{color:var(--dch-text-dim);font-size:.85rem;text-align:center;margin-top:2rem}}</style>
</head><body>
<h1>Data Center Operators</h1>
<p class="sub">Top {len(ops)} operators by tracked facility count. Click any name for the per-operator portfolio.</p>
<table>
 <thead><tr><th>#</th><th>Operator</th><th>Facilities</th><th>Total MW</th></tr></thead>
 <tbody>{rows or '<tr><td colspan=4 style="text-align:center;color:#9ca3af;padding:2rem">No operators tracked yet.</td></tr>'}</tbody>
</table>
<p class="foot">Live: <a href="/api/v1/operators">/api/v1/operators</a> · Brand: <a href="/vs">vs static competitors</a> · Ops: <a href="/transparency">transparency console</a></p>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})


@operators_bp.route("/operators/<slug>", methods=["GET"])
def operator_page(slug):
    """Per-operator profile page (HTML). schema.org Organization markup."""
    from flask import redirect
    c = _conn()
    summary = None
    if c is not None:
        try:
            with c.cursor() as cur:
                provider = _resolve_slug_to_provider(cur, slug)
                if provider:
                    summary = _operator_summary(cur, provider)
                    # If the user hit an alias slug, 301 to canonical for SEO
                    if summary and summary["slug"] != slug:
                        return redirect(f"/operators/{summary['slug']}", code=301)
        finally:
            try: c.close()
            except Exception: pass
    if not summary:
        abort(404)

    try:
        from routes.surface_brain import auto_log
        auto_log("operators", "view_profile", target=slug)
    except Exception: pass

    deals_rows = "".join(
        f'<tr><td>{d.get("date","")}</td>'
        f'<td>{d.get("buyer") or ""}</td>'
        f'<td>{d.get("seller") or ""}</td>'
        f'<td>{("$"+format(d["value"],",.0f")) if d.get("value") else ""}</td>'
        f'<td>{(format(d["mw"],",.0f")+" MW") if d.get("mw") else ""}</td>'
        f'<td>{d.get("type") or ""}</td></tr>'
        for d in (summary.get("recent_deals") or [])[:10]
    ) or '<tr><td colspan=6 style="text-align:center;color:#9ca3af;padding:.8rem">No tracked deals yet.</td></tr>'

    market_chips = "".join(
        f'<span style="display:inline-block;background:rgba(129,140,248,.15);color:#818cf8;'
        f'padding:.25rem .65rem;border-radius:999px;font-size:.85rem;margin:.15rem">'
        f'{m["market"]} <strong>×{m["facilities"]}</strong></span>'
        for m in (summary.get("top_markets") or [])[:10]
    ) or '<span style="color:#9ca3af">No market data tracked yet.</span>'

    html = f"""<!doctype html><html lang=en>
<head><meta charset=utf-8>
<title>{summary['name']} · Data Center Operator Profile | DC Hub</title>
<meta name="description" content="{summary['name']} operates {summary['facility_count']} tracked data centers totaling {summary['total_mw']:,.0f} MW across {summary['countries']} countries. Live portfolio + M&A history from DC Hub.">
<meta name="robots" content="index,follow,max-snippet:-1">
<link rel="canonical" href="https://dchub.cloud/operators/{slug}">
<meta property="og:title" content="{summary['name']} — DC Hub operator profile">
<script type="application/ld+json">{{
 "@context":"https://schema.org","@type":"Organization",
 "name":"{summary['name']}","url":"https://dchub.cloud/operators/{slug}",
 "description":"Data center operator tracked by DC Hub. {summary['facility_count']} facilities, {summary['total_mw']:,.0f} MW total, {summary['countries']} countries."
}}</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dchub-brand.css">
<style>body{{font-family:'Instrument Sans',-apple-system,sans-serif;max-width:1000px;margin:0 auto;
padding:2rem 1rem;color:var(--dch-text);line-height:1.55;background:var(--dch-bg)}}
h1{{margin:0 0 .25rem;font-size:2rem}}
.sub{{color:var(--dch-text-mute);margin:0 0 1.5rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.75rem;margin:1rem 0 2rem}}
.card{{background:var(--dch-surface);padding:1rem 1.2rem;border-radius:8px;border:1px solid var(--dch-border)}}
.card-label{{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dch-text-mute);font-weight:600}}
.card-metric{{font-size:1.8rem;font-weight:800;color:var(--dch-text);line-height:1;margin-top:.3rem}}
h2{{font-size:1rem;color:var(--dch-text-mute);text-transform:uppercase;letter-spacing:.08em;margin:1.5rem 0 .5rem}}
table{{width:100%;border-collapse:collapse;font-size:.92rem;background:var(--dch-surface);border-radius:8px;overflow:hidden;border:1px solid var(--dch-border)}}
th{{text-align:left;padding:.55rem .75rem;background:var(--dch-surface-2);font-size:.75rem;
text-transform:uppercase;color:var(--dch-text-mute);font-weight:600}}
td{{padding:.5rem .75rem;border-top:1px solid var(--dch-border)}}
a{{color:#818cf8;text-decoration:none}} a:hover{{text-decoration:underline;color:#a855f7}}
.back{{color:var(--dch-text-mute);font-size:.9rem}}
.foot{{color:var(--dch-text-dim);font-size:.85rem;text-align:center;margin-top:2rem}}</style>
</head><body>
<p class="back"><a href="/operators">← All operators</a></p>
<h1>{summary['name']}</h1>
<p class="sub">Live operator portfolio · tracked by DC Hub from public sources</p>
<div class="grid">
 <div class="card"><div class="card-label">Facilities</div><div class="card-metric">{summary['facility_count']:,}</div></div>
 <div class="card"><div class="card-label">Total MW</div><div class="card-metric">{summary['total_mw']:,.0f}</div></div>
 <div class="card"><div class="card-label">Operating</div><div class="card-metric">{summary['operating_count']:,}</div></div>
 <div class="card"><div class="card-label">Pipeline</div><div class="card-metric">{summary['pipeline_count']:,}</div></div>
 <div class="card"><div class="card-label">Countries</div><div class="card-metric">{summary['countries']}</div></div>
</div>
<h2>Top markets</h2>
<p>{market_chips}</p>
<h2>Recent M&A involving {summary['name']}</h2>
<table>
 <thead><tr><th>Date</th><th>Buyer</th><th>Seller</th><th>Value</th><th>MW</th><th>Type</th></tr></thead>
 <tbody>{deals_rows}</tbody>
</table>
<p class="foot">Live JSON: <a href="/api/v1/operators/{slug}">/api/v1/operators/{slug}</a> · Indexed by AI agents via MCP — call <code>search_facilities(operator="{summary['name']}")</code></p>
<script src="/js/dchub-nav.js" defer></script>
</body></html>"""
    return Response(html, mimetype="text/html",
                    headers={"Cache-Control": "public, max-age=600"})


# ── Live activity feed ─────────────────────────────────────────

@operators_bp.route("/api/v1/activity/recent", methods=["GET"])
def activity_recent():
    """Live feed of recent activity across surfaces — deals, new
    facilities discovered, citations, dormant agents. Powers a
    'what changed today' widget on /transparency."""
    c = _conn()
    if c is None: return jsonify(error="no_database"), 503
    out: list[dict] = []
    try:
        import psycopg2.extras
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Last 20 deals
            try:
                cur.execute("""
                    SELECT id, date, buyer, seller, value, mw
                      FROM deals
                     WHERE date IS NOT NULL
                     ORDER BY date DESC LIMIT 20
                """)
                for r in cur.fetchall():
                    out.append({
                        "type":     "deal",
                        "ts":       r["date"].isoformat() if hasattr(r["date"], "isoformat") else str(r["date"]),
                        "title":    f"{r['buyer'] or '?'} → {r['seller'] or '?'}",
                        "value_usd":float(r["value"]) if r["value"] is not None else None,
                        "mw":       float(r["mw"]) if r["mw"] is not None else None,
                        "url":      f"/transactions/{r['id']}" if r["id"] else None,
                    })
            except Exception: pass
            # Last 20 facilities discovered
            try:
                cur.execute("""
                    SELECT name, provider, state, country, first_seen
                      FROM discovered_facilities
                     WHERE first_seen IS NOT NULL
                       AND merged_at IS NULL AND is_duplicate = 0
                     ORDER BY first_seen DESC LIMIT 20
                """)
                for r in cur.fetchall():
                    out.append({
                        "type":     "facility_discovered",
                        "ts":       r["first_seen"].isoformat() if r["first_seen"] else None,
                        "title":    f"{r['name'] or 'New facility'} · {r['provider'] or ''}",
                        "location": (f"{r['state'] or ''}, {r['country'] or ''}").strip(", "),
                        "url":      f"/operators/{_slugify(r['provider'])}" if r["provider"] else None,
                    })
            except Exception: pass
    finally:
        try: c.close()
        except Exception: pass

    # Sort across types by ts desc; cap 30
    out.sort(key=lambda x: x.get("ts") or "", reverse=True)
    out = out[:30]

    resp = jsonify(activity=out, count=len(out),
                   generated_at=datetime.datetime.utcnow().isoformat() + "Z",
                   note="Mixed feed of recent platform activity. Powers 'what changed today' widget. Always public, cached 5min.")
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200
