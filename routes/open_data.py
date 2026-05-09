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
    return Response(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{r['market_name']} · DC Hub Research</title>
<style>body{{font-family:Georgia,serif;max-width:720px;margin:2rem auto;padding:2rem;line-height:1.7;color:#222}}
h1{{font-size:2.2rem;margin:0 0 0.4rem}}h2{{font-size:1.2rem;margin:1.5rem 0 0.5rem}}
.meta{{color:#666;font-size:0.85rem;margin:0 0 1.5rem}}
.score-block{{background:#f5f5f7;padding:1rem;border-radius:6px;margin:1rem 0}}
.score-block strong{{font-size:1.5rem;display:block}}.cite{{color:#666;font-size:0.85rem;margin-top:2rem;border-top:1px solid #ddd;padding-top:1rem}}</style></head>
<body><h1>{r['market_name']}: A Power Market Report</h1>
<p class="meta">{r['iso']} · {r['state']} · DC Hub · {datetime.datetime.utcnow().strftime("%B %Y")}</p>
<div class="score-block"><strong>Excess Power Score: {r['excess_power_score']}</strong>Constraint Score: {r['constraint_score']} · Verdict: <strong>{r['verdict']}</strong> · Time-to-power: ~{int(r['time_to_power_months'] or 0)} months</div>
<h2>Top Opportunities</h2><ul>{opps_html}</ul>
<h2>Top Risks</h2><ul>{risk_html}</ul>
<p class="cite">Cite as: DC Hub Power Index Research, {r['market_name']}, dchub.cloud/research/{slug}, accessed {datetime.date.today().isoformat()}.</p>
</body></html>""", mimetype="text/html")
