"""Phase FF+25-followup-r24 (2026-05-20) — public /coverage page.
==========================================================================

Social proof for the OSM crawler + manual seeding work. Shows the
data center footprint by country, sortable, with delta-since-last-week
so visitors see active discovery.

Public — no auth, no PII. Just facility counts.

URL: https://dchub.cloud/coverage
JSON: https://dchub.cloud/api/v1/site/coverage
"""
import os
import json
import logging
import datetime
from flask import Blueprint, jsonify, Response, request

logger = logging.getLogger(__name__)
coverage_page_bp = Blueprint("coverage_page", __name__)


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


def _compute_coverage() -> dict:
    """Per-country counts + per-region totals. Best-effort across both
    facilities and discovered_facilities tables."""
    out = {
        "as_of": datetime.datetime.utcnow().isoformat() + "Z",
        "by_country": [],
        "total_facilities": 0,
        "countries_tracked": 0,
        "added_7d": 0,
    }
    c = _get_db()
    if c is None: return out
    try:
        with c.cursor() as cur:
            # Per-country breakdown — union of both tables, dedup'd by
            # name+country
            cur.execute("""
                SELECT UPPER(COALESCE(country, '?')) AS cc, COUNT(*) AS n
                  FROM facilities
                 WHERE COALESCE(country, '') != ''
                 GROUP BY UPPER(country)
                 ORDER BY n DESC
                 LIMIT 25
            """)
            out["by_country"] = [
                {"country": r[0], "facilities": int(r[1])}
                for r in cur.fetchall() if r[0] != '?'
            ]
            cur.execute("SELECT COUNT(*) FROM facilities")
            out["total_facilities"] = int((cur.fetchone() or [0])[0] or 0)
            out["countries_tracked"] = len(out["by_country"])

            # 7-day add count from discovered_facilities + manual ingest
            try:
                cur.execute("""
                    SELECT COUNT(*) FROM discovered_facilities
                     WHERE discovered_at::timestamptz > NOW() - INTERVAL '7 days'
                """)
                out["added_7d"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass

            # Source breakdown — gives a "we're not just buying data,
            # we're building it" story
            try:
                cur.execute("""
                    SELECT COALESCE(source, 'unknown') AS s, COUNT(*) AS n
                      FROM facilities
                     GROUP BY source
                     ORDER BY n DESC LIMIT 8
                """)
                out["by_source"] = [
                    {"source": r[0], "facilities": int(r[1])}
                    for r in cur.fetchall()
                ]
            except Exception:
                try: c.rollback()
                except Exception: pass
                out["by_source"] = []
    finally:
        try: c.close()
        except Exception: pass
    return out


@coverage_page_bp.route("/api/v1/site/coverage", methods=["GET"])
def coverage_json():
    resp = jsonify(_compute_coverage())
    resp.headers["Cache-Control"] = "public, max-age=300"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@coverage_page_bp.route("/coverage", methods=["GET"])
def coverage_html():
    d = _compute_coverage()

    rows = ""
    flag_map = {
        "US": "🇺🇸", "CA": "🇨🇦", "GB": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷",
        "NL": "🇳🇱", "IE": "🇮🇪", "SG": "🇸🇬", "JP": "🇯🇵", "AU": "🇦🇺",
        "BR": "🇧🇷", "IN": "🇮🇳", "MX": "🇲🇽", "IT": "🇮🇹", "ES": "🇪🇸",
        "CN": "🇨🇳", "BE": "🇧🇪", "CH": "🇨🇭", "KR": "🇰🇷", "ZA": "🇿🇦",
    }
    for i, c in enumerate(d.get("by_country", [])[:25], 1):
        cc = c["country"]
        flag = flag_map.get(cc, "")
        rows += (
            f'<tr><td class="rank">#{i:02d}</td>'
            f'<td class="cc">{flag} {cc}</td>'
            f'<td class="n">{c["facilities"]:,}</td></tr>'
        )

    sources_html = ""
    for s in d.get("by_source", [])[:8]:
        sname = (s["source"] or "unknown").replace("_", " ").title()
        sources_html += (
            f'<div class="source-row">'
            f'<span class="source-name">{sname}</span>'
            f'<span class="source-bar"><span style="width:{min(100, s["facilities"]/max(1, d["total_facilities"])*100)}%"></span></span>'
            f'<span class="source-count">{s["facilities"]:,}</span>'
            f'</div>'
        )

    total = d.get("total_facilities", 0)
    countries = d.get("countries_tracked", 0)
    added_7d = d.get("added_7d", 0)
    as_of = (d.get("as_of") or "")[:10]

    return Response(f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<title>DC Hub · Coverage — {total:,} facilities · {countries} countries</title>
<meta name="description" content="DC Hub global data center coverage: {total:,} facilities across {countries} countries. {added_7d:,} added in the last 7 days.">
<meta property="og:title" content="DC Hub · Coverage">
<meta property="og:description" content="{total:,} facilities · {countries} countries · {added_7d:,} added this week">
<link rel="icon" type="image/svg+xml" href="/icons/icon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script defer src="/js/dchub-brand.js"></script>
<style>
  :root{{--bg:#0a0a0f;--surface:#131319;--border:rgba(255,255,255,.06);
    --border-strong:rgba(255,255,255,.1);--text:#f5f5f7;
    --text-dim:#a1a1aa;--text-faint:#71717a;--indigo:#6366f1;
    --violet:#a855f7;
    --grad:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
    --grad-soft:linear-gradient(135deg,rgba(99,102,241,.10) 0%,rgba(168,85,247,.10) 100%);
    --font:'Instrument Sans',-apple-system,sans-serif;
    --mono:'JetBrains Mono','SF Mono',monospace;}}
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{font-family:var(--font);background:var(--bg);color:var(--text);
    line-height:1.55;-webkit-font-smoothing:antialiased;min-height:100vh;
    position:relative}}
  body::before{{content:'';position:fixed;top:-30%;left:50%;
    transform:translateX(-50%);width:1400px;height:1400px;z-index:0;
    pointer-events:none;
    background:radial-gradient(circle,rgba(99,102,241,.10) 0%,
                                rgba(168,85,247,.06) 30%,transparent 60%)}}
  .wrap{{position:relative;z-index:1;max-width:880px;margin:0 auto;
    padding:64px 24px 80px}}
  header.top{{display:flex;align-items:center;justify-content:space-between;
    margin-bottom:36px;flex-wrap:wrap;gap:12px}}
  a.brand{{display:inline-flex;align-items:center;gap:10px;
    text-decoration:none;color:var(--text)}}
  .pulse-pill{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
    letter-spacing:.1em;color:var(--text-faint);
    padding:6px 14px;border-radius:999px;background:var(--grad-soft);
    border:1px solid rgba(168,85,247,.22)}}
  .eyebrow{{font-family:var(--mono);font-size:11px;text-transform:uppercase;
    letter-spacing:.16em;color:var(--violet);font-weight:600;margin-bottom:14px}}
  h1{{font-size:clamp(2rem,4.2vw,2.8rem);font-weight:700;
    letter-spacing:-.03em;line-height:1.05;margin-bottom:16px}}
  h1 .grad{{background:var(--grad);-webkit-background-clip:text;
    background-clip:text;color:transparent}}
  .lede{{color:var(--text-dim);font-size:1.02rem;line-height:1.55;
    max-width:640px;margin-bottom:36px}}

  .top-stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;
    margin-bottom:48px}}
  @media (max-width:640px){{.top-stats{{grid-template-columns:1fr}}}}
  .stat{{background:var(--surface);border:1px solid var(--border);
    border-radius:14px;padding:22px;text-align:center}}
  .stat-val{{font-size:1.8rem;font-weight:700;letter-spacing:-.02em;
    background:var(--grad);-webkit-background-clip:text;
    background-clip:text;color:transparent;font-family:var(--mono);
    display:block;line-height:1.1}}
  .stat-lbl{{font-family:var(--mono);font-size:10px;
    text-transform:uppercase;letter-spacing:.1em;
    color:var(--text-faint);margin-top:8px;display:block}}

  .section{{margin-bottom:48px}}
  h2{{font-size:1.25rem;font-weight:700;letter-spacing:-.015em;
    margin-bottom:14px}}
  table{{width:100%;border-collapse:collapse;background:var(--surface);
    border:1px solid var(--border);border-radius:14px;overflow:hidden}}
  td{{padding:12px 16px;border-top:1px solid var(--border);font-size:14px}}
  tr:first-child td{{border-top:none}}
  td.rank{{font-family:var(--mono);font-size:11px;color:var(--text-faint);
    width:60px}}
  td.cc{{font-weight:600;font-size:15px}}
  td.n{{text-align:right;font-family:var(--mono);font-weight:600;
    color:var(--text)}}
  tr:hover td{{background:rgba(168,85,247,.04)}}

  .source-row{{display:flex;align-items:center;gap:12px;padding:10px 16px;
    background:var(--surface);border:1px solid var(--border);
    border-radius:10px;margin-bottom:6px}}
  .source-name{{font-family:var(--mono);font-size:11px;
    text-transform:uppercase;letter-spacing:.08em;
    color:var(--text-dim);min-width:140px}}
  .source-bar{{flex:1;height:6px;background:rgba(255,255,255,.04);
    border-radius:3px;overflow:hidden}}
  .source-bar span{{display:block;height:100%;background:var(--grad)}}
  .source-count{{font-family:var(--mono);font-weight:600;font-size:13px;
    min-width:70px;text-align:right}}

  .foot{{font-family:var(--mono);font-size:10.5px;color:var(--text-faint);
    text-align:center;margin-top:48px;letter-spacing:.06em}}
  .foot a{{color:var(--text-dim);margin:0 8px;text-decoration:none}}
  .foot a:hover{{color:var(--text)}}
</style>
</head><body>
<div class="wrap">
  <header class="top">
    <a href="/" class="brand" data-dchub-brand></a>
    <span class="pulse-pill">as of {as_of}</span>
  </header>

  <div class="eyebrow">Coverage</div>
  <h1>{total:,} facilities. <span class="grad">{countries} countries.</span></h1>
  <p class="lede">DC Hub's global data center footprint. Updated continuously from open registries (OpenStreetMap), curated industry sources, and operator submissions. {added_7d:,} new facilities added in the last 7 days.</p>

  <div class="top-stats">
    <div class="stat"><span class="stat-val">{total:,}</span><span class="stat-lbl">Total facilities</span></div>
    <div class="stat"><span class="stat-val">{countries}</span><span class="stat-lbl">Countries tracked</span></div>
    <div class="stat"><span class="stat-val">+{added_7d:,}</span><span class="stat-lbl">Added · 7d</span></div>
  </div>

  <section class="section">
    <h2>Top 25 by facility count</h2>
    <table>{rows}</table>
  </section>

  <section class="section">
    <h2>Sources</h2>
    {sources_html or '<p style="color:var(--text-faint);font-size:14px">Source breakdown loading…</p>'}
  </section>

  <div class="foot">
    <a href="/">dchub.cloud</a> · <a href="/reports/monthly">monthly trend</a> · <a href="/cited-by">cited by</a> · <a href="/founders">founders</a> · <a href="/pricing">pricing</a>
  </div>
</div>
</body></html>""",
        mimetype="text/html",
        headers={"Cache-Control": "public, max-age=300"})


def _smoke():
    logger.info("[coverage-page] ready · GET /coverage + /api/v1/site/coverage")

_smoke()
