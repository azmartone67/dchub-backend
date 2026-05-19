import datetime
"""Phase 115 — open data + research. The gift to the world.

  GET /data                         landing page
  GET /data/dcpi-history.csv        all historical DCPI scores
  GET /data/dcpi-current.json       current DCPI snapshot
  GET /api/v1/data/manifest         catalog of available datasets
  GET /research                     auto-generated quarterly reports
  GET /research/<market>            single market report (auto-refreshed)
"""
import os, csv, io, datetime
from flask import Blueprint, jsonify, Response
import psycopg2, psycopg2.extras

open_data_bp = Blueprint("open_data", __name__)


def _conn():
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    return psycopg2.connect(db, sslmode="require")


@open_data_bp.route("/data", methods=["GET"])
def data_landing():
    return Response("""<!DOCTYPE html><html><head><meta charset="utf-8"><title>DC Hub · Open Data</title>
<style>body{font-family:-apple-system,system-ui;background:#0a0a12;color:#fff;max-width:780px;margin:2rem auto;padding:2rem;line-height:1.6}
h1{font-size:2rem;margin:0 0 0.4rem}h2{margin:1.5rem 0 0.5rem;color:#9ca3af;font-size:0.9rem;text-transform:uppercase;letter-spacing:0.08em}
a{color:#818cf8}code{background:#1f2030;padding:0.2rem 0.5rem;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:0.85em}
.card{background:#11121a;border:1px solid #1f2030;border-radius:10px;padding:1.2rem;margin:0.8rem 0}.card h3{margin:0 0 0.4rem;font-size:1rem}
.card p{margin:0 0 0.6rem;color:#9ca3af;font-size:0.9rem}</style></head><body>
<h1>DC Hub · Open Data</h1><p style="color:#9ca3af">Free for citation. Updated daily.</p>
<h2>Datasets</h2>
<div class="card"><h3>DCPI History</h3><p>Daily Excess Power + Constraint scores for every U.S. data center market since launch.</p>
<a href="/data/dcpi-history.csv">Download CSV →</a> · <a href="/data/dcpi-current.json">JSON snapshot</a></div>
<div class="card"><h3>API Manifest</h3><p>Machine-readable catalog of every dataset.</p>
<a href="/api/v1/data/manifest">JSON →</a></div>
<h2>Citation</h2><p><code>DC Hub Open Data, dchub.cloud/data, accessed [date]</code></p>
<h2>Contact</h2><p>jonathan@dchub.cloud</p></body></html>""", mimetype="text/html")


@open_data_bp.route("/data/dcpi-history.csv", methods=["GET"])
def dcpi_history_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["computed_at","market_slug","market_name","state","iso",
                "excess_power_score","constraint_score","time_to_power_months","verdict"])
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT computed_at, market_slug, market_name, state, iso,
                           excess_power_score, constraint_score, time_to_power_months, verdict
                           FROM market_power_scores ORDER BY computed_at DESC LIMIT 100000""")
            for row in cur.fetchall():
                w.writerow(row)
    except Exception as e:
        w.writerow([f"# error: {e}"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=dcpi-history.csv"})


@open_data_bp.route("/data/dcpi-current.json", methods=["GET"])
def dcpi_current_json():
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT DISTINCT ON (market_slug) market_slug, market_name, state, iso,
                           excess_power_score, constraint_score, time_to_power_months, verdict, computed_at
                           FROM market_power_scores ORDER BY market_slug, computed_at DESC""")
            rows = cur.fetchall()
        for r in rows:
            if r.get("computed_at"): r["computed_at"] = r["computed_at"].isoformat()
    except Exception:
        rows = []
    return jsonify(snapshot=rows, count=len(rows),
                   citation="DC Hub Open Data, dchub.cloud/data"), 200


@open_data_bp.route("/api/v1/data/manifest", methods=["GET"])
def manifest():
    return jsonify(datasets=[
        {"name":"dcpi-history.csv","url":"/data/dcpi-history.csv","format":"csv","frequency":"daily"},
        {"name":"dcpi-current.json","url":"/data/dcpi-current.json","format":"json","frequency":"daily"},
    ], generated_at=datetime.datetime.utcnow().isoformat()+"Z"), 200


# AUTO-REPAIR: duplicate route '/research' also in main.py:2242 — review and remove one
@open_data_bp.route("/research", methods=["GET"])
def research_landing():
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""SELECT DISTINCT market_slug, market_name FROM market_power_scores
                           ORDER BY market_name""")
            markets = cur.fetchall()
    except Exception:
        markets = []
    items = "".join([f'<li><a href="/research/{m[0]}">{m[1]} — Market Report</a></li>' for m in markets])
    return Response(f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>DC Hub · Research</title>
<style>body{{font-family:-apple-system,system-ui;background:#0a0a12;color:#fff;max-width:780px;margin:2rem auto;padding:2rem}}
a{{color:#818cf8}}h1{{font-size:1.8rem;margin:0 0 0.3rem}}h2{{margin-top:1.5rem;color:#9ca3af;font-size:0.85rem;text-transform:uppercase}}
ul{{padding-left:1.2rem}}li{{margin:0.3rem 0}}</style></head><body>
<h1>DC Hub · Research</h1><p style="color:#9ca3af">Auto-generated. Updated daily. Free to cite.</p>
<h2>Market Reports</h2><ul>{items}</ul></body></html>""", mimetype="text/html")


@open_data_bp.route("/research/<slug>", methods=["GET"])
def research_market(slug):
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""SELECT * FROM market_power_scores WHERE market_slug=%s
                           ORDER BY computed_at DESC LIMIT 1""", (slug,))
            r = cur.fetchone()
    except Exception:
        r = None
    if not r:
        return Response("<h1>Market not found</h1>", status=404, mimetype="text/html")
    risks = (r.get("top_risks_json") or [])
    opps = (r.get("top_opportunities_json") or [])
    risk_html = "".join(f"<li>{x}</li>" for x in risks)
    opps_html = "".join(f"<li>{x}</li>" for x in opps)
    excess = r.get("excess_power_score") or 0
    constraint = r.get("constraint_score") or 0
    excess_color = "#10b981" if excess >= 65 else "#f59e0b" if excess >= 40 else "#ef4444"
    constraint_color = "#ef4444" if constraint >= 70 else "#f59e0b" if constraint >= 45 else "#10b981"
    verdict = r.get("verdict") or "?"
    verdict_color = {"BUILD": "#10b981", "CAUTION": "#f59e0b", "AVOID": "#ef4444"}.get(verdict, "#9ca3af")
    return Response(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{r['market_name']} · DC Hub Research</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="DCPI Research · {r['market_name']}">
<meta property="og:description" content="Excess Power {excess} · Constraint {constraint} · Verdict {verdict}. Updated {r['computed_at'].isoformat()[:10] if r.get('computed_at') else 'today'}.">
<meta property="og:image" content="https://dchub.cloud/api/v1/dcpi/og/{slug}.svg">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#0a0a12; --card:#11121a; --bd:#1f2030; --bd-hi:#2a2c3e;
  --tx:#fff; --tx2:#9ca3af; --tx3:#6b7280;
  --acc:#6366f1; --acc-light:#818cf8;
  --green:#10b981; --orange:#f59e0b; --red:#ef4444;
  --gradient:linear-gradient(135deg,#6366f1 0%,#a855f7 100%);
}}
*{{box-sizing:border-box}}
body{{font-family:'Inter',-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--tx);margin:0;padding:0;line-height:1.7;-webkit-font-smoothing:antialiased}}
code,.mono{{font-family:'JetBrains Mono',monospace}}
.top-nav{{border-bottom:1px solid var(--bd);background:rgba(10,10,18,0.85);backdrop-filter:blur(8px);position:sticky;top:0;z-index:100}}
.top-nav-inner{{max-width:880px;margin:0 auto;padding:1rem 1.5rem;display:flex;align-items:center;justify-content:space-between;gap:1.5rem}}
.logo{{font-weight:800;font-size:1.05rem;color:var(--tx);text-decoration:none}}
.logo span{{color:var(--acc)}}
.nav-links{{display:flex;gap:1.5rem;flex-wrap:wrap}}
.nav-links a{{color:var(--tx2);text-decoration:none;font-size:0.92rem;font-weight:500}}
.nav-links a:hover{{color:var(--tx)}}
.wrap{{max-width:880px;margin:0 auto;padding:3rem 1.5rem}}
.crumbs{{font-family:'JetBrains Mono',monospace;font-size:0.78rem;color:var(--tx3);margin-bottom:1.5rem}}
.crumbs a{{color:var(--acc-light);text-decoration:none}}
.crumbs a:hover{{color:var(--tx)}}
h1{{font-size:clamp(2.4rem,5vw,3.4rem);margin:0 0 0.4rem;font-weight:800;letter-spacing:-0.025em;line-height:1.05}}
.subtitle{{color:var(--tx2);font-family:'JetBrains Mono',monospace;font-size:0.92rem;margin:0 0 2.5rem;text-transform:uppercase;letter-spacing:0.06em}}
.scoreboard{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin:2rem 0}}
.sb{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:1.75rem;position:relative;overflow:hidden}}
.sb::after{{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(99,102,241,0.06),transparent 60%);pointer-events:none}}
.sb .v{{font-family:'JetBrains Mono',monospace;font-size:clamp(3rem,7vw,5rem);font-weight:800;line-height:1;letter-spacing:-0.03em}}
.sb .l{{color:var(--tx2);font-size:0.78rem;text-transform:uppercase;letter-spacing:0.08em;margin-top:0.6rem;font-weight:600}}
.verdict-banner{{padding:1.1rem 1.5rem;border-radius:10px;margin:2rem 0;font-weight:700;font-size:1rem;border:1px solid}}
.section-h{{display:flex;align-items:center;gap:0.6rem;margin:2.5rem 0 1rem;font-size:0.78rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:var(--tx2)}}
.section-h .pip{{width:4px;height:14px;background:var(--acc);border-radius:2px}}
.section{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:1.5rem 1.75rem}}
.section ul{{padding-left:1.4rem;margin:0}}
.section li{{margin:0.5rem 0;color:#ddd;font-size:0.95rem}}
.cite{{color:var(--tx3);font-size:0.85rem;margin-top:3rem;border-top:1px solid var(--bd);padding-top:1.5rem;font-family:'JetBrains Mono',monospace}}
.cite strong{{color:var(--tx2)}}
.share{{margin-top:1.5rem;padding:1.25rem;background:var(--card);border:1px solid var(--bd);border-radius:10px}}
.share .l{{color:var(--tx2);font-size:0.74rem;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:0.5rem}}
.share img{{max-width:100%;border-radius:6px;border:1px solid var(--bd-hi)}}
@media(max-width:600px){{.scoreboard{{grid-template-columns:1fr}}.nav-links{{display:none}}}}
</style>
</head><body>
<nav class="top-nav">
  <div class="top-nav-inner">
    <a class="logo" href="/">DC <span>Hub</span></a>
    <div class="nav-links">
      <a href="/api/v1/dcpi/page">DCPI</a>
      <a href="/api/v1/research">Research</a>
      <a href="/api/v1/data">Open Data</a>
      <a href="/pricing">Pricing</a>
    </div>
  </div>
</nav>
<div class="wrap">
  <div class="crumbs"><a href="/api/v1/research">Research</a> / <a href="/api/v1/dcpi/page/{slug}">{r['market_name']}</a> / Power Market Report</div>
  <h1>{r['market_name']}: A Power Market Report</h1>
  <p class="subtitle">{r['iso']} · {r['state']} · DC Hub Power Index · {r['computed_at'].strftime("%B %Y") if r.get('computed_at') else 'May 2026'}</p>

  <div class="verdict-banner" style="background:rgba({hex_to_rgb(verdict_color)},0.10);border-color:{verdict_color};color:{verdict_color};">
    {'🟢 BUILD HERE — Excess capacity available, manageable constraints.' if verdict=='BUILD' else
     '🟡 CAUTION — Mixed signals, due-diligence required.' if verdict=='CAUTION' else
     '🔴 AVOID FOR NEW BUILDS — Severe constraints, multi-year wait.'}
  </div>

  <div class="scoreboard">
    <div class="sb">
      <div class="v" style="color:{excess_color}">{int(excess)}</div>
      <div class="l">Excess Power Score · Opportunity</div>
    </div>
    <div class="sb">
      <div class="v" style="color:{constraint_color}">{int(constraint)}</div>
      <div class="l">Constraint Score · Avoid</div>
    </div>
  </div>

  <div class="section-h"><span class="pip"></span>🌟 Top Opportunities</div>
  <div class="section"><ul>{opps_html}</ul></div>

  <div class="section-h"><span class="pip"></span>⚠️ Top Risks</div>
  <div class="section"><ul>{risk_html}</ul></div>

  <div class="section-h"><span class="pip"></span>📋 Methodology</div>
  <div class="section">
    <p style="margin:0;color:#ddd;font-size:0.94rem;line-height:1.7">
      <strong>Excess Power Score</strong> ({int(excess)}/100) combines reserve-margin headroom, generation additions queued &lt;12 months, renewable curtailment volume, queue approval rate, stranded interconnection at retiring plants, and behind-the-meter industrial generation.
      <br><br>
      <strong>Constraint Score</strong> ({int(constraint)}/100) combines queue wait time, reserve margin proximity to NERC floor (13%), demand-growth YoY, and 30-day grid-emergency frequency.
      <br><br>
      <strong>Time-to-power</strong> (~{int(r.get('time_to_power_months') or 0)} months) is the ISO's median interconnection-queue wait time adjusted for reserve-margin headroom (faster fast-track when reserves are abundant).
      <br><br>
      Daily refresh from ISO public filings + DC Hub's grid-feed extractors. Free for press citation.
    </p>
  </div>

  <div class="share">
    <div class="l">Embed in your article</div>
    <img src="/api/v1/dcpi/og/{slug}.svg" alt="DCPI {r['market_name']}" />
  </div>

  <p class="cite"><strong>Cite as:</strong> DC Hub Power Index Research, {r['market_name']}, https://dchub.cloud/research/{slug}, accessed {datetime.date.today().isoformat()}.</p>
</div>
</body></html>""", mimetype="text/html")


def hex_to_rgb(hx):
    hx = hx.lstrip("#")
    return f"{int(hx[0:2],16)},{int(hx[2:4],16)},{int(hx[4:6],16)}"



@open_data_bp.route("/api/v1/data", methods=["GET"])
def data_landing_alias():
    return data_landing()

@open_data_bp.route("/api/v1/data/dcpi-history.csv", methods=["GET"])
def dcpi_history_csv_alias():
    return dcpi_history_csv()

@open_data_bp.route("/api/v1/data/dcpi-current.json", methods=["GET"])
def dcpi_current_json_alias():
    return dcpi_current_json()

@open_data_bp.route("/api/v1/research", methods=["GET"])
def research_landing_alias():
    return research_landing()

@open_data_bp.route("/api/v1/research/<slug>", methods=["GET"])
def research_market_alias(slug):
    return research_market(slug)

